"""Official SIAP document templates — fetch, fill, and deliver as editable DOCX.

The PPKP doc-prep co-pilot must hand the citizen the REAL SIAP form (correct
recipient, exact field block), not an invented layout. SIAP hosts these as files
in `ptsp.files` (see services/siap_tools.siap_get_templates); they are served
from Beta storage at `{SIAP_STORAGE_BASE}/storage/{internal_filename}`.

Pipeline per doc_type:
  1. Look up the template's storage key for the resolved licence.
  2. If it is a `.docx` that is actually present on Beta → fetch it and fill the
     citizen's IDENTITY fields (Nama, Jabatan, Alamat, Nomer HP, NIK) in place,
     leaving the vessel fields blank for the citizen. (Decision: identity only.)
  3. Else (legacy `.doc`, or not synced yet, or a fill error) → if BIMA has a
     hand-built DOCX for that doc_type, use it; otherwise return None so the
     caller falls back to the existing fpdf2 generator.

python-docx is imported lazily so this module imports fine where it is absent
(local test env); the container ships it via requirements.txt. Nothing here
raises to the caller — a template miss is a graceful None, never a broken flow.
"""
from __future__ import annotations

import io
import logging
import os
import re
from typing import Any, Optional

import httpx

logger = logging.getLogger("bima_ai.siap_templates")

# Master switch + tunables (env-overridable, no redeploy to flip).
_ENABLED = os.getenv("BIMA_SIAP_TEMPLATES_ENABLED", "true").lower() in ("1", "true", "yes")
_STORAGE_BASE = os.getenv("SIAP_STORAGE_BASE", "https://beta-siap.nolongin.com").rstrip("/")
_FETCH_TIMEOUT = float(os.getenv("BIMA_SIAP_TEMPLATE_TIMEOUT", "20"))
_MAX_TEMPLATE_BYTES = int(os.getenv("BIMA_SIAP_TEMPLATE_MAX_BYTES", str(8 * 1024 * 1024)))

_ID_MONTHS = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
              "Agustus", "September", "Oktober", "November", "Desember"]


def is_enabled() -> bool:
    return _ENABLED


def _today_id() -> str:
    """Indonesian long date, e.g. '2 Juli 2026'. Runtime-only (not import-time)."""
    from datetime import datetime
    now = datetime.now()
    return f"{now.day} {_ID_MONTHS[now.month]} {now.year}"


# ---------------------------------------------------------------------------
# Identity-field mapping: BIMA session field -> label variants seen in the SIAP
# Word templates. Vessel fields are intentionally NOT here (left blank for the
# citizen). Matching is case-insensitive on the label text before the ':'.
# ---------------------------------------------------------------------------
_IDENTITY_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("applicant_name", ("nama pemohon", "nama lengkap", "nama")),
    ("jabatan", ("jabatan",)),
    ("alamat", ("alamat",)),
    ("phone", ("nomer hp", "nomor hp", "no hp", "no. hp", "no telp", "no. telp",
               "nomor telepon", "telepon", "hp")),
    ("nik", ("nik",)),
)


def _identity_value(field_key: str, fields: dict[str, Any]) -> Optional[str]:
    """Resolve the value BIMA has for an identity field, or None to leave blank."""
    if field_key == "jabatan":
        # PPKP applicants are the vessel owner; default when unset.
        return (fields.get("jabatan") or "Pemilik Kapal").strip() or "Pemilik Kapal"
    val = fields.get(field_key)
    if val is None:
        return None
    val = str(val).strip()
    return val or None


def _match_identity_field(label_text: str) -> Optional[str]:
    """Return the field_key whose label matches this line's pre-colon text."""
    low = label_text.strip().lower()
    for field_key, variants in _IDENTITY_LABELS:
        variants_t = (variants,) if isinstance(variants, str) else variants
        for v in variants_t:
            # Whole-label match (allow a trailing '*' or spaces), not substring,
            # so "Nama" does not swallow "Nama Kapal".
            if low == v or low.rstrip(" *") == v:
                return field_key
    return None


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
async def fetch_template_bytes(internal_filename: str) -> Optional[bytes]:
    """GET the template file from Beta SIAP storage. None on any miss/error.

    Not-yet-synced files return 404 → None → caller falls back. Never raises.
    """
    # internal_filename comes from the SIAP DB (ULID paths); guard anyway so a
    # bad/tampered value can't traverse out of /storage or hit another host.
    if (not internal_filename or ".." in internal_filename
            or internal_filename.startswith(("/", "\\"))):
        if internal_filename:
            logger.warning("SIAP template ref rejected (unsafe path)")
        return None
    url = f"{_STORAGE_BASE}/storage/{internal_filename}"
    try:
        # A public static file returns 200 directly; a 30x is anomalous and could
        # redirect off the trusted storage host (SSRF), so refuse to follow it.
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=False) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            logger.info("SIAP template fetch miss | status=%d file=%s",
                        resp.status_code, internal_filename)
            return None
        data = resp.content
        if not data or len(data) > _MAX_TEMPLATE_BYTES:
            logger.warning("SIAP template size out of range | bytes=%d file=%s",
                           len(data or b""), internal_filename)
            return None
        return data
    except Exception:
        logger.exception("SIAP template fetch failed | file=%s", internal_filename)
        return None


# ---------------------------------------------------------------------------
# Fill an existing .docx template in place (identity fields only)
# ---------------------------------------------------------------------------
def _iter_block_paragraphs(container):
    """Yield every paragraph in a doc OR table cell, recursing into nested tables."""
    for p in container.paragraphs:
        yield p
    for table in container.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from _iter_block_paragraphs(cell)


def _set_paragraph_value_after_colon(paragraph, value: str) -> bool:
    """If `paragraph` is a 'Label : <blank>' line, write value after the colon
    without disturbing the label's formatting. Returns True if it filled."""
    text = paragraph.text
    if ":" not in text:
        return False
    label, _, rest = text.partition(":")
    if _match_identity_field(label) is None:
        return False
    # Only fill when the value slot is empty/placeholder (dots/underscores/dashes).
    if re.sub(r"[\s._–\-]", "", rest):
        return False
    # Append the value to the run that carries the colon (preserves font/size).
    for run in reversed(paragraph.runs):
        if ":" in run.text:
            run.text = run.text[: run.text.rindex(":") + 1] + f" {value}"
            return True
    # Fallback: append a new run at the paragraph end.
    paragraph.add_run(f" {value}")
    return True


def fill_docx_identity(docx_bytes: bytes, fields: dict[str, Any]) -> bytes:
    """Fill identity fields into a .docx template, return the edited .docx bytes.

    Best-effort per label; unmatched fields are left as the citizen must fill.
    NOTE: label variants live in _IDENTITY_LABELS — validate against the real
    SIAP templates and extend there if a form uses a different wording.
    """
    from docx import Document  # lazy: only needed when actually filling

    doc = Document(io.BytesIO(docx_bytes))
    filled: list[str] = []
    for paragraph in _iter_block_paragraphs(doc):
        text = paragraph.text
        if ":" not in text:
            continue
        label = text.split(":", 1)[0]
        field_key = _match_identity_field(label)
        if field_key is None:
            continue
        value = _identity_value(field_key, fields)
        if not value:
            continue
        if _set_paragraph_value_after_colon(paragraph, value):
            filled.append(field_key)
    logger.info("SIAP template filled | identity_fields=%s", ",".join(filled) or "none")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Hand-built DOCX for the legacy .doc Surat Permohonan PPKP (SIAP stores it as
# .doc, which python-docx cannot open and we refuse to ship LibreOffice for).
# Layout mirrors the official template `SURAT PERMOHONAN PPKP.doc` verbatim.
# ---------------------------------------------------------------------------
_PERMOHONAN_FIELD_ORDER = (
    ("Nama", "applicant_name"),
    ("Jabatan", "jabatan"),
    ("Alamat", "alamat"),
    ("Nomer HP", "phone"),
    ("NIK", "nik"),
    ("Nama Kapal", None),          # vessel fields: blank for the citizen
    ("Range GT", None),
    ("Bahan Kapal", None),
    ("Alat Penangkap Ikan", None),
    ("Nama Tukang dan Alamat Galangan", None),
)


def build_surat_permohonan_ppkp_docx(fields: dict[str, Any]) -> bytes:
    """Build the official SIAP Surat Permohonan PPKP as an editable .docx,
    identity fields pre-filled, vessel fields left blank."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.shared import Pt

    doc = Document()
    normal = doc.styles["Normal"].font
    normal.name = "Times New Roman"
    normal.size = Pt(12)

    def _p(text="", *, align=None, bold=False, italic=False, size=None):
        p = doc.add_paragraph()
        if align is not None:
            p.alignment = align
        if text:
            r = p.add_run(text)
            r.bold = bold
            r.italic = italic
            if size:
                r.font.size = Pt(size)
        return p

    # Date (right-aligned)
    _p(f"Semarang, {_today_id()}", align=WD_ALIGN_PARAGRAPH.RIGHT)
    _p()
    _p("Kepada Yth.")
    _p("Kepala Dinas Kelautan dan Perikanan Provinsi Jawa Tengah")
    _p("di –")
    _p("\tTEMPAT")
    _p()
    _p("Dengan hormat,")
    _p("Saya yang bertanda tangan di bawah ini, mengajukan Permohonan Persetujuan "
       "Pengadaan Kapal Perikanan (PPKP) Pembangunan / Modifikasi *) sebagai berikut :")

    # Field block as a borderless table: [label | : | value]
    table = doc.add_table(rows=len(_PERMOHONAN_FIELD_ORDER), cols=3)
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = True
    for i, (label, field_key) in enumerate(_PERMOHONAN_FIELD_ORDER):
        cells = table.rows[i].cells
        cells[0].text = label
        cells[1].text = ":"
        value = _identity_value(field_key, fields) if field_key else None
        cells[2].text = value or ""
        # Make the filled identity values visually distinct (bold), like a form.
        if value:
            for r in cells[2].paragraphs[0].runs:
                r.bold = True

    _p()
    _p("Demikian surat permohonan ini saya sampaikan, atas perhatiannya kami "
       "ucapkan terimakasih.")
    _p()
    _p("Hormat Saya,", align=WD_ALIGN_PARAGRAPH.RIGHT)
    _p()
    _p("(Meterai Rp10.000 + Tanda Tangan)", align=WD_ALIGN_PARAGRAPH.RIGHT, italic=True, size=9)
    _p()
    name = _identity_value("applicant_name", fields) or ""
    _p(name, align=WD_ALIGN_PARAGRAPH.RIGHT, bold=True)
    _p("Pemilik Kapal", align=WD_ALIGN_PARAGRAPH.RIGHT)
    _p("*) Coret Yang Tidak Perlu", italic=True, size=9)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# doc_type -> hand-built DOCX builder (used when there is no fillable .docx).
_BUILDERS = {
    "surat_permohonan": build_surat_permohonan_ppkp_docx,
}

# Nice download filenames per doc_type.
_OUT_NAMES = {
    "surat_permohonan": "Surat_Permohonan_PPKP.docx",
    "pakta_integritas": "Pakta_Integritas_PPKP.docx",
    "surat_pesanan": "Surat_Pesanan_PPKP.docx",
}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
async def render_official_docx(
    license_id: Optional[int], doc_type: str, fields: dict[str, Any]
) -> Optional[tuple[bytes, str]]:
    """Return (docx_bytes, download_filename) for the official SIAP template, or
    None so the caller falls back to the existing fpdf2 generator.

    Order: a synced fillable .docx wins (dynamic, always current); otherwise a
    hand-built DOCX (the legacy .doc case); otherwise None (fallback).
    """
    if not _ENABLED:
        return None

    out_name = _OUT_NAMES.get(doc_type, f"{doc_type}.docx")

    internal: Optional[str] = None
    if license_id is not None:
        try:
            from services.siap_tools import siap_get_templates
            info = await siap_get_templates(int(license_id))
            internal = (info.get("by_doctype") or {}).get(doc_type)
        except Exception:
            logger.exception("siap_get_templates lookup failed | license=%s doc=%s",
                             license_id, doc_type)

    # 1) A real .docx on Beta → fetch + fill (dynamic; reflects SIAP edits).
    if internal and internal.lower().endswith(".docx"):
        raw = await fetch_template_bytes(internal)
        if raw:
            try:
                return fill_docx_identity(raw, fields), out_name
            except Exception:
                logger.exception("Fill of SIAP .docx failed | doc=%s file=%s",
                                 doc_type, internal)

    # 2) Legacy .doc / not-synced / fill error → hand-built DOCX if we have one.
    builder = _BUILDERS.get(doc_type)
    if builder:
        try:
            return builder(fields), out_name
        except Exception:
            logger.exception("Hand-built DOCX failed | doc=%s", doc_type)

    # 3) Nothing usable → fall back to fpdf2.
    return None
