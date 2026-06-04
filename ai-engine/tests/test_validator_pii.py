"""PII masking on the admin-api suitability endpoint (routers/validator.py).

The /suitability endpoint serializes a raw SuitabilityResult into output models
consumed by the bima-admin dashboard. The Gemini-Vision `evidence` quote (and
the `Issue.detail` that embeds it as "Bukti: {evidence} (kepercayaan ..%)") can
carry a 16-digit KTP NIK or a phone number — the exact leak seen in the WhatsApp
score message, on a THIRD surface. This test pins that those document-derived
free-text fields are run through services.pii.mask_pii at the serialization
sites, so the dashboard JSON never exposes a raw NIK/phone.

The test suite stubs fastapi (routers import a heavy chain), so instead of
spinning up the route we assert two things that together guarantee the masked
endpoint output:
  (1) the exact transform the endpoint applies — mask_pii on a realistic KTP
      evidence quote and on the "Bukti: {evidence}" issue-detail wrapper —
      removes the NIK/phone while preserving the surrounding non-PII context;
  (2) routers/validator.py wires mask_pii at all three serialization sites
      (evidence + the two SuitabilityIssueOut detail lists) and leaves no raw
      `f.evidence` / `i.detail` behind.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.pii import mask_pii  # noqa: E402  (light, no heavy deps)

# A 16-digit KTP NIK + a realistic Vision evidence quote (the live-incident shape).
_NIK = "3327080511740081"
_EVIDENCE_WITH_NIK = f"PROVINSI JAWA TENGAH KABUPATEN PEMALANG NIK : {_NIK} Nama: CASMO"
# Issue.detail is built in suitability_judge._build_issues() as:
#   f"Bukti: {f.evidence} (kepercayaan {confidence}%)"
_DETAIL_WITH_NIK = f"Bukti: {_EVIDENCE_WITH_NIK} (kepercayaan 85%)"
_PHONE = "085602237412"
_EVIDENCE_WITH_PHONE = f"Hubungi pemilik kapal di {_PHONE} untuk verifikasi"
_EVIDENCE_CLEAN = "Alamat usaha tertera jelas dan sesuai domisili pemohon"


class TestSuitabilityEvidenceTransform(unittest.TestCase):
    """The transform the endpoint applies to document-derived free text."""

    def test_nik_masked_in_evidence(self):
        out = mask_pii(_EVIDENCE_WITH_NIK)
        self.assertNotIn(_NIK, out)                  # full 16-digit NIK is gone
        self.assertTrue(out.startswith("PROVINSI JAWA TENGAH"))
        self.assertIn("KABUPATEN PEMALANG", out)     # non-PII context preserved
        self.assertIn("33", out)                     # masked head (first 2)
        self.assertIn("81", out)                     # masked tail (last 2)

    def test_nik_masked_inside_issue_detail_wrapper(self):
        out = mask_pii(_DETAIL_WITH_NIK)
        self.assertNotIn(_NIK, out)                  # NIK gone even inside "Bukti: ..."
        self.assertIn("Bukti:", out)                 # wrapper preserved
        self.assertIn("kepercayaan 85%", out)        # confidence note preserved

    def test_phone_masked_in_evidence(self):
        out = mask_pii(_EVIDENCE_WITH_PHONE)
        self.assertNotIn(_PHONE, out)                # full phone gone
        self.assertIn("verifikasi", out)             # context preserved

    def test_clean_evidence_unchanged(self):
        self.assertEqual(mask_pii(_EVIDENCE_CLEAN), _EVIDENCE_CLEAN)

    def test_short_ids_and_idempotent(self):
        # a 9-digit ticket and an already-masked value must be left alone
        self.assertEqual(mask_pii("Tiket 000000352 disetujui"), "Tiket 000000352 disetujui")
        self.assertEqual(mask_pii(mask_pii(_EVIDENCE_WITH_NIK)), mask_pii(_EVIDENCE_WITH_NIK))


class TestValidatorRouterWiresMaskPii(unittest.TestCase):
    """routers/validator.py must run the three document-derived free-text
    fields through mask_pii at serialization time (source-level guard)."""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "routers" / "validator.py").read_text(encoding="utf-8")

    def test_imports_mask_pii(self):
        self.assertIn("from services.pii import mask_pii", self.src)

    def test_evidence_site_wrapped(self):
        self.assertIn("evidence=mask_pii(f.evidence)", self.src)

    def test_both_detail_sites_wrapped(self):
        # compatibility_findings + issues -> two SuitabilityIssueOut blocks
        self.assertEqual(self.src.count("detail=mask_pii(i.detail)"), 2)

    def test_no_raw_evidence_or_detail_serialized(self):
        self.assertNotIn("evidence=f.evidence,", self.src)
        self.assertNotIn("detail=i.detail,", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
