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
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
