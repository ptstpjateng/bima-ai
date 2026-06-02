"""
Suitability judge — Vision req #6 (killer feature) layer 2.

The original validator (`services.agents.validator`) does field consistency
and percentage completion. This module adds three more dimensions on top of
the same underlying Gemini Vision client:

  1. COMPLETENESS    — given a license_id, does the citizen have ALL the
                        required documents? (Today's validator only flags
                        missing FIELDS, not missing documents.)
  2. TYPE-CORRECTNESS — is each uploaded file actually the document it's
                        labelled as? A citizen who labels an NPWP as "KTP"
                        gets called out here, not by the field extractor.
  3. SUITABILITY     — does the document CONTENT satisfy the SPECIFIC
                        requirement text from `ptsp.license_requirements`?
                        e.g. a Surat Domisili must show the business
                        address the citizen claims.

The existing cross-doc consistency findings ("compatibility_findings") are
treated as a third input to the unified score and surfaced verbatim — this
module never re-runs that logic. See [[Phase 5+ Plan]] §5B.

Why a fresh module instead of extending `agents/validator.py`?
  * Keeps the original validator's contract stable (`admin/src/lib/case-types.ts`
    and the demo fixtures lock the shape). The killer-feature output is a
    PR-1 scope; the per-license suitability layer is PR-2 (this file) and
    will get its own admin tab.
  * Lets us cache per-license requirement registries independently from
    extraction calls.
  * Lets the suitability prompt evolve without touching extraction prompts
    that are tuned for OCR fidelity.

Failure model:
  * SIAP DB unreachable → completeness/suitability degrade to "unknown"
    with a note; type-correctness still runs (it doesn't need SIAP).
  * Gemini Vision down → that document's findings degrade to a soft issue,
    but other documents and the cached requirement registry still produce
    a partial answer.
  * No prompt or call ever raises out of the public entrypoint; everything
    is returned as a structured `SuitabilityResult`. The router decides
    HTTP semantics.

Security notes:
  * Document bytes never logged. Filenames are logged (callers pass safe
    names from admin-api — never raw user uploads without a UUID rename).
  * `license_id` is logged plainly because it's catalogue data, not PII.
  * Caching uses license_id as the key (catalogue data); no PII enters
    the cache.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from services.gemini_vision import extract_structured, is_configured as vision_configured
from services.siap_db import get_siap_pool, is_siap_db_configured

logger = logging.getLogger("bima_ai.suitability_judge")


# ---------------------------------------------------------------------------
# Public dataclasses — Pydantic models live in the router; this module is
# import-light (no FastAPI dep) so it can be unit-tested in isolation.
# ---------------------------------------------------------------------------


# The canonical set of document classes Gemini is asked to choose from. Kept
# here (not on the prompt) so callers can match against it deterministically.
DOC_CLASSES: list[str] = [
    "KTP",
    "NIB",
    "NPWP",
    "Surat_Domisili",
    "Pakta_Integritas",
    "SK_Pengangkatan",
    "Akta_Pendirian",
    "IMB_PBG",
    "Other",
]


# Severity vocabulary — same labels the v1 validator uses so the admin UI
# can render both surfaces with one issue card component.
SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"


@dataclass
class UploadedDoc:
    """One uploaded document to judge.

    `content` carries the raw bytes — the suitability prompts run Gemini
    Vision directly on the file. `gcs_url` and `local_path` are kept for
    future shape compatibility (admin-api may eventually pass a URL pointer
    instead of streaming bytes through ai-engine), but `content` is the
    only field that's actually read today.
    """

    file_id: str                       # opaque admin-api identifier — logged
    claimed_type: str                  # what the citizen LABELLED it (KTP, NIB, ...)
    filename: str
    mime_type: str
    content: bytes
    gcs_url: Optional[str] = None      # reserved — future remote-fetch path
    local_path: Optional[str] = None   # reserved


@dataclass
class CompletenessSection:
    score: float                  # 0.0-1.0 → fraction of required types present
    missing: list[str]            # required doc types absent from the upload set
    required: list[str]           # full required list from the registry
    note: Optional[str] = None    # set when the registry was unavailable


@dataclass
class TypeCorrectnessFinding:
    file: str                     # filename for human display
    file_id: str                  # opaque id for cross-referencing
    claimed_type: str
    detected_type: str            # one of DOC_CLASSES (or "Unknown" on failure)
    confidence: float             # 0.0-1.0 — Gemini's self-reported score
    matches: bool                 # convenience: claimed_type == detected_type
    note: Optional[str] = None    # populated on vision failure


@dataclass
class SuitabilityFinding:
    requirement: str              # the requirement text from the registry
    file: Optional[str]           # which file was judged against it (best match)
    file_id: Optional[str]
    judgement: str                # "match" | "partial" | "mismatch" | "unknown"
    evidence: str                 # short quote / explanation from Gemini
    confidence: float             # 0.0-1.0


@dataclass
class Issue:
    id: str                       # stable string for client deduping
    severity: str
    title: str                    # short, Indonesian, user-facing
    detail: str                   # longer, Indonesian


@dataclass
class SuitabilityResult:
    completeness: CompletenessSection
    type_correctness: list[TypeCorrectnessFinding]
    suitability: list[SuitabilityFinding]
    compatibility_findings: list[Issue]      # passed through from v1 validator
    overall_suitability_score: float         # 0.0-1.0 unified score
    issues: list[Issue]                      # prioritized merged issue list


# ---------------------------------------------------------------------------
# Per-license requirement registry — cached in-process for ~5 min.
#
# Source: `ptsp.license_requirements` LEFT JOIN `ptsp.requirements` in
# dbsiapjateng. The join table is sparse — only ~195 / 384 LICENSE rows
# carry structured requirement rows. When a license has no rows here we
# fall back to the HTML lines parsed by services.siap_tools (also DB-only)
# so the registry is non-empty whenever the license exists.
# ---------------------------------------------------------------------------

_REGISTRY_TTL_SECONDS = 300.0  # 5 minutes — catalogue data is essentially static

# license_id -> (expires_at, list of requirement strings)
_registry_cache: dict[int, tuple[float, list[str]]] = {}

_SQL_LICENSE_REQUIREMENTS = """
    SELECT r.name AS requirement_name
      FROM ptsp.license_requirements lr
      JOIN ptsp.requirements r ON r.requirements_id = lr.requirements_id
     WHERE lr.license_id = $1
     ORDER BY r.requirements_id
"""

# Fallback: pull the HTML requirements blob — same source the v1 validator
# would use for free-text requirement display. We don't re-parse the HTML
# here (siap_tools owns that); we just check whether the license exists.
_SQL_LICENSE_HTML_REQUIREMENTS = """
    SELECT properties ->> 'requirements' AS requirements_html
      FROM ptsp.license
     WHERE license_id = $1
       AND stereotype = 'LICENSE'
     LIMIT 1
"""


async def get_license_requirements(license_id: int) -> tuple[list[str], Optional[str]]:
    """
    Return (requirements, note). `requirements` is a list of expected
    document/requirement names; `note` is set when the registry could not
    be loaded (DB unreachable, license unknown) so the caller can degrade
    completeness scoring gracefully.

    Cached in-process for `_REGISTRY_TTL_SECONDS`.
    """
    now = time.monotonic()
    cached = _registry_cache.get(license_id)
    if cached is not None:
        expires_at, reqs = cached
        if expires_at > now:
            return list(reqs), None

    if not is_siap_db_configured():
        return [], "Integrasi basis data SIAP belum dikonfigurasi."

    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SQL_LICENSE_REQUIREMENTS, int(license_id))
            requirements = [
                r["requirement_name"].strip()
                for r in rows
                if r["requirement_name"] and r["requirement_name"].strip()
            ]
            note: Optional[str] = None
            if not requirements:
                # No structured rows — confirm the license itself exists,
                # otherwise the caller's license_id is wrong and we should
                # say so rather than report "0 requirements".
                lic = await conn.fetchrow(_SQL_LICENSE_HTML_REQUIREMENTS, int(license_id))
                if lic is None:
                    note = f"License {license_id} tidak ditemukan di SIAP."
                elif not (lic["requirements_html"] or "").strip():
                    note = (
                        "Daftar persyaratan terstruktur belum tersedia di SIAP "
                        "untuk izin ini."
                    )

        _registry_cache[license_id] = (now + _REGISTRY_TTL_SECONDS, list(requirements))
        return requirements, note
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("get_license_requirements failed | license_id=%s", license_id)
        return [], f"Gagal memuat persyaratan dari SIAP: {exc}"


def _registry_cache_clear() -> None:
    """Test helper — clear the in-process registry cache."""
    _registry_cache.clear()


# ---------------------------------------------------------------------------
# Doc-class normalisation — citizens label files inconsistently ("ktp",
# "KTP", "kartu tanda penduduk"). We canonicalise to the DOC_CLASSES set so
# completeness + type-correctness can compare apples to apples.
# ---------------------------------------------------------------------------

# Aliases include both the v1 validator's lowercase doc_type names and the
# free-text labels admin-api might pass through. The matcher is lossy on
# purpose — anything that doesn't reduce to a known class becomes "Other"
# and the file is still type-checked by Gemini.
_DOC_CLASS_ALIASES: dict[str, str] = {
    "ktp": "KTP",
    "kartu tanda penduduk": "KTP",
    "nib": "NIB",
    "nomor induk berusaha": "NIB",
    "npwp": "NPWP",
    "nomor pokok wajib pajak": "NPWP",
    "surat domisili": "Surat_Domisili",
    "domisili": "Surat_Domisili",
    "surat_domisili": "Surat_Domisili",
    "pakta integritas": "Pakta_Integritas",
    "pakta_integritas": "Pakta_Integritas",
    "sk pengangkatan": "SK_Pengangkatan",
    "sk_pengangkatan": "SK_Pengangkatan",
    "akta pendirian": "Akta_Pendirian",
    "akta_pendirian": "Akta_Pendirian",
    "akta": "Akta_Pendirian",
    "imb": "IMB_PBG",
    "pbg": "IMB_PBG",
    "imb_pbg": "IMB_PBG",
}


def _canonical_class(label: str) -> str:
    """Map a free-text label to a DOC_CLASSES entry; falls back to 'Other'."""
    if not label:
        return "Other"
    key = label.strip().lower().replace("-", " ")
    # Try exact alias hit first; then substring of the canonical token.
    if key in _DOC_CLASS_ALIASES:
        return _DOC_CLASS_ALIASES[key]
    for alias, canonical in _DOC_CLASS_ALIASES.items():
        if alias in key:
            return canonical
    return "Other"


def _requirement_implies_class(requirement: str) -> Optional[str]:
    """Heuristic: derive a canonical DOC_CLASS from a requirement string.

    The registry is free-text Indonesian ("Fotokopi KTP", "Surat Domisili
    Usaha dari Kelurahan", ...). We try to match each requirement to a doc
    class for the completeness check; if nothing matches, the requirement
    is still surfaced under suitability but doesn't gate completeness.
    """
    if not requirement:
        return None
    key = requirement.strip().lower()
    # The aliases table covers most common phrasings; treat any alias
    # appearing as a substring of the requirement as a hit.
    for alias, canonical in _DOC_CLASS_ALIASES.items():
        if alias in key:
            return canonical
    return None


# ---------------------------------------------------------------------------
# Gemini Vision prompts + schemas — TWO calls per uploaded doc:
#   (a) type detection — what kind of document is this, really?
#   (b) suitability    — does this content satisfy this specific
#                        requirement text?
#
# Both calls share the same Gemini model fallback ladder already
# implemented in `services.gemini_vision.extract_structured` — we pass
# through `model_override=None` so the default chain (env-configurable)
# runs. No bespoke API key, no separate quota account.
# ---------------------------------------------------------------------------

_TYPE_DETECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "detected_type": {
            "type": "string",
            "description": (
                "One of: " + ", ".join(DOC_CLASSES) +
                ". Pick the class that best describes the document. If unsure, pick 'Other'."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "Self-rated confidence 0.0-1.0.",
        },
        "rationale": {
            "type": "string",
            "description": "One short sentence — what visual cue led to the classification.",
        },
    },
    "required": ["detected_type", "confidence"],
}

_TYPE_DETECT_PROMPT = (
    "What kind of Indonesian government document is this? "
    "Pick exactly one from: KTP, NIB, NPWP, Surat_Domisili, "
    "Pakta_Integritas, SK_Pengangkatan, Akta_Pendirian, IMB_PBG, Other. "
    "Use the visual layout, headers, official logos, and any visible "
    "issuing-authority text. Provide a 0.0-1.0 confidence score and "
    "a one-sentence rationale. If the image is unreadable or clearly "
    "unrelated to Indonesian licensing, return 'Other' with low confidence."
)

_SUITABILITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "judgement": {
            "type": "string",
            "description": "One of: match, partial, mismatch.",
        },
        "evidence": {
            "type": "string",
            "description": "Short quote or paraphrase from the document supporting the judgement.",
        },
        "confidence": {
            "type": "number",
            "description": "Self-rated confidence 0.0-1.0.",
        },
    },
    "required": ["judgement", "evidence"],
}


def _suitability_prompt(requirement: str) -> str:
    """Per-call prompt — embeds the requirement text verbatim."""
    safe_req = (requirement or "").strip().replace("\n", " ")
    return (
        f"Given this requirement text for an Indonesian permit application:\n"
        f"  «{safe_req}»\n\n"
        f"Does the attached document satisfy that requirement? "
        f"Reply with judgement = 'match' (clearly satisfies), 'partial' "
        f"(plausibly relevant but missing a key detail), or 'mismatch' "
        f"(the document is unrelated or contradicts the requirement). "
        f"Quote one short piece of evidence from the document, and a "
        f"0.0-1.0 confidence score. Be strict — a KTP does not satisfy "
        f"'Surat Domisili' even if both contain an address."
    )


# ---------------------------------------------------------------------------
# Per-document Gemini Vision calls
# ---------------------------------------------------------------------------


async def detect_doc_type(doc: UploadedDoc) -> TypeCorrectnessFinding:
    """Run the type-detection prompt for one document.

    Returns a finding with `detected_type="Unknown"` and a note when Gemini
    Vision fails (no key, network error, malformed response). Never raises.
    """
    claimed_canonical = _canonical_class(doc.claimed_type)

    if not vision_configured():
        return TypeCorrectnessFinding(
            file=doc.filename,
            file_id=doc.file_id,
            claimed_type=claimed_canonical,
            detected_type="Unknown",
            confidence=0.0,
            matches=False,
            note="Gemini Vision belum dikonfigurasi.",
        )

    parsed = await extract_structured(
        image_bytes=doc.content,
        mime_type=doc.mime_type,
        prompt=_TYPE_DETECT_PROMPT,
        response_schema=_TYPE_DETECT_SCHEMA,
    )

    if parsed is None:
        return TypeCorrectnessFinding(
            file=doc.filename,
            file_id=doc.file_id,
            claimed_type=claimed_canonical,
            detected_type="Unknown",
            confidence=0.0,
            matches=False,
            note="Gagal membaca dokumen — pastikan foto/PDF jelas.",
        )

    raw_detected = str(parsed.get("detected_type", "")).strip()
    detected = raw_detected if raw_detected in DOC_CLASSES else "Other"
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return TypeCorrectnessFinding(
        file=doc.filename,
        file_id=doc.file_id,
        claimed_type=claimed_canonical,
        detected_type=detected,
        confidence=confidence,
        matches=(detected == claimed_canonical),
    )


async def judge_suitability(
    requirement: str,
    doc: UploadedDoc,
) -> SuitabilityFinding:
    """Run the suitability prompt for one (requirement, doc) pair.

    Returns judgement="unknown" with evidence note on Gemini failure.
    Never raises.
    """
    if not vision_configured():
        return SuitabilityFinding(
            requirement=requirement,
            file=doc.filename,
            file_id=doc.file_id,
            judgement="unknown",
            evidence="Gemini Vision belum dikonfigurasi.",
            confidence=0.0,
        )

    parsed = await extract_structured(
        image_bytes=doc.content,
        mime_type=doc.mime_type,
        prompt=_suitability_prompt(requirement),
        response_schema=_SUITABILITY_SCHEMA,
    )

    if parsed is None:
        return SuitabilityFinding(
            requirement=requirement,
            file=doc.filename,
            file_id=doc.file_id,
            judgement="unknown",
            evidence="Tidak dapat menilai — pastikan foto/PDF jelas.",
            confidence=0.0,
        )

    judgement = str(parsed.get("judgement", "")).strip().lower()
    if judgement not in {"match", "partial", "mismatch"}:
        judgement = "unknown"
    evidence = str(parsed.get("evidence", "")).strip()[:400] or "—"
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return SuitabilityFinding(
        requirement=requirement,
        file=doc.filename,
        file_id=doc.file_id,
        judgement=judgement,
        evidence=evidence,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Scoring + orchestration
# ---------------------------------------------------------------------------

# Weighting of the three dimensions in the unified `overall_suitability_score`.
# Sum to 1.0. Completeness dominates because a missing document is the
# single most common ground for outright rejection.
_W_COMPLETENESS = 0.45
_W_TYPE = 0.25
_W_SUITABILITY = 0.30


def _completeness(
    docs: list[UploadedDoc],
    required: list[str],
    registry_note: Optional[str],
) -> CompletenessSection:
    """Bucket required requirements that map to a known DOC_CLASS and check
    which classes are represented in the uploaded set."""
    if not required:
        # Registry couldn't load OR the license honestly has no structured
        # requirements. Either way, completeness is "unknown" — score 0.0
        # and the note tells the UI to render a neutral state.
        return CompletenessSection(
            score=0.0,
            missing=[],
            required=[],
            note=registry_note or "Tidak ada daftar persyaratan terstruktur untuk izin ini.",
        )

    # Build the set of canonical classes the registry implies.
    needed_classes: list[str] = []
    seen_set: set[str] = set()
    for req in required:
        cls = _requirement_implies_class(req)
        if cls and cls not in seen_set:
            needed_classes.append(cls)
            seen_set.add(cls)

    # What did the citizen upload (by claimed type)?
    uploaded_classes = {_canonical_class(d.claimed_type) for d in docs}

    missing = [c for c in needed_classes if c not in uploaded_classes]
    total = len(needed_classes)
    if total == 0:
        # No requirement could be classed → can't score; surface registry
        # text as `required` so the admin UI still has something to show.
        return CompletenessSection(
            score=0.0,
            missing=[],
            required=required,
            note=(
                "Persyaratan tercatat tidak bisa dipetakan ke jenis dokumen "
                "yang dikenal — perlu pengecekan manual oleh petugas."
            ),
        )

    present = total - len(missing)
    return CompletenessSection(
        score=round(present / total, 3),
        missing=missing,
        required=required,
    )


def _type_avg_score(findings: list[TypeCorrectnessFinding]) -> float:
    """Average match-with-confidence across uploaded docs. 0.0 when none."""
    if not findings:
        return 0.0
    score = 0.0
    n = 0
    for f in findings:
        n += 1
        if f.matches:
            score += max(0.5, f.confidence)  # a match with low confidence still counts at 0.5
        else:
            # mismatch hurts the most; "Other"/Unknown is a soft penalty so
            # niche docs (e.g. an attachment we don't classify) don't tank
            # the score by themselves.
            if f.detected_type in {"Unknown", "Other"}:
                score += 0.5
            else:
                score += 0.0
    return round(score / n, 3) if n else 0.0


def _suitability_avg_score(findings: list[SuitabilityFinding]) -> float:
    if not findings:
        return 0.0
    weights = {"match": 1.0, "partial": 0.5, "mismatch": 0.0, "unknown": 0.5}
    total = 0.0
    n = 0
    for f in findings:
        n += 1
        total += weights.get(f.judgement, 0.5) * max(0.5, f.confidence)
    return round(total / n, 3) if n else 0.0


def _build_issues(
    completeness: CompletenessSection,
    type_findings: list[TypeCorrectnessFinding],
    suit_findings: list[SuitabilityFinding],
    compatibility: list[Issue],
) -> list[Issue]:
    """Merge per-dimension findings into a single prioritized issue list.

    Severity rules:
      * Missing required doc        → critical
      * Type mismatch (non-Other)   → high
      * Suitability mismatch        → high
      * Suitability partial         → medium
      * Type Unknown/Other          → low (informational)
    """
    issues: list[Issue] = []

    for missing_class in completeness.missing:
        issues.append(Issue(
            id=f"completeness:missing:{missing_class}",
            severity=SEVERITY_CRITICAL,
            title=f"Dokumen wajib belum diunggah: {missing_class}",
            detail=(
                f"Izin ini memerlukan dokumen kelas {missing_class}, "
                f"namun belum terdeteksi di berkas yang diunggah. "
                f"Mohon lengkapi sebelum dikirim ke PTSP."
            ),
        ))

    for f in type_findings:
        if not f.matches and f.detected_type not in {"Unknown", "Other"}:
            issues.append(Issue(
                id=f"type:mismatch:{f.file_id}",
                severity=SEVERITY_HIGH,
                title=f"Label dokumen tidak cocok: {f.file}",
                detail=(
                    f"Berkas '{f.file}' diberi label '{f.claimed_type}' "
                    f"tapi terdeteksi sebagai '{f.detected_type}' "
                    f"(kepercayaan {int(f.confidence * 100)}%). "
                    f"Mohon periksa kembali sebelum dikirim."
                ),
            ))
        elif f.detected_type == "Unknown" and f.note:
            issues.append(Issue(
                id=f"type:unreadable:{f.file_id}",
                severity=SEVERITY_LOW,
                title=f"Tidak dapat membaca dokumen: {f.file}",
                detail=f.note,
            ))

    for f in suit_findings:
        if f.judgement == "mismatch":
            issues.append(Issue(
                id=f"suitability:mismatch:{f.file_id or '_'}:{f.requirement[:40]}",
                severity=SEVERITY_HIGH,
                title=f"Dokumen tidak sesuai persyaratan: {f.requirement}",
                detail=(
                    f"Bukti: {f.evidence} "
                    f"(kepercayaan {int(f.confidence * 100)}%)."
                ),
            ))
        elif f.judgement == "partial":
            issues.append(Issue(
                id=f"suitability:partial:{f.file_id or '_'}:{f.requirement[:40]}",
                severity=SEVERITY_MEDIUM,
                title=f"Dokumen kurang lengkap untuk: {f.requirement}",
                detail=(
                    f"Bukti: {f.evidence} "
                    f"(kepercayaan {int(f.confidence * 100)}%)."
                ),
            ))

    # Compatibility findings come in already shaped as Issues — append as-is.
    issues.extend(compatibility)

    return issues


def _match_doc_for_requirement(
    requirement: str,
    docs: list[UploadedDoc],
    type_findings: list[TypeCorrectnessFinding],
) -> Optional[UploadedDoc]:
    """Pick the best uploaded doc to judge against this requirement.

    Strategy: if the requirement implies a known DOC_CLASS, prefer a doc
    whose DETECTED type matches; fall back to claimed type; otherwise None.
    """
    needed = _requirement_implies_class(requirement)
    if needed is None:
        return None
    finding_by_id = {f.file_id: f for f in type_findings}
    # First pass — detected type matches.
    for d in docs:
        f = finding_by_id.get(d.file_id)
        if f is not None and f.detected_type == needed:
            return d
    # Second pass — claimed type matches.
    for d in docs:
        if _canonical_class(d.claimed_type) == needed:
            return d
    return None


async def judge_submission(
    *,
    license_id: int,
    documents: list[UploadedDoc],
    compatibility_findings: Optional[list[Issue]] = None,
) -> SuitabilityResult:
    """End-to-end: registry → type detection (parallel) → suitability
    (parallel) → score → issues.

    `compatibility_findings` is the prior cross-doc validator output passed
    through verbatim (as Issues); when omitted, this dimension is treated
    as empty.
    """
    compatibility_findings = list(compatibility_findings or [])

    # Registry first — needed for both completeness AND suitability.
    requirements, registry_note = await get_license_requirements(license_id)

    # Type detection per uploaded doc, in parallel.
    type_findings: list[TypeCorrectnessFinding] = []
    if documents:
        type_findings = list(
            await asyncio.gather(*[detect_doc_type(d) for d in documents])
        )

    # Suitability — one prompt per (requirement, best-matching-doc) pair.
    # We only ask Gemini when we can pair a requirement with a doc; un-
    # paired requirements are surfaced as completeness gaps, not
    # suitability questions.
    suitability_tasks: list[tuple[str, UploadedDoc]] = []
    for req in requirements:
        doc = _match_doc_for_requirement(req, documents, type_findings)
        if doc is not None:
            suitability_tasks.append((req, doc))

    suit_findings: list[SuitabilityFinding] = []
    if suitability_tasks:
        suit_findings = list(
            await asyncio.gather(*[
                judge_suitability(req, d) for req, d in suitability_tasks
            ])
        )

    completeness = _completeness(documents, requirements, registry_note)
    type_score = _type_avg_score(type_findings)
    suit_score = _suitability_avg_score(suit_findings)

    # Unified score — completeness dominates by design.
    overall = round(
        _W_COMPLETENESS * completeness.score
        + _W_TYPE * type_score
        + _W_SUITABILITY * suit_score,
        3,
    )

    issues = _build_issues(completeness, type_findings, suit_findings, compatibility_findings)

    # Stable severity ordering so the admin UI doesn't shuffle on re-run.
    severity_rank = {
        SEVERITY_CRITICAL: 0,
        SEVERITY_HIGH: 1,
        SEVERITY_MEDIUM: 2,
        SEVERITY_LOW: 3,
    }
    issues.sort(key=lambda i: severity_rank.get(i.severity, 99))

    logger.info(
        "judge_submission | license_id=%s docs=%d reqs=%d "
        "complete=%.3f type=%.3f suit=%.3f overall=%.3f issues=%d",
        license_id,
        len(documents),
        len(requirements),
        completeness.score,
        type_score,
        suit_score,
        overall,
        len(issues),
    )

    return SuitabilityResult(
        completeness=completeness,
        type_correctness=type_findings,
        suitability=suit_findings,
        compatibility_findings=compatibility_findings,
        overall_suitability_score=overall,
        issues=issues,
    )
