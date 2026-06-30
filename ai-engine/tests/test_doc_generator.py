"""Doc-generator + PPKP guide (PPKP guided-submission, Phase 1).

Each generated document must be a valid, non-trivial PDF, must never crash on
missing data or exotic characters, and must reject unknown types. fpdf2 is a
real dependency — skip if it isn't installed (it ships in the container).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from services import doc_generator as dg
    _HAS = True
except Exception:  # noqa: BLE001
    _HAS = False


@unittest.skipUnless(_HAS, "fpdf2 not installed")
class TestDocGenerator(unittest.TestCase):
    DATA = {
        "applicant_name": "BUDI SANTOSO", "nik": "3301234567890001",
        "jabatan": "Direktur", "business_name": "PT Mina Bahari Jaya",
        "alamat": "Jl. Pelabuhan No. 10, Tegal",
        "license_name": "Persetujuan Pengadaan Kapal Perikanan (PKPP) - Pembangunan",
        "nama_kapal": "KM Mina Jaya 01", "gt_kapal": "120 GT",
        "bahan_kapal": "Baja", "galangan": "PT Galangan Nusantara",
    }

    def test_each_doc_is_a_valid_pdf(self):
        for dt in dg.DOC_TYPES:
            pdf = dg.generate(dt, self.DATA)
            self.assertEqual(pdf[:5], b"%PDF-", dt)
            self.assertGreater(len(pdf), 1000, dt)

    def test_missing_data_never_crashes(self):
        pdf = dg.generate(dg.PAKTA_INTEGRITAS, {})
        self.assertEqual(pdf[:5], b"%PDF-")

    def test_non_latin1_data_is_sanitized(self):
        pdf = dg.generate(dg.PAKTA_INTEGRITAS, {"applicant_name": "Budi — O’Hara “X”"})
        self.assertEqual(pdf[:5], b"%PDF-")

    def test_unknown_type_raises(self):
        with self.assertRaises(KeyError):
            dg.generate("nope", self.DATA)


@unittest.skipUnless(_HAS, "fpdf2 not installed")
class TestPpkpGuide(unittest.TestCase):
    def test_ppkp_guide_shape(self):
        from services import license_guides as lg
        g = lg.get_guide(459)
        self.assertIsNotNone(g)
        self.assertEqual(len(g["requirements"]), 8)
        self.assertEqual(len(g["generate_docs"]), 3)
        self.assertEqual(len(g["upload_docs"]), 5)
        for r in g["generate_docs"]:
            self.assertIn(r["doc_type"], dg.DOC_TYPES)

    def test_uncurated_license_returns_none(self):
        from services import license_guides as lg
        self.assertIsNone(lg.get_guide(999999))


if __name__ == "__main__":
    unittest.main(verbosity=2)
