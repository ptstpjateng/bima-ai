"""
Submission completion validator agent — the killer feature per [[BIMA Vision]] req #6.

Given a set of supporting documents (KTP, NIB, NPWP, etc.) uploaded as part
of a permit application, this agent:
  1. Extracts structured fields from each document using Gemini Vision.
  2. Cross-validates fields across documents (name consistency, NIK format,
     address consistency, etc.).
  3. Returns a completion score (0.0-1.0) and a prioritized list of issues
     the applicant should fix before submitting.

Design notes:
  - Pure orchestration on top of `services.gemini_vision.extract_structured`.
    All Gemini calls go through that module so retries, errors, and quota
    accounting are centralised.
  - Schemas + prompts per document type live in this file as constants —
    keeps the contract auditable in one place.
  - Cross-validation rules are pure Python (no LLM) so they're deterministic,
    cheap, and unit-testable without network access.
  - Indonesian name fuzzy matching strips honorifics + degree suffixes
    (S.E., S.H., M.SI., HJ., etc.) before comparing — these differ across
    documents even for the same person.

Not yet covered (deferred):
  - SIUP, AKTA, IMB, NIB-Pribadi variants → add as more doc_type constants
  - Validation against the SPECIFIC license's required-fields list
    (today's checks are generic person-identity consistency, not
     license-specific. Per-license validation is Phase 2.5.)
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from services.gemini_vision import extract_structured

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

DocType = str  # ktp | nib | npwp (extensible)

SUPPORTED_DOC_TYPES: list[DocType] = ["ktp", "nib", "npwp"]


@dataclass
class IssueLevel:
    CRITICAL: str = "critical"  # blocks submission outright
    HIGH: str = "high"          # likely rejection if not fixed
    MEDIUM: str = "medium"      # warning, fix recommended
    LOW: str = "low"            # cosmetic / informational


SEVERITY_WEIGHTS: dict[str, int] = {
    "critical": 30,
    "high": 15,
    "medium": 5,
    "low": 1,
}
MAX_DEFICIT = 100  # score floors at 0 when deficit exceeds this


@dataclass
class Document:
    """One uploaded document with its bytes and declared type."""

    doc_type: DocType
    mime_type: str            # e.g. "image/jpeg", "application/pdf"
    filename: str
    content: bytes


@dataclass
class Issue:
    severity: str             # IssueLevel.*
    field: str                # human field identifier (e.g. "nik", "name_consistency")
    message: str              # Indonesian, user-facing
    related_docs: list[str] = field(default_factory=list)  # filenames involved


@dataclass
class ValidationResult:
    score: float                                  # 0.0-1.0
    status: str                                   # "ready" | "needs_minor_corrections" | "needs_major_corrections" | "unverified"
    per_document: dict[DocType, dict[str, Any]]   # extracted fields per doc type
    issues: list[Issue]
    summary: str                                  # one-paragraph plain-Indonesian summary


# ---------------------------------------------------------------------------
# Per-doc extraction schemas + prompts
# ---------------------------------------------------------------------------

_KTP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "nik": {"type": "string", "description": "16-digit NIK exactly as printed, no spaces"},
        "nama": {"type": "string", "description": "Full name as printed on the card"},
        "tempat_lahir": {"type": "string"},
        "tanggal_lahir": {"type": "string", "description": "DD-MM-YYYY format"},
        "jenis_kelamin": {"type": "string"},
        "alamat": {"type": "string"},
        "rt_rw": {"type": "string"},
        "kelurahan_desa": {"type": "string"},
        "kecamatan": {"type": "string"},
        "agama": {"type": "string"},
        "status_perkawinan": {"type": "string"},
        "pekerjaan": {"type": "string"},
        "kewarganegaraan": {"type": "string"},
        "berlaku_hingga": {"type": "string"},
        "kabupaten_kota": {"type": "string", "description": "Header line — KABUPATEN or KOTA"},
        "provinsi": {"type": "string", "description": "Top header — PROVINSI"},
    },
    "required": ["nik", "nama"],
}

_KTP_PROMPT = (
    "Ekstrak data dari Kartu Tanda Penduduk (KTP) Indonesia ini. "
    "Untuk setiap field, salin teks PERSIS seperti tercetak di KTP, "
    "tanpa interpretasi atau koreksi. Pertahankan kapitalisasi dan tanda baca asli. "
    "Untuk NIK, salin 16 digit tanpa spasi atau tanda baca. "
    "Jika ada field yang tidak terbaca atau tidak terlihat di gambar, gunakan string kosong."
)

_NIB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "nib": {"type": "string", "description": "13-digit NIB"},
        "nama_pelaku_usaha": {"type": "string"},
        "alamat": {"type": "string"},
        "kbli_codes": {
            "type": "array",
            "items": {"type": "string", "description": "5-digit KBLI code"},
        },
        "skala_usaha": {"type": "string"},
        "status_modal": {"type": "string", "description": "PMA or PMDN"},
        "tanggal_penerbitan": {"type": "string"},
        "npwp": {"type": "string"},
    },
    "required": ["nib", "nama_pelaku_usaha"],
}

_NIB_PROMPT = (
    "Ekstrak data dari Nomor Induk Berusaha (NIB) ini — biasanya berupa PDF "
    "yang dikeluarkan oleh OSS (Online Single Submission). "
    "Untuk setiap field, salin teks persis seperti tertulis. "
    "Untuk NIB, 13 digit tanpa spasi. KBLI: kumpulkan semua kode 5-digit yang muncul. "
    "Jika field tidak ada, gunakan string kosong (atau array kosong untuk kbli_codes)."
)

_NPWP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "npwp": {
            "type": "string",
            "description": "NPWP dalam format XX.XXX.XXX.X-XXX.XXX atau 15 digit",
        },
        "nama": {"type": "string"},
        "alamat": {"type": "string"},
        "tanggal_terdaftar": {"type": "string"},
    },
    "required": ["npwp", "nama"],
}

_NPWP_PROMPT = (
    "Ekstrak data dari Nomor Pokok Wajib Pajak (NPWP) ini. "
    "Pertahankan format NPWP asli (XX.XXX.XXX.X-XXX.XXX). "
    "Salin nama dan alamat persis seperti tertulis."
)

_SCHEMAS: dict[DocType, dict[str, Any]] = {
    "ktp": _KTP_SCHEMA,
    "nib": _NIB_SCHEMA,
    "npwp": _NPWP_SCHEMA,
}

_PROMPTS: dict[DocType, str] = {
    "ktp": _KTP_PROMPT,
    "nib": _NIB_PROMPT,
    "npwp": _NPWP_PROMPT,
}


# ---------------------------------------------------------------------------
# Single-document extraction
# ---------------------------------------------------------------------------

async def extract_document(doc: Document) -> Optional[dict[str, Any]]:
    """
    Extract structured fields from one document via Gemini Vision.
    Returns parsed fields dict on success, None on any failure.
    """
    if doc.doc_type not in _SCHEMAS:
        logger.warning("Unknown doc_type=%r — skipping extraction", doc.doc_type)
        return None

    return await extract_structured(
        image_bytes=doc.content,
        mime_type=doc.mime_type,
        prompt=_PROMPTS[doc.doc_type],
        response_schema=_SCHEMAS[doc.doc_type],
    )


# ---------------------------------------------------------------------------
# Format validators — pure helpers
# ---------------------------------------------------------------------------

_NIK_PATTERN = re.compile(r"^\d{16}$")
_NIB_PATTERN = re.compile(r"^\d{13}$")
# NPWP can come as 15 digits OR with dots/dash: XX.XXX.XXX.X-XXX.XXX
_NPWP_DIGITS_PATTERN = re.compile(r"^\d{15}$")
_NPWP_FORMATTED_PATTERN = re.compile(r"^\d{2}\.\d{3}\.\d{3}\.\d-\d{3}\.\d{3}$")


def _normalize_nik(raw: str) -> str:
    return re.sub(r"\D", "", str(raw or ""))


def _normalize_npwp(raw: str) -> str:
    return re.sub(r"\D", "", str(raw or ""))


# Indonesian honorifics + degree suffixes that should NOT cause a name mismatch.
_NAME_NOISE_PATTERN = re.compile(
    r"\b(s\.?e\.?|s\.?h\.?|s\.?si\.?|s\.?kom\.?|s\.?pd\.?|s\.?t\.?|m\.?m\.?|"
    r"m\.?si\.?|m\.?pd\.?|m\.?h\.?|m\.?t\.?|m\.?ba\.?|"
    r"dr\.?|drs\.?|dra\.?|prof\.?|"
    r"hj\.?|h\.?|tn\.?|ny\.?|sdr\.?)\b",
    re.IGNORECASE,
)


def _normalize_name(raw: str) -> str:
    """Uppercase, strip honorifics + degree suffixes, collapse whitespace."""
    if not raw:
        return ""
    s = str(raw).upper()
    s = _NAME_NOISE_PATTERN.sub("", s)
    s = re.sub(r"[^A-Z\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _name_similarity(a: str, b: str) -> float:
    """0.0-1.0 fuzzy ratio after Indonesian-name normalisation."""
    na, nb = _normalize_name(a), _normalize_name(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


# ---------------------------------------------------------------------------
# Cross-validation rules
# ---------------------------------------------------------------------------

_NAME_MATCH_THRESHOLD = 0.85  # >= this counts as "same person"


def cross_validate(
    extracted: dict[DocType, Optional[dict[str, Any]]],
    docs: list[Document],
) -> list[Issue]:
    """
    Run all cross-document consistency checks. Pure function.

    `extracted` maps doc_type → extracted fields dict (or None if extraction
    failed). `docs` is the original document list so we can populate
    related_docs filenames on each issue.
    """
    issues: list[Issue] = []

    # Helper to find filename for a doc_type (first match — typical case).
    def _filename_for(dt: DocType) -> str:
        for d in docs:
            if d.doc_type == dt:
                return d.filename
        return dt

    ktp = extracted.get("ktp")
    nib = extracted.get("nib")
    npwp = extracted.get("npwp")

    # --- Format checks (per-document) -----------------------------------
    if ktp:
        nik_norm = _normalize_nik(ktp.get("nik", ""))
        if not _NIK_PATTERN.match(nik_norm):
            issues.append(Issue(
                severity=IssueLevel.HIGH,
                field="nik_format",
                message=(
                    "NIK di KTP tidak sesuai format 16 digit. "
                    "Pastikan foto KTP jelas dan NIK terbaca dengan baik."
                ),
                related_docs=[_filename_for("ktp")],
            ))

    if nib:
        nib_norm = re.sub(r"\D", "", str(nib.get("nib", "")))
        if not _NIB_PATTERN.match(nib_norm):
            issues.append(Issue(
                severity=IssueLevel.HIGH,
                field="nib_format",
                message=(
                    "NIB tidak sesuai format 13 digit. "
                    "Pastikan PDF NIB asli dari OSS, bukan hasil scan yang buram."
                ),
                related_docs=[_filename_for("nib")],
            ))

    if npwp:
        npwp_raw = str(npwp.get("npwp", ""))
        npwp_norm = _normalize_npwp(npwp_raw)
        if not (_NPWP_DIGITS_PATTERN.match(npwp_norm) or
                _NPWP_FORMATTED_PATTERN.match(npwp_raw)):
            issues.append(Issue(
                severity=IssueLevel.MEDIUM,
                field="npwp_format",
                message=(
                    "Format NPWP tidak dikenali. Format standar: "
                    "XX.XXX.XXX.X-XXX.XXX (15 digit total)."
                ),
                related_docs=[_filename_for("npwp")],
            ))

    # --- Cross-document: name consistency ------------------------------
    if ktp and nib:
        sim = _name_similarity(ktp.get("nama", ""), nib.get("nama_pelaku_usaha", ""))
        if sim < _NAME_MATCH_THRESHOLD:
            issues.append(Issue(
                severity=IssueLevel.HIGH,
                field="name_ktp_vs_nib",
                message=(
                    f"Nama di KTP ('{ktp.get('nama', '')}') tidak konsisten dengan "
                    f"nama pelaku usaha di NIB ('{nib.get('nama_pelaku_usaha', '')}'). "
                    f"Kemiripan: {int(sim * 100)}%. "
                    f"Pastikan dokumen atas nama orang yang sama."
                ),
                related_docs=[_filename_for("ktp"), _filename_for("nib")],
            ))

    if ktp and npwp:
        sim = _name_similarity(ktp.get("nama", ""), npwp.get("nama", ""))
        if sim < _NAME_MATCH_THRESHOLD:
            issues.append(Issue(
                severity=IssueLevel.HIGH,
                field="name_ktp_vs_npwp",
                message=(
                    f"Nama di KTP ('{ktp.get('nama', '')}') tidak konsisten dengan "
                    f"nama di NPWP ('{npwp.get('nama', '')}'). "
                    f"Kemiripan: {int(sim * 100)}%. "
                    f"Periksa kembali ejaan nama."
                ),
                related_docs=[_filename_for("ktp"), _filename_for("npwp")],
            ))

    if nib and npwp:
        # The NPWP field on NIB should match the actual NPWP document, if both present.
        nib_npwp = _normalize_npwp(nib.get("npwp", ""))
        actual_npwp = _normalize_npwp(npwp.get("npwp", ""))
        if nib_npwp and actual_npwp and nib_npwp != actual_npwp:
            issues.append(Issue(
                severity=IssueLevel.HIGH,
                field="npwp_nib_vs_npwp_doc",
                message=(
                    f"NPWP yang tercantum di NIB ({nib_npwp}) berbeda dengan "
                    f"NPWP pada dokumen NPWP ({actual_npwp}). "
                    "Salah satu dokumen mungkin sudah tidak berlaku."
                ),
                related_docs=[_filename_for("nib"), _filename_for("npwp")],
            ))

    # --- Cross-document: address consistency (loose check) -------------
    # We only check kabupaten/kota substring presence — full street-level
    # consistency is too fragile (people legitimately operate businesses
    # at a different address from their KTP).
    if ktp and nib:
        ktp_kab = str(ktp.get("kabupaten_kota", "")).upper()
        nib_alamat = str(nib.get("alamat", "")).upper()
        if ktp_kab and nib_alamat and ktp_kab not in nib_alamat:
            # Try partial — strip "KABUPATEN " / "KOTA " prefix
            ktp_kab_short = re.sub(r"^(KABUPATEN|KOTA)\s+", "", ktp_kab)
            if ktp_kab_short and ktp_kab_short not in nib_alamat:
                issues.append(Issue(
                    severity=IssueLevel.LOW,
                    field="address_kab_consistency",
                    message=(
                        f"Domisili KTP ({ktp_kab}) tidak terdeteksi di alamat NIB. "
                        f"Ini bisa wajar (usaha di alamat berbeda dari KTP), tapi "
                        f"pastikan tidak ada kesalahan input."
                    ),
                    related_docs=[_filename_for("ktp"), _filename_for("nib")],
                ))

    # --- Missing-field warnings ----------------------------------------
    if ktp and not str(ktp.get("tanggal_lahir", "")).strip():
        issues.append(Issue(
            severity=IssueLevel.LOW,
            field="ktp_tanggal_lahir_missing",
            message="Tanggal lahir di KTP tidak terbaca — pastikan foto KTP tidak terpotong.",
            related_docs=[_filename_for("ktp")],
        ))

    return issues


def score_from_issues(issues: list[Issue], docs_attempted: int) -> tuple[float, str]:
    """
    Compute a 0.0-1.0 score and a human status label from the issue list.

    Penalty model: each issue subtracts its severity weight from a 100-point
    budget. Score = max(0, 100 - deficit) / 100. We don't reward extra docs
    (presence is binary — either uploaded or not).
    """
    if docs_attempted == 0:
        return 0.0, "unverified"

    deficit = sum(SEVERITY_WEIGHTS.get(i.severity, 0) for i in issues)
    deficit = min(deficit, MAX_DEFICIT)
    score = (100 - deficit) / 100

    # Status enum values are the canonical contract shared with the bima-admin
    # TS client (admin/src/lib/case-types.ts:46-61). Do NOT rename without
    # also updating the client AND the JSON fixtures under tests/fixtures/ —
    # see QA finding C1 (2026-05-19) for the original drift incident.
    if any(i.severity == IssueLevel.CRITICAL for i in issues):
        status = "major_issues"
    elif any(i.severity == IssueLevel.HIGH for i in issues):
        status = "major_issues"
    elif any(i.severity == IssueLevel.MEDIUM for i in issues):
        status = "minor_issues"
    elif issues:
        status = "minor_issues"
    else:
        status = "ready"

    return score, status


def build_summary(score: float, status: str, issue_count: int, docs_attempted: int) -> str:
    """Plain-Indonesian one-paragraph summary for WhatsApp/portal display."""
    pct = int(round(score * 100))
    if status == "ready":
        return (
            f"Kelengkapan {pct}%. Semua dokumen terverifikasi dan konsisten. "
            f"Siap untuk dikirim ke PTSP."
        )
    if status == "minor_issues":
        return (
            f"Kelengkapan {pct}%. Ada {issue_count} catatan kecil yang sebaiknya "
            f"diperbaiki, tapi tidak menghambat pengiriman."
        )
    if status == "major_issues":
        return (
            f"Kelengkapan {pct}%. Ditemukan {issue_count} masalah penting yang "
            f"perlu diperbaiki sebelum dikirim — jika dibiarkan, kemungkinan "
            f"besar berkas akan ditolak di tahap verifikasi."
        )
    return (
        f"Belum bisa diverifikasi sepenuhnya ({docs_attempted} dokumen diproses). "
        f"Pastikan foto/PDF dokumen jelas dan coba lagi."
    )


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

async def validate_submission(documents: list[Document]) -> ValidationResult:
    """
    End-to-end: extract from each document in parallel, cross-validate, score.
    """
    if not documents:
        return ValidationResult(
            score=0.0,
            status="unverified",
            per_document={},
            issues=[],
            summary="Tidak ada dokumen untuk diverifikasi.",
        )

    extraction_tasks = [extract_document(d) for d in documents]
    extracted_lists = await asyncio.gather(*extraction_tasks, return_exceptions=False)

    per_document: dict[DocType, dict[str, Any]] = {}
    failed_docs: list[Document] = []

    for doc, fields in zip(documents, extracted_lists):
        if fields is None:
            failed_docs.append(doc)
            continue
        # If multiple docs share a type, keep the first successful one and
        # log the duplicate so the caller can decide what to do.
        if doc.doc_type in per_document:
            logger.info(
                "Duplicate doc_type=%s — keeping first extraction (filename=%s ignored)",
                doc.doc_type, doc.filename,
            )
            continue
        per_document[doc.doc_type] = fields

    issues = cross_validate(per_document, documents)

    # Add an Issue for each extraction failure so the user knows about it.
    for d in failed_docs:
        issues.append(Issue(
            severity=IssueLevel.HIGH,
            field=f"extraction_failed_{d.doc_type}",
            message=(
                f"Tidak bisa membaca isi dokumen '{d.filename}' ({d.doc_type}). "
                f"Pastikan foto/PDF cukup jelas dan format file didukung "
                f"(JPG, PNG, PDF)."
            ),
            related_docs=[d.filename],
        ))

    score, status = score_from_issues(issues, docs_attempted=len(documents))
    summary = build_summary(score, status, len(issues), len(documents))

    return ValidationResult(
        score=score,
        status=status,
        per_document=per_document,
        issues=issues,
        summary=summary,
    )
