"""SEC-001 regression: unauthenticated SIAP ticket lookups must NOT leak the
full applicant name.

SIAP tickets are sequential, zero-padded 9-digit numbers, so anyone can
enumerate them over the public WhatsApp line (or the public portal /track page)
and harvest real names at scale. The status reply now masks the applicant name
to first-name + last-initial — recognisable to the owner, useless to a scraper.

Run:
    python -m tests.test_siap_status_masking
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# siap_client imports httpx + dotenv (and calls load_dotenv()) at module load;
# stub them so this pure-logic test runs without the heavy/optional deps,
# mirroring the sibling tests' "stub before import" pattern.
sys.modules.setdefault("httpx", types.ModuleType("httpx"))
_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *a, **k: None  # type: ignore[attr-defined]
sys.modules.setdefault("dotenv", _dotenv)

from services.siap_client import _mask_name, format_status_reply  # noqa: E402


class TestMaskName(unittest.TestCase):
    def test_full_name_masked_to_first_plus_initial(self):
        self.assertEqual(_mask_name("FAJAR JOKO WASKITO"), "Fajar W.")

    def test_two_part_name(self):
        self.assertEqual(_mask_name("SITI AMINAH"), "Siti A.")

    def test_single_name_kept(self):
        self.assertEqual(_mask_name("BUDI"), "Budi")

    def test_empty_and_blank(self):
        self.assertEqual(_mask_name(""), "")
        self.assertEqual(_mask_name("   "), "")


class TestStatusReplyHasNoFullNameLeak(unittest.TestCase):
    def _record(self):
        return {
            "no_tiket": "000077591",
            "nama_pemohon": "FAJAR JOKO WASKITO",
            "nama_perizinan": "Izin Usaha Industri",
            "nama_bidang": "Perdagangan",
            "posisi_berkas": "Front Office PTSP",
            "status_permohonan": "VERIFIKASI",
            "tanggal_permohonan": "2026-01-02",
        }

    def test_reply_masks_name_keeps_status(self):
        out = format_status_reply(self._record())
        # Masked name present; full name and surname/middle gone.
        self.assertIn("Fajar W.", out)
        self.assertNotIn("Joko", out)
        self.assertNotIn("Waskito", out)
        self.assertNotIn("FAJAR JOKO WASKITO", out)
        # Non-PII status info still rendered (the useful part).
        self.assertIn("VERIFIKASI", out)
        self.assertIn("000077591", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
