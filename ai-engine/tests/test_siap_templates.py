"""Official SIAP template rendering (services/siap_templates.py).

Covers the label matcher, the hand-built Surat Permohonan PPKP DOCX, and the
in-place identity fill. Skips automatically where python-docx / httpx are not
installed (the bare CI shell) — it runs in the ai-engine container and any venv
with the deps. The dynamic fetch (render_official_docx over the SIAP DB +
Beta storage) is verified live against Beta post-deploy.
"""
from __future__ import annotations

import io
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ensure_stub(name: str, attrs: dict | None = None) -> None:
    """Install a thin stub module ONLY when the real one is absent (bare env).
    Never overwrites a real module already imported — so a sibling suite that
    needs the real services.siap_tools (which pulls asyncpg/dotenv) is not
    poisoned. We stub only the leaf infra deps, never a services.* module."""
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


# render_sk_docx lazily imports services.siap_tools, which imports services.
# siap_db → asyncpg + dotenv. Stub those leaf infra deps so the SK-render tests
# can drive the (patched) siap_get_output_template without a live DB driver.
_ensure_stub("asyncpg")
_ensure_stub("dotenv", {"load_dotenv": lambda *a, **k: None})

try:
    import docx  # noqa: F401  (fill/build need it; lazy inside the module)
    from docx import Document
    from services import siap_templates as st  # imports httpx at top
    _DEPS_OK = True
except Exception:  # pragma: no cover — bare env without python-docx/httpx
    _DEPS_OK = False

_FIELDS = {
    "applicant_name": "CASMO",
    "nik": "3374012345678901",
    "phone": "628123456789",
    "alamat": "Jl. Laut No. 1, Tegal",
}


@unittest.skipUnless(_DEPS_OK, "python-docx/httpx not installed")
class TestLabelMatcher(unittest.TestCase):
    def test_identity_labels_match(self):
        self.assertEqual(st._match_identity_field("Nama"), "applicant_name")
        self.assertEqual(st._match_identity_field("NIK"), "nik")
        self.assertEqual(st._match_identity_field("Alamat"), "alamat")
        self.assertEqual(st._match_identity_field("Jabatan"), "jabatan")
        self.assertEqual(st._match_identity_field("No HP"), "phone")
        self.assertEqual(st._match_identity_field("Nomer HP"), "phone")

    def test_whole_label_only_no_substring_bleed(self):
        # These must NOT be treated as identity fields (would misfill).
        self.assertIsNone(st._match_identity_field("Nama Kapal"))
        self.assertIsNone(st._match_identity_field("Nama lembaga/institusi"))
        self.assertIsNone(st._match_identity_field("Nama Penyedia/Galangan"))
        self.assertIsNone(st._match_identity_field("Jabatan dalam lembaga/institusi"))


@unittest.skipUnless(_DEPS_OK, "python-docx/httpx not installed")
class TestSuratPermohonanBuilder(unittest.TestCase):
    def _doc(self):
        return Document(io.BytesIO(st.build_surat_permohonan_ppkp_docx(_FIELDS)))

    def test_correct_recipient_not_dpmptsp(self):
        paras = "\n".join(p.text for p in self._doc().paragraphs)
        self.assertIn("Dinas Kelautan dan Perikanan Provinsi Jawa Tengah", paras)
        self.assertNotIn("DPMPTSP", paras)
        self.assertIn("Coret Yang Tidak Perlu", paras)

    def test_identity_filled_vessel_blank(self):
        doc = self._doc()
        rows = {t.rows[i].cells[0].text: t.rows[i].cells[2].text
                for t in doc.tables for i in range(len(t.rows))}
        self.assertEqual(rows.get("Nama"), "CASMO")
        self.assertEqual(rows.get("NIK"), "3374012345678901")
        self.assertEqual(rows.get("Nomer HP"), "628123456789")
        self.assertEqual(rows.get("Alamat"), "Jl. Laut No. 1, Tegal")
        self.assertEqual(rows.get("Jabatan"), "Pemilik Kapal")
        for vessel in ("Nama Kapal", "Range GT", "Bahan Kapal",
                       "Alat Penangkap Ikan", "Nama Tukang dan Alamat Galangan"):
            self.assertEqual(rows.get(vessel), "", f"{vessel} must be blank")

    def test_output_is_valid_docx(self):
        data = st.build_surat_permohonan_ppkp_docx(_FIELDS)
        self.assertEqual(data[:2], b"PK")  # DOCX = zip container


@unittest.skipUnless(_DEPS_OK, "python-docx/httpx not installed")
class TestFillDocxIdentity(unittest.TestCase):
    def _template(self, labels):
        d = Document()
        for lbl in labels:
            d.add_paragraph(f"{lbl}\t: ")
        buf = io.BytesIO()
        d.save(buf)
        return buf.getvalue()

    def _lines(self, docx_bytes):
        fd = Document(io.BytesIO(docx_bytes))
        out = {}
        for p in st._iter_block_paragraphs(fd):
            if ":" in p.text:
                k, _, v = p.text.partition(":")
                out[k.strip()] = v.strip()
        return out

    def test_fills_identity_leaves_vessel(self):
        raw = self._template(["Nama", "Alamat", "No HP", "NIK", "Nama Kapal", "Range GT"])
        lines = self._lines(st.fill_docx_identity(raw, _FIELDS))
        self.assertEqual(lines["Nama"], "CASMO")
        self.assertEqual(lines["Alamat"], "Jl. Laut No. 1, Tegal")
        self.assertEqual(lines["No HP"], "628123456789")
        self.assertEqual(lines["NIK"], "3374012345678901")
        self.assertEqual(lines["Nama Kapal"], "")   # vessel untouched
        self.assertEqual(lines["Range GT"], "")

    def test_does_not_overwrite_prefilled_slot(self):
        d = Document()
        d.add_paragraph("Nama\t: Sudah Terisi")
        buf = io.BytesIO(); d.save(buf)
        lines = self._lines(st.fill_docx_identity(buf.getvalue(), _FIELDS))
        self.assertEqual(lines["Nama"], "Sudah Terisi")  # not clobbered

    def test_missing_field_left_blank(self):
        raw = self._template(["Nama", "Alamat"])
        # No alamat in fields → Alamat stays blank; Nama fills.
        lines = self._lines(st.fill_docx_identity(raw, {"applicant_name": "CASMO"}))
        self.assertEqual(lines["Nama"], "CASMO")
        self.assertEqual(lines["Alamat"], "")


@unittest.skipUnless(_DEPS_OK, "python-docx/httpx not installed")
class TestFillDocxPlaceholders(unittest.TestCase):
    """The `[data.KEY]` fill for the OFFICER SK output template — run-aware,
    blank-fills unmapped keys, never leaves a raw token behind."""

    def _para_from_runs(self, doc, run_texts):
        """Build a paragraph whose text is SPLIT across the given runs — this is
        exactly how Word shatters a `[data.KEY]` token across runs, the case a
        naive per-run replace would miss."""
        p = doc.add_paragraph()
        for t in run_texts:
            p.add_run(t)
        return p

    def _template_with_split_token(self):
        """A template where `[data.no_siup]` is split across three runs (like the
        real Surat PKPP), plus an intact `[data.gt]` and a to-be-blanked
        `[data.tgl_siup]`."""
        d = Document()
        # no_siup shattered: "[", "data.no_siup", "]" — plus a trailing token.
        self._para_from_runs(
            d, ["SIUP Nomor : ", "[", "data.no_siup", "]",
                " Tanggal ", "[data.tgl_siup]"],
        )
        # gt intact in a single run.
        self._para_from_runs(d, ["Ukuran GT: ", "[data.gt]"])
        buf = io.BytesIO()
        d.save(buf)
        return buf.getvalue()

    def _text(self, docx_bytes):
        fd = Document(io.BytesIO(docx_bytes))
        return "\n".join(p.text for p in st._iter_block_paragraphs(fd))

    def test_fills_run_split_token(self):
        raw = self._template_with_split_token()
        out = st.fill_docx_placeholders(raw, {"no_siup": "SIUP-123", "gt": "5"})
        text = self._text(out)
        # The shattered token filled from the JOINED paragraph text.
        self.assertIn("SIUP-123", text)
        self.assertIn("5", text)
        # No raw token survives anywhere.
        self.assertNotIn("[data.", text)

    def test_unmapped_and_empty_keys_become_blank_fill(self):
        raw = self._template_with_split_token()
        # tgl_siup is absent; no_siup empty → both become the blank fill line,
        # NOT the raw "[data.KEY]".
        out = st.fill_docx_placeholders(raw, {"no_siup": "", "gt": "5"})
        text = self._text(out)
        self.assertNotIn("[data.", text)
        self.assertIn(st._BLANK_FILL, text)

    def test_multiple_tokens_one_paragraph(self):
        d = Document()
        self._para_from_runs(
            d, ["Kapal ", "[data.nama_kapal]", " bahan ", "[data.bahan]"])
        buf = io.BytesIO(); d.save(buf)
        out = st.fill_docx_placeholders(
            buf.getvalue(), {"nama_kapal": "KM BAHARI", "bahan": "Kayu"})
        text = self._text(out)
        self.assertIn("KM BAHARI", text)
        self.assertIn("Kayu", text)
        self.assertNotIn("[data.", text)

    def test_output_is_valid_docx(self):
        raw = self._template_with_split_token()
        out = st.fill_docx_placeholders(raw, {"gt": "5"})
        self.assertEqual(out[:2], b"PK")

    def test_fill_in_table_cell(self):
        # Tokens inside a table cell are reached by _iter_block_paragraphs too.
        d = Document()
        t = d.add_table(rows=1, cols=1)
        cell = t.rows[0].cells[0]
        cell.paragraphs[0].add_run("Tipe: ")
        cell.paragraphs[0].add_run("[data.tipe]")
        buf = io.BytesIO(); d.save(buf)
        out = st.fill_docx_placeholders(buf.getvalue(), {"tipe": "Fiber"})
        self.assertIn("Fiber", self._text(out))
        self.assertNotIn("[data.", self._text(out))


@unittest.skipUnless(_DEPS_OK, "python-docx/httpx not installed")
class TestBuildSkData(unittest.IsolatedAsyncioTestCase):
    """build_sk_data — identity deterministic, jenis_pengadaan parsed from the
    licence name, vessel fields from a best-effort (here: absent) Vision pass."""

    async def test_identity_and_jenis_pengadaan_pembangunan(self):
        data = await st.build_sk_data(
            applicant_name="CASMO", alamat="Jl. Laut No. 1",
            license_name="Persetujuan Pengadaan Kapal (Pembangunan)",
            documents={},
        )
        self.assertEqual(data["nama_pemohon"], "CASMO")
        self.assertEqual(data["alamat"], "Jl. Laut No. 1")
        self.assertEqual(data["jenis_pengadaan"], "Pembangunan")

    async def test_jenis_pengadaan_modifikasi(self):
        data = await st.build_sk_data(
            applicant_name="A", alamat="B",
            license_name="Izin ... Modifikasi Kapal", documents=None)
        self.assertEqual(data["jenis_pengadaan"], "Modifikasi")

    async def test_jenis_pengadaan_blank_when_absent(self):
        data = await st.build_sk_data(
            applicant_name="A", alamat="B", license_name="Izin Lain", documents=None)
        self.assertEqual(data["jenis_pengadaan"], "")

    async def test_vessel_fields_from_vision(self):
        # One in-session doc with bytes → build_sk_data runs ONE Vision pass and
        # merges the non-empty vessel fields. Stub gemini_vision.
        mod = types.ModuleType("services.gemini_vision")

        async def _extract(*, image_bytes, mime_type, prompt, response_schema):
            return {"nama_kapal": "KM BAHARI", "gt": "5", "bahan": "",
                    "no_siup": "SIUP-9"}

        mod.extract_structured = _extract
        mod.is_configured = lambda: True
        sys.modules["services.gemini_vision"] = mod
        try:
            data = await st.build_sk_data(
                applicant_name="CASMO", alamat="Jl X",
                license_name="Pengadaan Kapal (Pembangunan)",
                documents={"doc-1": {"filename": "permohonan.pdf",
                                     "mime_type": "application/pdf",
                                     "content": b"pdfbytes",
                                     "claimed_type": "Surat_Permohonan"}},
            )
        finally:
            sys.modules.pop("services.gemini_vision", None)
        self.assertEqual(data["nama_kapal"], "KM BAHARI")
        self.assertEqual(data["gt"], "5")
        self.assertEqual(data["no_siup"], "SIUP-9")
        # Empty vessel value not carried; identity still present.
        self.assertNotIn("bahan", data)
        self.assertEqual(data["nama_pemohon"], "CASMO")

    async def test_vision_unconfigured_leaves_vessel_blank(self):
        mod = types.ModuleType("services.gemini_vision")
        mod.extract_structured = None
        mod.is_configured = lambda: False
        sys.modules["services.gemini_vision"] = mod
        try:
            data = await st.build_sk_data(
                applicant_name="CASMO", alamat="Jl X",
                license_name="Pengadaan (Pembangunan)",
                documents={"doc-1": {"content": b"x", "mime_type": "application/pdf"}})
        finally:
            sys.modules.pop("services.gemini_vision", None)
        # No vessel keys added; identity intact.
        self.assertNotIn("nama_kapal", data)
        self.assertEqual(data["nama_pemohon"], "CASMO")


@unittest.skipUnless(_DEPS_OK, "python-docx/httpx not installed")
class TestRenderSkDocx(unittest.IsolatedAsyncioTestCase):
    """render_sk_docx — fetch LICENSE-RECOMMEND template, fill, name the file;
    None on every miss so the caller degrades."""

    def _template_bytes(self):
        d = Document()
        p = d.add_paragraph()
        for t in ["Pemohon: ", "[", "data.nama_pemohon", "]"]:
            p.add_run(t)
        d.add_paragraph("No PPKP: [data.no_ppkp]")
        buf = io.BytesIO(); d.save(buf)
        return buf.getvalue()

    def _patch(self, *, out_template, fetch_bytes):
        """Patch siap_get_output_template (in siap_tools, imported lazily by
        render_sk_docx) and fetch_template_bytes (on the module)."""
        import services.siap_tools as stools

        async def _out(lid):
            return out_template

        async def _fetch(internal):
            return fetch_bytes

        self._old_out = getattr(stools, "siap_get_output_template", None)
        self._old_fetch = st.fetch_template_bytes
        stools.siap_get_output_template = _out
        st.fetch_template_bytes = _fetch

    def _unpatch(self):
        import services.siap_tools as stools
        if self._old_out is not None:
            stools.siap_get_output_template = self._old_out
        st.fetch_template_bytes = self._old_fetch

    async def test_renders_named_docx_from_template(self):
        self._patch(
            out_template={"found": True, "license_id": 459,
                          "internal_filename": "master/ULID.docx"},
            fetch_bytes=self._template_bytes(),
        )
        try:
            res = await st.render_sk_docx(
                459, {"nama_pemohon": "CASMO"}, ticket="000123456")
        finally:
            self._unpatch()
        self.assertIsNotNone(res)
        data, name = res
        self.assertEqual(name, "SK_PPKP_000123456.docx")
        self.assertEqual(data[:2], b"PK")
        text = "\n".join(p.text for p in st._iter_block_paragraphs(
            Document(io.BytesIO(data))))
        self.assertIn("CASMO", text)
        self.assertNotIn("[data.", text)  # no raw token left

    async def test_none_when_no_license_id(self):
        res = await st.render_sk_docx(None, {}, ticket="1")
        self.assertIsNone(res)

    async def test_none_when_template_not_found(self):
        self._patch(out_template={"found": False}, fetch_bytes=None)
        try:
            res = await st.render_sk_docx(459, {}, ticket="1")
        finally:
            self._unpatch()
        self.assertIsNone(res)

    async def test_none_when_template_is_legacy_doc(self):
        self._patch(
            out_template={"found": True, "internal_filename": "master/OLD.doc"},
            fetch_bytes=b"whatever",
        )
        try:
            res = await st.render_sk_docx(459, {}, ticket="1")
        finally:
            self._unpatch()
        self.assertIsNone(res)

    async def test_none_when_fetch_misses(self):
        self._patch(
            out_template={"found": True, "internal_filename": "master/ULID.docx"},
            fetch_bytes=None,   # 404 / not synced
        )
        try:
            res = await st.render_sk_docx(459, {}, ticket="1")
        finally:
            self._unpatch()
        self.assertIsNone(res)


if __name__ == "__main__":
    unittest.main(verbosity=2)
