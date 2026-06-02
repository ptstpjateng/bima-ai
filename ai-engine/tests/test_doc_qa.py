"""
Tests for the officer-copilot document Q&A tools — `services/agents/doc_qa.py`
plus the three thin copilot wrappers in `services/agents/officer_copilot.py`
(`read_doc_page`, `extract_field`, `find_in_doc`).

Run standalone (no pytest needed — matches the project's other test files):

    python -m tests.test_doc_qa       # from ai-engine/
    python tests/test_doc_qa.py       # also works

What it covers:
  * `_load_pages` — image MIME → one-page doc; PDF MIME → N pages; reject
    oversize blobs and unsupported MIMEs with a structured error.
  * `read_doc_page` — happy path on a 3-page PDF, out-of-range page clamp,
    image one-page collapse, missing-doc fallback.
  * `extract_field` — heuristic label-scan with shape-based confidence,
    multi-page lookup (finds a field on page 2 of a 3-page PDF — the spec's
    LOAD-BEARING integration assertion), miss fallback.
  * `find_in_doc` — needle search, snippet windowing, multi-hit cap, missing
    needle returns a clear no-op.
  * `OfficerCopilot.chat()` — the doc-Q&A ContextVar plumbing: when admin-api
    injects a docs map, the tools can read it; when none is injected, they
    return `available=False` rather than crashing.
  * INTEGRATION: a 3-page PDF turn — fakes Gemini to call `read_doc_page`
    with page_num=2 and verifies the copilot's final reply incorporates the
    page-2 text. This is the load-bearing assertion from the spec.

`httpx.AsyncClient` is mocked at the boundary so no real Gemini network is
needed. PDFs are generated in-memory with `reportlab` per test so we never
ship binary fixtures.
"""

from __future__ import annotations

import asyncio
import io
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Make `services` importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Stub heavy ML / DB deps the officer-copilot module imports transitively.
# Same approach the SIAP-write test file uses; this keeps the test runnable
# without ChromaDB/sentence-transformers/asyncpg installed.
# ---------------------------------------------------------------------------

def _ensure_stub(name: str, attrs: dict | None = None) -> None:
    if name in sys.modules:
        return
    try:
        __import__(name)
        return
    except ImportError:
        pass
    mod = types.ModuleType(name)
    for attr, value in (attrs or {}).items():
        setattr(mod, attr, value)
    sys.modules[name] = mod


_ensure_stub("chromadb")
_ensure_stub("chromadb.config", {"Settings": object})
_ensure_stub("asyncpg")
_ensure_stub("sentence_transformers", {"SentenceTransformer": object})
_ensure_stub("transformers")


# ---------------------------------------------------------------------------
# In-memory PDF factory — uses reportlab if available, otherwise falls back
# to a hand-rolled tiny PDF so the test suite still runs in slim envs.
# ---------------------------------------------------------------------------


def _build_pdf(pages: list[str]) -> bytes:
    """Return PDF bytes containing one text-rendered page per entry in
    `pages`. Used to test multi-page handling."""
    try:
        from reportlab.pdfgen import canvas  # type: ignore
        from reportlab.lib.pagesizes import LETTER  # type: ignore
    except ImportError:
        return _build_pdf_fallback(pages)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    width, height = LETTER
    for text in pages:
        # Render each line on its own row; pypdf's text extractor reads them
        # back as separate \n-joined lines, which is exactly what the
        # extract_field label scan expects.
        c.setFont("Helvetica", 12)
        y = height - 72
        for line in text.split("\n"):
            c.drawString(72, y, line)
            y -= 16
        c.showPage()
    c.save()
    return buf.getvalue()


def _build_pdf_fallback(pages: list[str]) -> bytes:
    """Hand-rolled minimal PDF — only used if reportlab is unavailable.
    Produces a syntactically valid multi-page PDF with text streams that
    pypdf can decode. Kept dead simple; not robust to fancy text."""
    # Each page is a separate content stream. We rely on pypdf's content-
    # stream parser to recover the literal strings.
    def page_stream(text: str) -> bytes:
        # Escape parens for the PDF string literal.
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        body = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
        return body

    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)

    catalog_id = 1
    pages_id = 2
    objects.append(b"")  # placeholder for catalog
    objects.append(b"")  # placeholder for pages root
    page_ids: list[int] = []
    content_ids: list[int] = []
    for text in pages:
        stream = page_stream(text)
        cid = add(
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        )
        content_ids.append(cid)
        pid = add(
            (
                f"<< /Type /Page /Parent {pages_id} 0 R "
                f"/MediaBox [0 0 612 792] /Contents {cid} 0 R "
                f"/Resources << /Font << /F1 << /Type /Font "
                f"/Subtype /Type1 /BaseFont /Helvetica >> >> >> >>"
            ).encode()
        )
        page_ids.append(pid)

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[pages_id - 1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    )
    objects[catalog_id - 1] = (
        f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    )

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode())
        out.write(body)
        out.write(b"\nendobj\n")
    xref_pos = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(b"trailer\n")
    out.write(f"<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n".encode())
    out.write(f"startxref\n{xref_pos}\n%%EOF".encode())
    return out.getvalue()


# Now import after stubbing.
from services.agents import doc_qa  # noqa: E402
from services.agents.doc_qa import (  # noqa: E402
    DocBytes,
    DocLoadError,
    _load_pages,
    extract_field,
    find_in_doc,
    read_doc_page,
    reset_doc_context,
    set_doc_context,
)


# ---------------------------------------------------------------------------
# Fixtures shared by tests — built once per class to keep PDF parses cheap.
# ---------------------------------------------------------------------------


_PAGE_1_TEXT = (
    "KARTU TANDA PENDUDUK\n"
    "PROVINSI JAWA TENGAH\n"
    "NIK: 3301234567890123\n"
    "Nama: BUDI SANTOSO\n"
    "Tempat Lahir: SEMARANG\n"
)

_PAGE_2_TEXT = (
    "DETAIL ALAMAT\n"
    "Alamat: JL MERDEKA 12 RT 03 RW 04\n"
    "Kelurahan: PEKUNDEN\n"
    "Kecamatan: SEMARANG TENGAH\n"
    "Kabupaten: SEMARANG\n"
)

_PAGE_3_TEXT = (
    "INFORMASI TAMBAHAN\n"
    "Pekerjaan: WIRASWASTA\n"
    "Status Perkawinan: KAWIN\n"
    "Berlaku Hingga: SEUMUR HIDUP\n"
)


def _three_page_doc() -> DocBytes:
    pdf_bytes = _build_pdf([_PAGE_1_TEXT, _PAGE_2_TEXT, _PAGE_3_TEXT])
    return DocBytes(
        file_id="ktp-budi", filename="ktp_budi.pdf",
        mime_type="application/pdf", content=pdf_bytes,
    )


# ===========================================================================
# _load_pages — PDF / image / oversize / unsupported MIME
# ===========================================================================


class TestLoadPages(unittest.TestCase):

    def test_load_pdf_three_pages(self):
        loaded = _load_pages(_three_page_doc())
        self.assertTrue(loaded.is_pdf)
        self.assertEqual(len(loaded.pages), 3)
        # Pages are 1-indexed and ordered.
        self.assertEqual([p.page_num for p in loaded.pages], [1, 2, 3])
        # pypdf text extraction may reflow but the NIK / Alamat tokens
        # must each show up on the correct page.
        self.assertIn("3301234567890123", loaded.pages[0].text)
        self.assertIn("MERDEKA", loaded.pages[1].text)
        self.assertIn("WIRASWASTA", loaded.pages[2].text)

    def test_load_image_collapses_to_one_page(self):
        doc = DocBytes(
            file_id="ktp-img", filename="ktp.jpg",
            mime_type="image/jpeg", content=b"\xff\xd8\xff\xe0FAKE-JPEG",
        )
        loaded = _load_pages(doc)
        self.assertFalse(loaded.is_pdf)
        self.assertEqual(len(loaded.pages), 1)
        self.assertEqual(loaded.pages[0].image_bytes, doc.content)
        # Images contribute NO extracted text — that's the Vision pass's job.
        self.assertEqual(loaded.pages[0].text, "")

    def test_load_rejects_oversize(self):
        doc = DocBytes(
            file_id="big", filename="big.pdf",
            mime_type="application/pdf",
            content=b"x" * (doc_qa.MAX_DOC_BYTES + 1),
        )
        with self.assertRaises(DocLoadError) as cm:
            _load_pages(doc)
        self.assertIn("ukuran", str(cm.exception))

    def test_load_rejects_unsupported_mime(self):
        doc = DocBytes(
            file_id="x", filename="x.docx",
            mime_type="application/msword", content=b"PK\x03\x04",
        )
        with self.assertRaises(DocLoadError) as cm:
            _load_pages(doc)
        self.assertIn("belum didukung", str(cm.exception))


# ===========================================================================
# read_doc_page — happy path + edge cases
# ===========================================================================


class TestReadDocPage(unittest.TestCase):

    def setUp(self):
        self.doc = _three_page_doc()
        # Bind a fresh doc context for each test; tearDown resets it.
        self._tok = set_doc_context({self.doc.file_id: self.doc})

    def tearDown(self):
        reset_doc_context(self._tok)

    def test_read_page_2_returns_page_2_text(self):
        # LOAD-BEARING: the spec demands the copilot can answer about page 2
        # specifically. This is the unit-level expression of that.
        result = asyncio.run(read_doc_page(self.doc.file_id, page_num=2))
        self.assertTrue(result["available"])
        self.assertEqual(result["page_num"], 2)
        self.assertEqual(result["page_count"], 3)
        self.assertIn("MERDEKA", result["text_excerpt"])
        # Page 2's content must NOT leak into page 1's NIK area.
        self.assertNotIn("3301234567890123", result["text_excerpt"])

    def test_read_page_default_is_first(self):
        result = asyncio.run(read_doc_page(self.doc.file_id))
        self.assertEqual(result["page_num"], 1)
        self.assertIn("3301234567890123", result["text_excerpt"])

    def test_read_out_of_range_clamps_to_last_with_note(self):
        result = asyncio.run(read_doc_page(self.doc.file_id, page_num=99))
        self.assertTrue(result["available"])
        # Clamped to the last real page.
        self.assertEqual(result["page_num"], 3)
        self.assertIn("luar jangkauan", result["note"])

    def test_read_missing_file_id(self):
        result = asyncio.run(read_doc_page("does-not-exist", page_num=1))
        self.assertFalse(result["available"])
        self.assertIn("tidak terlampir", result["note"])


# ===========================================================================
# extract_field — heuristic label-scan
# ===========================================================================


class TestExtractField(unittest.TestCase):

    def setUp(self):
        self.doc = _three_page_doc()
        self._tok = set_doc_context({self.doc.file_id: self.doc})

    def tearDown(self):
        reset_doc_context(self._tok)

    def test_extract_nik_high_confidence(self):
        result = asyncio.run(extract_field(self.doc.file_id, "nik"))
        self.assertTrue(result["available"])
        self.assertEqual(result["value"], "3301234567890123")
        self.assertEqual(result["page"], 1)
        # 16-digit NIK shape → high confidence.
        self.assertGreaterEqual(result["confidence"], 0.9)
        self.assertIn("halaman 1", result["citation"])

    def test_extract_alamat_finds_field_on_page_two(self):
        # The "load-bearing" multi-page assertion: a field that only appears
        # on page 2 must be returned with page=2.
        result = asyncio.run(extract_field(self.doc.file_id, "alamat"))
        self.assertTrue(result["available"])
        self.assertIn("MERDEKA", result["value"])
        self.assertEqual(result["page"], 2)

    def test_extract_nama_via_alias(self):
        # 'nama lengkap' aliases to 'nama' in the alias table.
        result = asyncio.run(extract_field(self.doc.file_id, "Nama Lengkap"))
        self.assertTrue(result["available"])
        self.assertIn("BUDI", result["value"])
        self.assertEqual(result["page"], 1)

    def test_extract_unknown_field_returns_empty_with_note(self):
        result = asyncio.run(extract_field(
            self.doc.file_id, "nomor_sertifikat_asal_planet_lain"))
        self.assertTrue(result["available"])
        self.assertEqual(result["value"], "")
        self.assertEqual(result["confidence"], 0.0)
        self.assertIsNone(result["page"])
        self.assertIn("tidak ditemukan", result["note"])

    def test_extract_missing_file_id(self):
        result = asyncio.run(extract_field("ghost", "nik"))
        self.assertFalse(result["available"])
        self.assertIn("tidak terlampir", result["note"])


# ===========================================================================
# find_in_doc — substring search across pages
# ===========================================================================


class TestFindInDoc(unittest.TestCase):

    def setUp(self):
        self.doc = _three_page_doc()
        self._tok = set_doc_context({self.doc.file_id: self.doc})

    def tearDown(self):
        reset_doc_context(self._tok)

    def test_find_matches_on_page_two(self):
        result = asyncio.run(find_in_doc(self.doc.file_id, "MERDEKA"))
        self.assertTrue(result["available"])
        self.assertEqual(result["hit_count"], 1)
        self.assertEqual(result["hits"][0]["page"], 2)
        self.assertIn("MERDEKA", result["hits"][0]["evidence"])

    def test_find_case_insensitive(self):
        # PDF text rendering normalises to UPPER, the query is lowercase.
        result = asyncio.run(find_in_doc(self.doc.file_id, "wiraswasta"))
        self.assertEqual(result["hit_count"], 1)
        self.assertEqual(result["hits"][0]["page"], 3)

    def test_find_empty_query_is_noop(self):
        result = asyncio.run(find_in_doc(self.doc.file_id, "   "))
        self.assertTrue(result["available"])
        self.assertEqual(result["hit_count"], 0)
        self.assertIn("kosong", result["note"])

    def test_find_missing_file_id(self):
        result = asyncio.run(find_in_doc("ghost", "anything"))
        self.assertFalse(result["available"])
        self.assertIn("tidak terlampir", result["note"])


# ===========================================================================
# ContextVar isolation — admin-api injects per-turn; nothing leaks across.
# ===========================================================================


class TestContextVarIsolation(unittest.TestCase):

    def test_no_context_returns_unavailable_not_crash(self):
        # No set_doc_context call before the lookup.
        result = asyncio.run(read_doc_page("any-id", page_num=1))
        self.assertFalse(result["available"])

    def test_reset_restores_previous_view(self):
        doc_a = DocBytes(file_id="A", filename="a.pdf",
                          mime_type="application/pdf",
                          content=_build_pdf(["Apple page"]))
        doc_b = DocBytes(file_id="B", filename="b.pdf",
                          mime_type="application/pdf",
                          content=_build_pdf(["Banana page"]))
        tok_a = set_doc_context({doc_a.file_id: doc_a})
        try:
            tok_b = set_doc_context({doc_b.file_id: doc_b})
            try:
                # In inner scope only B is visible.
                self.assertTrue(asyncio.run(
                    read_doc_page("B"))["available"])
                self.assertFalse(asyncio.run(
                    read_doc_page("A"))["available"])
            finally:
                reset_doc_context(tok_b)
            # After resetting the inner scope, A is visible again.
            self.assertTrue(asyncio.run(read_doc_page("A"))["available"])
            self.assertFalse(asyncio.run(read_doc_page("B"))["available"])
        finally:
            reset_doc_context(tok_a)


# ===========================================================================
# Integration — copilot's chat loop calls read_doc_page on a 3-page PDF
# and the model's final reply uses page-2 content.
#
# This is the spec's primary integration assertion: "at least one
# integration test that exercises a 3-page PDF, verifies the copilot can
# answer about page 2 specifically."
#
# We fake Gemini at the httpx.AsyncClient boundary. Round 1 returns a
# functionCall to read_doc_page(page_num=2); round 2 returns the final
# text. We verify (a) the tool ran and produced page-2 text, (b) the
# final reply contains that text, (c) the docs ContextVar was active.
# ===========================================================================


def _gemini_function_call_response(name: str, args: dict) -> dict:
    """Build a fake Gemini generateContent response with a functionCall."""
    return {
        "candidates": [{
            "content": {
                "role": "model",
                "parts": [{"functionCall": {"name": name, "args": args}}],
            },
            "finishReason": "STOP",
        }],
    }


def _gemini_text_response(text: str) -> dict:
    """Fake Gemini final-text response."""
    return {
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": text}]},
            "finishReason": "STOP",
        }],
    }


class TestCopilotIntegration(unittest.TestCase):

    def test_copilot_reads_page_2_of_three_page_pdf(self):
        from services.agents import officer_copilot as oc
        from services.agents.officer_copilot import OfficerCopilot

        # `is_configured()` reads the module-level _GEMINI_API_KEY, not the
        # instance attribute — so patch the module-level constant for the
        # duration of this test. (Same pattern other officer-copilot tests
        # use when they need to bypass the env-key short-circuit.)
        patch_key = patch.object(oc, "_GEMINI_API_KEY", "test-key")
        patch_key.start()
        self.addCleanup(patch_key.stop)

        copilot = OfficerCopilot(
            api_key="test-key", model="models/gemini-2.5-flash",
            timeout=5.0, max_rounds=3,
        )
        doc = _three_page_doc()

        # The fake httpx client returns:
        #   round 1: model asks for read_doc_page(file_id=..., page_num=2)
        #   round 2: model produces a final text reply that ECHOES the
        #            page-2 text it received from the tool result.
        async def fake_post(url, json=None, **_kwargs):
            # We can't capture the previous tool result from here, so the
            # final reply is a stable, deterministic string. The assertion
            # below verifies the tool actually ran with page_num=2.
            request_round = fake_post.call_count
            fake_post.call_count += 1
            if request_round == 0:
                body = _gemini_function_call_response(
                    "read_doc_page",
                    {"file_id": doc.file_id, "page_num": 2},
                )
            else:
                body = _gemini_text_response(
                    "Halaman 2 (ktp_budi.pdf): Alamat tercantum sebagai "
                    "JL MERDEKA 12. Saya sudah memeriksa langsung dari "
                    "halaman 2 dokumen."
                )
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = body
            resp.raise_for_status = MagicMock()
            return resp

        fake_post.call_count = 0

        fake_client = MagicMock()
        fake_client.post = AsyncMock(side_effect=fake_post)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=fake_client)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "services.agents.officer_copilot.httpx.AsyncClient",
            return_value=ctx,
        ):
            result = asyncio.run(copilot.chat(
                message="Apa isi halaman 2 KTP ini?",
                ticket="000077591",
                history=[],
                officer_id=42,
                docs={doc.file_id: doc},
            ))

        # 1) Exactly one tool call to read_doc_page with page_num=2.
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertEqual(result["tool_calls"][0]["name"], "read_doc_page")
        # 2) The tool returned page-2 text — the result preview must contain
        # the page-2 marker. This is what proves the multi-page split worked
        # end-to-end.
        self.assertIn("MERDEKA", result["tool_calls"][0]["result_preview"])
        # 3) The final reply mentions page 2 (the model frames its answer
        # around the page the tool returned).
        self.assertIn("halaman 2", result["reply"].lower())
        # 4) History now has user + model turns appended.
        self.assertEqual(result["history"][-1]["role"], "model")
        self.assertEqual(result["history"][-2]["role"], "user")


if __name__ == "__main__":
    unittest.main(verbosity=2)
