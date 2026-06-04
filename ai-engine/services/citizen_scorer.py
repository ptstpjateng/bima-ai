"""
Citizen-facing content-scoring entrypoint — the demo centerpiece.

The guided-submission flow (`services/guided_submission.py`) collects a
citizen's applicant fields AND (new in this slice) the documents they send
over WhatsApp/Telegram. This module is the thin bridge between those
in-session document bytes and the heavy lifter — `suitability_judge.judge_submission()`.

It does TWO things and nothing else:

  1. `score_session_documents()` — given the docs a citizen sent in-session
     plus a SIAP `license_id`, run the suitability judge (which itself runs
     the v1 cross-doc validator's compatibility pass is NOT re-run here —
     this entrypoint only adds completeness + type-correctness + suitability,
     matching the chat slice) and return the structured `SuitabilityResult`.

  2. `render_score_message()` — turn that `SuitabilityResult` into ONE
     WhatsApp-friendly Indonesian message: a headline score %, "X/N lengkap",
     per-document type-correctness lines, materai/suitability flags, and the
     top issues. No new scoring logic — pure presentation over the dataclasses
     the judge already returns.

WHY a separate module (not inside guided_submission.py)?
  * Keeps guided_submission.py's state-machine readable. The scoring +
    rendering is a self-contained concern with its own tests.
  * Lets the officer-brief path (`services/officer_bridge.py`) reuse the same
    renderer so the citizen and the officer see a consistent score summary.

PII: document bytes are NEVER logged. Filenames + license_id (catalogue data)
are logged. Applicant names inside documents are not surfaced by this module —
the judge returns evidence quotes which CAN contain a name; callers that log
the rendered message must treat it as PII (the chat layer does not log replies).
"""

from __future__ import annotations

import logging
from typing import Optional

from services.agents.suitability_judge import (
    Issue,
    SuitabilityResult,
    UploadedDoc,
    judge_submission,
)

logger = logging.getLogger("bima_ai.citizen_scorer")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


async def score_session_documents(
    *,
    license_id: int,
    documents: list[UploadedDoc],
    compatibility_findings: Optional[list[Issue]] = None,
) -> SuitabilityResult:
    """Run the suitability judge over the documents a citizen sent in-session.

    Thin pass-through to `suitability_judge.judge_submission()`. Kept as its
    own function so the chat layer has ONE import surface for scoring and so
    tests can target the entrypoint the citizen flow actually calls.

    Never raises — `judge_submission` is itself fully defensive (Gemini down,
    SIAP DB down) and always returns a structured `SuitabilityResult`.
    """
    logger.info(
        "Citizen content-scoring | license_id=%s docs=%d",
        license_id,
        len(documents),
    )
    return await judge_submission(
        license_id=license_id,
        documents=documents,
        compatibility_findings=compatibility_findings,
    )


# ---------------------------------------------------------------------------
# Rendering — WhatsApp/Telegram-friendly Indonesian
# ---------------------------------------------------------------------------

# Map the canonical DOC_CLASS tokens to a human Indonesian label for display.
_CLASS_LABEL: dict[str, str] = {
    "KTP": "KTP",
    "NIB": "NIB",
    "NPWP": "NPWP",
    "Surat_Domisili": "Surat Domisili",
    "Pakta_Integritas": "Pakta Integritas",
    "SK_Pengangkatan": "SK Pengangkatan",
    "Akta_Pendirian": "Akta Pendirian",
    "IMB_PBG": "IMB/PBG",
    "Other": "Dokumen lain",
    "Unknown": "Tidak terbaca",
}

# Severity → plain-text prefix for the issues list (no emoji — WhatsApp
# replies are kept emoji-free, see BIMA persona / Fix B).
_SEVERITY_BULLET: dict[str, str] = {
    "critical": "- [Penting]",
    "high": "- [Penting]",
    "medium": "-",
    "low": "-",
}

# How many issues to show in the citizen message before "...dan N lainnya".
_MAX_ISSUES_SHOWN = 5


def _label(token: str) -> str:
    return _CLASS_LABEL.get(token, token.replace("_", " "))


def _completeness_line(result: SuitabilityResult) -> str:
    """"X/N dokumen wajib lengkap" — or a neutral note when the registry was
    unavailable (no structured requirements / SIAP DB down)."""
    comp = result.completeness
    if not comp.required:
        # Registry could not be loaded or license has no structured list.
        return f"*Kelengkapan:* {comp.note or 'tidak dapat dinilai otomatis'}"
    total = len(comp.required)
    present = total - len(comp.missing)
    return f"*Kelengkapan dokumen wajib:* {present}/{total} lengkap"


def _type_lines(result: SuitabilityResult) -> list[str]:
    """One line per uploaded document: does its detected type match what the
    citizen labelled it?"""
    lines: list[str] = []
    for f in result.type_correctness:
        if f.detected_type in ("Unknown", ""):
            lines.append(
                f"- {f.file}: tidak dapat dibaca — pastikan foto/PDF jelas"
            )
            continue
        if f.matches:
            lines.append(
                f"- {f.file}: [OK] terbaca sebagai {_label(f.detected_type)}"
            )
        else:
            lines.append(
                f"- {f.file}: Anda labeli *{_label(f.claimed_type)}* "
                f"tapi terbaca sebagai *{_label(f.detected_type)}*"
            )
    return lines


def _materia_or_suitability_lines(result: SuitabilityResult) -> list[str]:
    """Surface suitability mismatches/partials (e.g. a Surat Permohonan that
    is missing materai, or content that does not satisfy the requirement)."""
    lines: list[str] = []
    for f in result.suitability:
        if f.judgement == "match":
            continue
        if f.judgement == "mismatch":
            lines.append(f"- {f.requirement}: belum sesuai — {f.evidence}")
        elif f.judgement == "partial":
            lines.append(f"- {f.requirement}: kurang lengkap — {f.evidence}")
        # "unknown" judgements are not shown to the citizen (vision was down
        # for that pair) — they would just confuse, and the completeness line
        # already covers presence.
    return lines


def render_score_message(
    result: SuitabilityResult,
    *,
    license_name: Optional[str] = None,
) -> str:
    """Render a `SuitabilityResult` as ONE WhatsApp/Telegram message.

    Pure presentation over the judge's dataclasses — no scoring decisions are
    made here. The headline is the unified `overall_suitability_score` as a
    percentage; below it sits completeness, per-doc type checks, suitability
    flags, and a prioritized issue list (worst-first; the judge already sorts
    `issues`).
    """
    percent = int(round(result.overall_suitability_score * 100))

    # Plain-text status band by score — no emoji (WhatsApp replies stay
    # emoji-free, see Fix B). The band word gives the citizen a quick read of
    # whether their packet is in good shape.
    if percent >= 85:
        band = "Lengkap"
    elif percent >= 60:
        band = "Hampir lengkap"
    else:
        band = "Perlu diperbaiki"

    title = (
        f"*Hasil pemeriksaan dokumen {license_name}*"
        if license_name
        else "*Hasil pemeriksaan dokumen Anda*"
    )

    lines: list[str] = [
        title,
        "",
        f"*Skor kelayakan: {percent}%* ({band})",
        _completeness_line(result),
    ]

    type_lines = _type_lines(result)
    if type_lines:
        lines += ["", "*Pemeriksaan jenis dokumen:*", *type_lines]

    suit_lines = _materia_or_suitability_lines(result)
    if suit_lines:
        lines += ["", "*Kesesuaian isi dokumen:*", *suit_lines]

    # Prioritized issues — the judge already sorted worst-first.
    if result.issues:
        lines += ["", "*Yang perlu diperhatikan:*"]
        for issue in result.issues[:_MAX_ISSUES_SHOWN]:
            bullet = _SEVERITY_BULLET.get(issue.severity, "-")
            lines.append(f"{bullet} {issue.title}")
        remaining = len(result.issues) - _MAX_ISSUES_SHOWN
        if remaining > 0:
            lines.append(f"...dan {remaining} catatan lain.")

    return "\n".join(lines).rstrip()


def is_submission_ready(result: SuitabilityResult, *, threshold: float = 0.85) -> bool:
    """Gate helper for the guided flow: a submission is "clean enough" to file
    when the unified score clears `threshold` AND there are no CRITICAL issues
    (a missing required document is always critical).

    This is a presentation/UX gate over the judge's output — it adds no new
    scoring. The citizen can still choose to submit a sub-threshold packet
    (the flow asks for explicit confirmation); this just drives the default
    recommendation wording.
    """
    has_critical = any(i.severity == "critical" for i in result.issues)
    return (result.overall_suitability_score >= threshold) and not has_critical
