"""
Officer-copilot document Q&A helpers — Vision req #8 enhancement (Phase 5C).

Background:
  The officer copilot's first-pass `get_doc_summary(file_id)` is a one-shot
  summary. Vision req #8's *"chat with documents"* line implies multi-turn,
  PAGE-LEVEL Q&A: *"what's on page 2 of the KTP?"*, *"find the signature
  stamp"*, *"extract the address field from the NIB"*. This module is the
  read-only engine behind those follow-ups. The copilot's three new tools
  (`read_doc_page`, `extract_field`, `find_in_doc`) all funnel through here.

Officer-in-the-loop, read-only:
  Every entry point here is a PURE READ. No SIAP writes, no DB mutation, no
  network side effects. The copilot's existing write tools (`forward_case`,
  `record_decision`) stay the only path that can change SIAP state, and they
  still demand explicit officer confirmation. Doc Q&A is an inspection layer
  — it only ever helps the officer SEE.

Why a separate module:
  `officer_copilot.py` is the agent's tool registry and chat loop. Keeping
  the document-fetch + PDF-paging + Vision-narration plumbing here keeps
  that file from ballooning and lets us unit-test paging without spinning up
  the whole agent. The copilot just imports the three thin async wrappers.

Source of document bytes (and why a ContextVar):
  SIAP does not currently expose a file-download endpoint (verified against
  [[SIAP API Inventory]] Tool 13). admin-api is the layer that DOES hold
  document bytes — it accepts them on the case-validation POST. So admin-api
  injects an in-memory `{file_id → DocBytes}` map per copilot turn, exactly
  like it already injects the validation result. We carry that map via a
  `ContextVar` so:
    * each asyncio task gets its own isolated copy (no cross-officer leak),
    * the tools stay plain module-level functions (consistent with the rest
      of `officer_copilot.py`),
    * when SIAP eventually adds a file-download endpoint we can keep the
      ContextVar as a CACHE and add a `_fetch_doc_bytes(file_id)` fallback
      that hits SIAP — without touching tool signatures.

PDF library choice — pypdf:
  Pure-Python, no system deps, fits the FrankenPHP/python3.11 container.
  Single-image inputs (JPG/PNG) are treated as a one-page doc; we never
  rasterise pages (no PyMuPDF/poppler dependency). For Gemini Vision calls
  we send EITHER the original image bytes (image MIME) OR a per-page text
  excerpt (PDF MIME, extracted text). That keeps the worker container slim.

Token-budget guard:
  Gemini Vision is happy with multi-MB image inputs but the copilot is
  internal-officer-facing, not citizen-facing — we keep latency human-feel.
  Hard caps:
    * MAX_PAGES_PER_CALL = 5 (extract_field/find_in_doc never iterate more)
    * MAX_TEXT_PER_PAGE  = 4000 chars in returned excerpts
    * MAX_DOC_BYTES      = 20 MB (refuse anything larger before parsing)
  These ceilings live as module constants so they're easy to flex if a real
  document type forces it.
"""

from __future__ import annotations

import contextvars
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Budgets — fine to flex via env later; today they live as constants because
# (a) every tool uses the SAME budget and (b) flexing one without the other
# usually means we're working around the wrong limit.
# ---------------------------------------------------------------------------

MAX_PAGES_PER_CALL: int = 5
MAX_TEXT_PER_PAGE: int = 4000
MAX_DOC_BYTES: int = 20 * 1024 * 1024  # 20 MB
TEXT_EXCERPT_TRUNCATION_HINT: str = "…"

# Image MIME types the copilot treats as single-page docs. PDFs always take
# the multi-page code path (even one-page PDFs go through pypdf).
_IMAGE_MIMES: frozenset[str] = frozenset({
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic",
})
_PDF_MIME: str = "application/pdf"


# ---------------------------------------------------------------------------
# DocBytes — what admin-api injects per turn.
#
# We deliberately keep this a plain dataclass (not a pydantic model) so the
# module has no admin-api-side dependencies; the boundary between the two
# services is the ContextVar, not a shared type.
# ---------------------------------------------------------------------------


@dataclass
class DocBytes:
    """One uploaded document attached to the copilot session.

    Fields:
      file_id:   stable handle the officer / model refers to it by; can be a
                 SIAP `files.file_id` once that endpoint exists, or the
                 client-side upload UUID admin-api hands out today.
      filename:  display name (e.g. "ktp_budi.pdf") — surfaced in tool output
                 so the officer can correlate.
      mime_type: image/* or application/pdf. Anything else is rejected at
                 ingest time (see `_load_pages`).
      content:   raw bytes. Held in memory only — admin-api never persists
                 these across turns; on next turn it re-injects.
    """

    file_id: str
    filename: str
    mime_type: str
    content: bytes


# Per-call doc registry. `chat()` in officer_copilot sets this; the three Q&A
# tools read it. Default is an empty dict, NOT None, so a missing-doc case is
# always a clear "file_id not in this turn's docs" rather than an
# uninitialised state.
_doc_context: contextvars.ContextVar[dict[str, DocBytes]] = contextvars.ContextVar(
    "officer_copilot_doc_context", default={}
)


def set_doc_context(docs: Optional[dict[str, DocBytes]]) -> contextvars.Token:
    """Bind a doc map for the current task. Returns the token the caller must
    pass to `reset_doc_context` in a `finally:` block — same pattern as the
    validation ContextVar so concurrent copilot turns stay isolated.

    A `None` argument resets to the empty-dict default, which is the safe
    behaviour: tools fall through to a clear `available=False` message
    rather than answering against stale documents from a previous turn."""
    return _doc_context.set(dict(docs) if docs else {})


def reset_doc_context(token: contextvars.Token) -> None:
    """Pair with `set_doc_context`. Restores whatever the previous task saw."""
    _doc_context.reset(token)


def _get_doc(file_id: str) -> Optional[DocBytes]:
    """Look up an injected doc; None when the model passes a file_id that
    admin-api did not include in this turn's payload."""
    fid = (file_id or "").strip()
    if not fid:
        return None
    return _doc_context.get().get(fid)


# ---------------------------------------------------------------------------
# Page model.
#
# We normalise images and PDFs into the SAME page representation so the three
# tools never need to branch on MIME type. An image becomes a one-page
# document (page_num=1) with empty extracted text; a PDF becomes N pages with
# whatever pypdf can pull out of each.
# ---------------------------------------------------------------------------


@dataclass
class Page:
    """One page of a normalised document."""

    page_num: int                  # 1-indexed (matches what a human says)
    text: str                      # extracted text (may be empty for images)
    image_bytes: Optional[bytes]   # only set for image-type docs (whole-page)
    mime_type: str                 # mime_type to send to Gemini Vision


@dataclass
class LoadedDoc:
    """A document after page extraction."""

    file_id: str
    filename: str
    pages: list[Page]
    is_pdf: bool


# ---------------------------------------------------------------------------
# PDF / image page loader.
#
# `_load_pages` is sync — pypdf is a CPU-bound parser, no I/O. The async
# tools below run it directly; if real submissions ever push it past a few
# hundred ms we can wrap it in `asyncio.to_thread`.
# ---------------------------------------------------------------------------


class DocLoadError(Exception):
    """Raised when a doc cannot be parsed. Tools catch this and return a
    structured `{available: False, note: ...}` to the model — they NEVER
    leak the raw exception text upstream."""


def _load_pages(doc: DocBytes) -> LoadedDoc:
    """Decompose one DocBytes into a uniform list of Pages.

    Image docs collapse to a single Page carrying the image bytes; PDFs walk
    every page and pull text via pypdf. Raises `DocLoadError` for unsupported
    MIME types, oversize docs, or unreadable PDFs — the callers catch and
    translate."""
    if len(doc.content) > MAX_DOC_BYTES:
        raise DocLoadError(
            f"Dokumen melebihi batas ukuran ({len(doc.content)} bytes > "
            f"{MAX_DOC_BYTES} bytes)."
        )

    mime = (doc.mime_type or "").lower()
    if mime in _IMAGE_MIMES:
        # Image — one-page doc. We do NOT extract text from images here; the
        # caller (typically `read_doc_page`) will hand the bytes off to
        # Gemini Vision when narrating.
        return LoadedDoc(
            file_id=doc.file_id,
            filename=doc.filename,
            is_pdf=False,
            pages=[Page(
                page_num=1,
                text="",
                image_bytes=doc.content,
                mime_type=mime or "image/jpeg",
            )],
        )

    if mime == _PDF_MIME:
        try:
            # Import inside the function so the module loads even when pypdf
            # is unavailable in a constrained env (e.g. unit test stubbing).
            # If pypdf is missing the error message stays Indonesian.
            from pypdf import PdfReader  # type: ignore
            from pypdf.errors import PdfReadError  # type: ignore
        except ImportError as e:  # pragma: no cover — covered by deps
            raise DocLoadError(
                "Library PDF (pypdf) belum terpasang di ai-engine."
            ) from e

        from io import BytesIO
        try:
            reader = PdfReader(BytesIO(doc.content))
        except PdfReadError as e:
            raise DocLoadError(f"PDF tidak dapat dibaca: {e}") from e
        except Exception as e:  # pragma: no cover — defensive
            raise DocLoadError(f"Gagal membuka PDF: {e}") from e

        pages: list[Page] = []
        for i, pdf_page in enumerate(reader.pages, start=1):
            try:
                text = pdf_page.extract_text() or ""
            except Exception as e:  # pypdf can throw on malformed pages
                logger.warning(
                    "pypdf extract_text failed on page %d of %s | err=%s",
                    i, doc.filename, e,
                )
                text = ""
            # Trim per-page text so a 50-page PDF can't blow the model's
            # context window in one tool call.
            if len(text) > MAX_TEXT_PER_PAGE:
                text = text[:MAX_TEXT_PER_PAGE] + TEXT_EXCERPT_TRUNCATION_HINT
            pages.append(Page(
                page_num=i,
                text=text,
                image_bytes=None,
                mime_type=_PDF_MIME,
            ))
        if not pages:
            raise DocLoadError("PDF tidak berisi halaman apa pun.")
        return LoadedDoc(
            file_id=doc.file_id, filename=doc.filename,
            is_pdf=True, pages=pages,
        )

    raise DocLoadError(
        f"Tipe file '{mime}' belum didukung. Hanya gambar (JPG/PNG/WEBP) dan PDF."
    )


# ---------------------------------------------------------------------------
# Tool 1 — read_doc_page
# ---------------------------------------------------------------------------


async def read_doc_page(file_id: str, page_num: int = 1) -> dict:
    """Return a specific page of a multi-page document for officer Q&A.

    For PDFs we return the extracted-text excerpt; for image docs we return
    a flag the upstream Gemini call can use to attach the original bytes.
    Always returns a structured dict — never raises (tools must keep the
    chat loop alive even on errors).

    Args:
      file_id: stable handle for an injected doc.
      page_num: 1-indexed page (default 1). Out-of-range falls back to the
        first page so the officer always gets *something*.

    Returns:
      {
        "available": bool,
        "file_id": str,
        "filename": str,
        "page_num": int,        # the page actually returned
        "page_count": int,      # total pages in the doc
        "text_excerpt": str,    # extracted text (empty for image docs)
        "has_image_bytes": bool,# true → caller can ask Gemini Vision next
        "note": str,            # one-line guidance for the model to narrate
      }
    """
    doc = _get_doc(file_id)
    if doc is None:
        return {
            "available": False,
            "file_id": file_id,
            "note": (
                f"Dokumen file_id={file_id!r} tidak terlampir pada sesi ini. "
                "Minta petugas mengunggah ulang atau memilih dokumen lain."
            ),
        }

    try:
        loaded = _load_pages(doc)
    except DocLoadError as e:
        return {
            "available": False,
            "file_id": file_id,
            "filename": doc.filename,
            "note": str(e),
        }

    # Clamp page_num to a real page — don't silently 1.
    requested = int(page_num) if page_num else 1
    actual = max(1, min(requested, len(loaded.pages)))
    page = loaded.pages[actual - 1]
    note = (
        f"Halaman {actual} dari {len(loaded.pages)} ({loaded.filename})."
        if requested == actual else
        f"Halaman {requested} di luar jangkauan — menampilkan halaman {actual} "
        f"dari {len(loaded.pages)} ({loaded.filename})."
    )

    return {
        "available": True,
        "file_id": file_id,
        "filename": loaded.filename,
        "page_num": actual,
        "page_count": len(loaded.pages),
        "text_excerpt": page.text,
        "has_image_bytes": page.image_bytes is not None,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Tool 2 — extract_field
#
# Heuristic extraction across pages. We deliberately keep this as a SCAN
# (not a Gemini Vision pass) for two reasons:
#   1. It's deterministic — useful for officers reviewing automated answers.
#   2. It costs zero quota. The big spend is `read_doc_page` → vision.
# When pypdf's text extract is empty (scanned image PDFs) the result is
# `{value: "", confidence: 0, note: "no text layer"}` and the model is
# instructed to suggest `read_doc_page` so a vision pass can handle it.
# ---------------------------------------------------------------------------


# Field-name aliases — Indonesian variants the officer might type. The match
# is case-insensitive and treats spaces/underscores/hyphens as equivalent.
_FIELD_ALIASES: dict[str, list[str]] = {
    "nik": ["nik", "no induk kependudukan", "nomor induk kependudukan",
            "no. induk kependudukan"],
    "nama": ["nama", "nama lengkap", "full name", "name", "atas nama"],
    "alamat": ["alamat", "alamat tempat tinggal", "address"],
    "tanggal_lahir": ["tanggal lahir", "tgl lahir", "tgl. lahir",
                       "date of birth", "lahir"],
    "tempat_lahir": ["tempat lahir", "place of birth"],
    "npwp": ["npwp", "no. npwp", "nomor pokok wajib pajak"],
    "nib": ["nib", "no induk berusaha", "nomor induk berusaha"],
    "kbli": ["kbli", "kode kbli"],
    "jenis_kelamin": ["jenis kelamin", "gender", "kelamin"],
    "kewarganegaraan": ["kewarganegaraan", "nationality"],
    "berlaku_hingga": ["berlaku hingga", "valid until", "valid through"],
    "tanggal_penerbitan": ["tanggal penerbitan", "tanggal terbit",
                           "tgl penerbitan"],
}


def _normalise_field(name: str) -> str:
    """Collapse whitespace/underscore/hyphen so 'Nama Lengkap', 'nama_lengkap'
    and 'NAMA-LENGKAP' all hash the same."""
    return re.sub(r"[\s_\-]+", " ", (name or "").strip().lower())


def _candidate_labels(field_name: str) -> list[str]:
    """Aliases to try when scanning a page for `field_name`. We try:
      1. the exact normalised key (so 'nik' → its alias list),
      2. the first token of a multi-word query (so 'Nama Lengkap' tries the
         'nama' alias list when the curated table has no exact match),
      3. the literal normalised field name itself (so bespoke labels like
         'nomor_sertifikat' still get a fair shot).
    Empty/whitespace inputs return [] and the caller treats the result as a
    no-match."""
    norm = _normalise_field(field_name)
    if not norm:
        return []
    key = norm.replace(" ", "_")
    if key in _FIELD_ALIASES:
        return _FIELD_ALIASES[key]
    # Drop trailing qualifiers ('nama lengkap' → 'nama') so common
    # noun-phrase variants still hit the curated alias bucket.
    first_token = norm.split(" ", 1)[0]
    if first_token != norm and first_token in _FIELD_ALIASES:
        return _FIELD_ALIASES[first_token]
    return [norm]


# Captures the value to the right of a label on the same line. We grab the
# label-with-colon (or label followed by run of whitespace ≥2) and capture
# everything until end-of-line. Conservative — we'd rather miss than make up.
_LABEL_VALUE_PATTERN = (
    r"(?im)^[\s\W]*{label}\s*[:|]?\s+(?P<value>[^\r\n]{{2,200}}?)\s*$"
)


async def extract_field(file_id: str, field_name: str) -> dict:
    """Locate one field across a document's pages and return its value.

    Uses a label-scan against pypdf's extracted text. Never raises.

    Args:
      file_id: stable handle for an injected doc.
      field_name: human field name in Indonesian ('nama', 'alamat', 'nik')
        or a custom label. Aliases in `_FIELD_ALIASES` map a few common
        variants to the same hit.

    Returns:
      {
        "available": bool,
        "file_id": str,
        "filename": str,
        "field": str,            # normalised label that hit, or echoes input
        "value": str,            # extracted value, "" on miss
        "page": int | None,      # 1-indexed page where it was found
        "confidence": float,     # 0.0–1.0; rough — see _confidence_for
        "citation": str,         # short "page X of <filename>" pointer
        "note": str,
      }
    """
    doc = _get_doc(file_id)
    if doc is None:
        return {
            "available": False, "file_id": file_id, "field": field_name,
            "value": "", "confidence": 0.0, "citation": "",
            "note": (
                f"Dokumen file_id={file_id!r} tidak terlampir pada sesi ini."
            ),
        }

    try:
        loaded = _load_pages(doc)
    except DocLoadError as e:
        return {
            "available": False, "file_id": file_id,
            "filename": doc.filename, "field": field_name,
            "value": "", "confidence": 0.0, "citation": "", "note": str(e),
        }

    # Scan first N pages only — keeps a 50-page PDF from O(N) churn.
    pages_to_scan = loaded.pages[:MAX_PAGES_PER_CALL]
    labels = _candidate_labels(field_name)

    for page in pages_to_scan:
        if not page.text:
            continue
        for label in labels:
            # Escape the label for regex; it's user-controlled but only via
            # the field_name argument, so no broader injection surface.
            pat = _LABEL_VALUE_PATTERN.format(label=re.escape(label))
            m = re.search(pat, page.text)
            if m:
                value = m.group("value").strip(" :|\t")
                # Strip a single trailing punctuation like "." or "," which
                # often dangles off OCR'd KTP fields.
                value = re.sub(r"[\s.,;]+$", "", value)
                conf = _confidence_for(value, field_name)
                return {
                    "available": True,
                    "file_id": file_id,
                    "filename": loaded.filename,
                    "field": _normalise_field(field_name),
                    "value": value,
                    "page": page.page_num,
                    "confidence": conf,
                    "citation": f"halaman {page.page_num} dari {loaded.filename}",
                    "note": (
                        f"Ditemukan '{label}' di halaman {page.page_num}."
                    ),
                }

    # Nothing matched. Distinguish "no text layer" (scanned image PDF) from
    # "we read the text but it didn't contain this field" — the former is
    # actionable: the model should ask for `read_doc_page` + vision.
    has_text = any(p.text for p in pages_to_scan)
    note = (
        "Tidak ada lapisan teks di PDF — kemungkinan hasil pindai. "
        "Coba `read_doc_page` agar Vision dapat membaca halaman langsung."
        if loaded.is_pdf and not has_text else
        f"Field '{field_name}' tidak ditemukan di {len(pages_to_scan)} "
        "halaman pertama."
    )
    return {
        "available": True,
        "file_id": file_id,
        "filename": loaded.filename,
        "field": _normalise_field(field_name),
        "value": "",
        "page": None,
        "confidence": 0.0,
        "citation": "",
        "note": note,
    }


def _confidence_for(value: str, field_name: str) -> float:
    """Tiny heuristic confidence — high when the value matches the field's
    expected shape, lower otherwise. Better than a flat 1.0/0.5 because the
    model can use it to hedge its phrasing."""
    norm = _normalise_field(field_name).replace(" ", "_")
    v = (value or "").strip()
    if not v:
        return 0.0
    if norm == "nik" and re.fullmatch(r"\d{16}", v.replace(" ", "")):
        return 0.95
    if norm == "nib" and re.fullmatch(r"\d{13}", v.replace(" ", "")):
        return 0.95
    if norm == "npwp" and re.fullmatch(r"[\d.\-]{15,20}", v):
        return 0.9
    if norm in {"tanggal_lahir", "tanggal_penerbitan", "berlaku_hingga"} \
            and re.search(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", v):
        return 0.85
    # Default — we found a non-empty value next to the requested label.
    return 0.6


# ---------------------------------------------------------------------------
# Tool 3 — find_in_doc
#
# Free-text needle search across pages. Returns the page + a snippet around
# each hit. Used when the officer says "find the signature" or "where does
# this NIB mention KBLI 56101?". Same pypdf-only constraint: scanned PDFs
# (no text layer) return an empty hit list with a helpful note pointing the
# model at `read_doc_page` for a Vision follow-up.
# ---------------------------------------------------------------------------


# Snippet window around a hit, in characters. Wide enough to give the officer
# context for what page text said; tight enough to keep the tool result
# inside the model's context budget when several pages match.
_SNIPPET_WINDOW: int = 80
_MAX_HITS_PER_PAGE: int = 3
_MAX_TOTAL_HITS: int = 10


async def find_in_doc(file_id: str, query: str) -> dict:
    """Locate every page where `query` appears as text in a document.

    Case-insensitive substring scan. Returns the page numbers + a small
    snippet around each hit so the officer (and the model narrating the
    result) can verify in context. Never raises.

    Args:
      file_id: stable handle for an injected doc.
      query: needle text. Empty/whitespace returns an explicit no-op.

    Returns:
      {
        "available": bool,
        "file_id": str,
        "filename": str,
        "query": str,
        "hit_count": int,
        "hits": [
          {"page": int, "evidence": str, "snippet": str}, …  # at most
                                                              # _MAX_TOTAL_HITS
        ],
        "note": str,
      }
    """
    doc = _get_doc(file_id)
    if doc is None:
        return {
            "available": False, "file_id": file_id, "query": query,
            "hit_count": 0, "hits": [],
            "note": (
                f"Dokumen file_id={file_id!r} tidak terlampir pada sesi ini."
            ),
        }

    q = (query or "").strip()
    if not q:
        return {
            "available": True, "file_id": file_id,
            "filename": doc.filename, "query": q,
            "hit_count": 0, "hits": [],
            "note": "Kueri kosong — tidak ada yang dicari.",
        }

    try:
        loaded = _load_pages(doc)
    except DocLoadError as e:
        return {
            "available": False, "file_id": file_id,
            "filename": doc.filename, "query": q,
            "hit_count": 0, "hits": [], "note": str(e),
        }

    hits: list[dict[str, Any]] = []
    needle = q.lower()
    pages_to_scan = loaded.pages[:MAX_PAGES_PER_CALL]
    for page in pages_to_scan:
        if not page.text:
            continue
        text_lower = page.text.lower()
        # Walk all occurrences on this page, capped to _MAX_HITS_PER_PAGE.
        start = 0
        per_page = 0
        while per_page < _MAX_HITS_PER_PAGE:
            idx = text_lower.find(needle, start)
            if idx == -1:
                break
            lo = max(0, idx - _SNIPPET_WINDOW)
            hi = min(len(page.text), idx + len(q) + _SNIPPET_WINDOW)
            snippet = page.text[lo:hi].replace("\n", " ").strip()
            hits.append({
                "page": page.page_num,
                "evidence": page.text[idx:idx + len(q)],
                "snippet": (("…" if lo > 0 else "")
                            + snippet
                            + ("…" if hi < len(page.text) else "")),
            })
            per_page += 1
            start = idx + len(q)
            if len(hits) >= _MAX_TOTAL_HITS:
                break
        if len(hits) >= _MAX_TOTAL_HITS:
            break

    has_text = any(p.text for p in pages_to_scan)
    if not hits:
        note = (
            "Tidak ada lapisan teks di PDF — kemungkinan hasil pindai. "
            "Coba `read_doc_page` agar Vision dapat memeriksa halaman."
            if loaded.is_pdf and not has_text else
            f"'{q}' tidak ditemukan di {len(pages_to_scan)} halaman pertama."
        )
    else:
        note = (
            f"Ditemukan {len(hits)} kemunculan '{q}' di {loaded.filename}."
            + (f" (Pencarian dibatasi ke {MAX_PAGES_PER_CALL} halaman pertama.)"
               if len(loaded.pages) > MAX_PAGES_PER_CALL else "")
        )

    return {
        "available": True,
        "file_id": file_id,
        "filename": loaded.filename,
        "query": q,
        "hit_count": len(hits),
        "hits": hits,
        "note": note,
    }
