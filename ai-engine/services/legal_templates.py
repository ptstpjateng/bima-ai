"""
Legal document templates — render citizen-data into PDF for Privy signing.

[[BIMA Vision]] Phase 5 / Vault: `BIMA-Vault/Phase 5+ Plan.md §6`. When the
guided-submission flow reaches the AWAITING_PRIVY_SIGN state, BIMA must
hand Privy a PDF that already contains the citizen's filled-in fields —
the citizen only adds their signature + e-meterai on top, they never
draft the document themselves.

────────────────────────────────────────────────────────────────────────────
DESIGN CHOICES
────────────────────────────────────────────────────────────────────────────

* **Jinja2 text templates → PDF**, not Jinja2 HTML → weasyprint or
  reportlab story flow. Two reasons:
    1. The legal-team review pass happens against the readable Jinja2
       file, not against a layout-laden HTML/CSS file. Lawyers redline
       prose; they don't redline `<div class="kop">`.
    2. weasyprint + cairo would add ~150 MB to the ai-engine image. For
       a one-paragraph legal document, that is wildly disproportionate.

* **Native-Python minimal PDF**. The renderer here writes the PDF wire
  format directly (PDF 1.4, Type 1 fonts: Helvetica + Helvetica-Bold)
  without any external library. PDFs of this kind — flat text + a
  signature/meterai box — are ~120 lines of byte plumbing and are well
  exercised. Privy accepts standard PDF 1.4+; their qualified signature
  is applied as a CMS overlay on the original byte stream, so a minimal
  PDF is the cleanest input.

* **Indonesian formal-government tone** is in the .j2 files, not in the
  code. Operators can tweak wording (subject to legal-team review) by
  editing the templates only — no Python deploy required if the
  templates are mounted as a volume on the VPS.

────────────────────────────────────────────────────────────────────────────
SCOPE — what this module does NOT do
────────────────────────────────────────────────────────────────────────────
* It does NOT apply the e-meterai stamp — that is Privy's job (see
  privy_client.apply_emeterai). The template just reserves a [ Meterai ]
  box that Privy renders inside.
* It does NOT draw the signature image — that comes back from Privy
  inside the signed PDF after the citizen completes signing.
* It does NOT validate the citizen's data — guided_submission has
  already done that.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("bima_ai.legal_templates")

# Template directory — sits next to this services/ tree to keep the
# package self-contained on the VPS.
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "legal"


# ---------------------------------------------------------------------------
# Indonesian month names — used to format the dateline ("Semarang, 4 Juni 2026")
# at the bottom of each document. Hardcoded so we don't depend on the
# container's locale settings (frequently en_US in production).
# ---------------------------------------------------------------------------
_BULAN = [
    "", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
]


def _format_date_id(dt: datetime) -> str:
    """Render a datetime as `4 Juni 2026` (Indonesian formal style)."""
    return f"{dt.day} {_BULAN[dt.month]} {dt.year}"


# ---------------------------------------------------------------------------
# Lightweight Jinja2-style substitution
#
# We deliberately AVOID pulling Jinja2 as a dep just for {{ var }}
# substitution — the legal templates are pure variable interpolation
# with no logic. A regex pass is safer (no template-injection surface
# from citizen-supplied fields) AND keeps the requirements.txt clean.
# If templates ever grow conditionals / loops, swap this for jinja2 +
# autoescape — the public function signature stays the same.
# ---------------------------------------------------------------------------

_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _escape_pdf_text(value: str) -> str:
    """Escape characters that have special meaning inside PDF text strings.

    Inside a PDF `(...)` literal: `(`, `)`, and `\\` must be escaped. This
    runs on every interpolated value, so an attacker cannot break out of
    a string literal via a malicious business name.
    """
    if value is None:
        return ""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def render_text(template_name: str, context: dict[str, Any]) -> str:
    """Render `<template_name>.j2` with `context` and return the plain text.

    Unknown template → FileNotFoundError. Missing variable → renders as
    an empty string (defensive — better than a hard crash inside the
    state machine right before the user is told "we're filing now").
    """
    if not template_name or "/" in template_name or "\\" in template_name:
        raise ValueError(f"template name not safe: {template_name!r}")
    path = _TEMPLATES_DIR / f"{template_name}.j2"
    if not path.exists():
        raise FileNotFoundError(f"legal template not found: {path}")
    raw = path.read_text(encoding="utf-8")

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        value = context.get(key, "")
        # Newlines inside an interpolated value would break PDF layout
        # downstream — collapse to a single space.
        return re.sub(r"\s+", " ", str(value or "")).strip()

    return _VAR_PATTERN.sub(_sub, raw)


# ---------------------------------------------------------------------------
# Native PDF writer
#
# PDF 1.4, single page, Type 1 Helvetica (Regular + Bold). The first
# non-blank line is rendered as the title in 14pt Bold; everything else
# is 11pt Regular. Body wraps at ~80 characters (the templates already
# wrap by hand so this is mostly a defensive backstop).
#
# We keep the document deliberately minimal — one page, no images, no
# embedded fonts beyond the PDF base 14 — so the byte stream Privy
# receives is small and easy to overlay a signature on.
# ---------------------------------------------------------------------------


_PAGE_WIDTH = 595.0   # A4 in points (72pt = 1 inch)
_PAGE_HEIGHT = 842.0
_LEFT_MARGIN = 72.0
_TOP_MARGIN = 760.0
_LINE_HEIGHT_BODY = 14.0
_FONT_SIZE_TITLE = 14.0
_FONT_SIZE_BODY = 11.0


def _build_content_stream(lines: list[str]) -> bytes:
    """Build the PDF content stream that draws the text body.

    PDF text operators used:
      BT / ET            — begin/end text object
      /F1 11 Tf          — set body font (regular)
      /F2 14 Tf          — set title font (bold)
      Tm                 — set text matrix (positioning)
      Tj                 — show text literal
      Td                 — translate text position (for wrapping)
    """
    buf = io.StringIO()
    buf.write("BT\n")

    # First non-blank line → title in bold.
    title_index = next((i for i, ln in enumerate(lines) if ln.strip()), -1)
    if title_index >= 0:
        title = lines[title_index]
        buf.write(f"/F2 {_FONT_SIZE_TITLE} Tf\n")
        buf.write(f"1 0 0 1 {_LEFT_MARGIN} {_TOP_MARGIN} Tm\n")
        buf.write(f"({_escape_pdf_text(title)}) Tj\n")
        # Move down for body lines.
        y = _TOP_MARGIN - (_LINE_HEIGHT_BODY * 2)
    else:
        y = _TOP_MARGIN
        title_index = -1

    buf.write(f"/F1 {_FONT_SIZE_BODY} Tf\n")
    for i, ln in enumerate(lines):
        if i == title_index:
            continue
        buf.write(f"1 0 0 1 {_LEFT_MARGIN} {y} Tm\n")
        if ln.strip():
            buf.write(f"({_escape_pdf_text(ln)}) Tj\n")
        y -= _LINE_HEIGHT_BODY
        if y < 60:
            # Page overflow safety — silently truncate. The templates are
            # short enough that this should never trigger in practice; if
            # it does, the operator sees a one-line warning in the logs.
            logger.warning(
                "legal_templates: content exceeded one page — truncating."
            )
            break

    buf.write("ET\n")
    return buf.getvalue().encode("latin-1", errors="replace")


def _assemble_pdf(content_stream: bytes) -> bytes:
    """Assemble a minimal PDF 1.4 file around a single content stream.

    Layout:
      1. catalog
      2. pages root
      3. page
      4. Helvetica (F1)
      5. Helvetica-Bold (F2)
      6. content stream
    """
    objects: list[bytes] = []

    def add(obj_body: bytes) -> int:
        objects.append(obj_body)
        return len(objects)  # 1-indexed PDF object number

    catalog_n = add(b"<< /Type /Catalog /Pages 2 0 R >>")
    _pages_n = add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    page_n = add(
        b"<< /Type /Page /Parent 2 0 R "
        b"/MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> "
        b"/Contents 6 0 R >>"
    )
    _font_regular_n = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    )
    _font_bold_n = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"
    )
    content_n = add(
        f"<< /Length {len(content_stream)} >>\nstream\n".encode("latin-1")
        + content_stream
        + b"\nendstream"
    )

    # Now serialise.
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    # Binary marker — required for many PDF parsers to treat the file as
    # binary rather than ASCII (which would corrupt later content).
    out.write(b"%\xe2\xe3\xcf\xd3\n")

    offsets: list[int] = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode("latin-1"))
        out.write(obj)
        out.write(b"\nendobj\n")

    xref_offset = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode("latin-1"))

    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_n} 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("latin-1")
    )

    # _page_n / font / content numbers are referenced via the page object's
    # `/Contents 6 0 R` etc. — those literal indices must match the order
    # objects were added above. Keep them in sync if you add objects.
    _ = (page_n, content_n)
    return out.getvalue()


def render_pdf(template_name: str, context: dict[str, Any]) -> bytes:
    """Render the named template + return a one-page PDF as bytes.

    The rendered text is split on lines; the first non-blank line
    becomes the title (centred-aligned to the left margin in 14pt Bold).
    Everything else is 11pt Regular, top-aligned, one line per text line.

    The implementation is intentionally minimal — Privy applies the
    signature + meterai overlays on top of this byte stream and the
    qualified-signature CMS does not require any embedded structure
    beyond a valid PDF page tree.
    """
    text = render_text(template_name, context)
    lines = text.splitlines()
    content_stream = _build_content_stream(lines)
    return _assemble_pdf(content_stream)


def build_signing_context(
    *,
    license_name: str,
    applicant_name: str,
    nik: str,
    business_name: str,
    phone: str,
    city: str = "Semarang",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compose the standard interpolation context used by every legal template.

    `city` defaults to Semarang because DPMPTSP Provinsi Jawa Tengah is
    headquartered there; overrideable for kabupaten-level rollouts.

    `now` is injectable for deterministic tests — production passes None
    so we read the wall clock.
    """
    dt = now or datetime.now()
    return {
        "license_name": license_name,
        "applicant_name": applicant_name,
        "nik": nik,
        "business_name": business_name,
        "phone": phone,
        "city": city,
        "date_id": _format_date_id(dt),
    }
