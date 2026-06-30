"""PPKP doc-prep flow (Stage.PREPARING_DOCS).

A curated licence routes through the doc-prep co-pilot: explain requirements,
collect the data the generated docs need, draft + deliver the 3 documents, hand
off to Mekari, then rejoin COLLECTING_DOCS. Stubs the PDF generator (so no
fpdf2 here — real generation is covered by test_doc_generator), the LLM field
extractor, and the channel send.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Inject a stub doc_generator so this flow test needs no fpdf2. setdefault keeps
# a real import (e.g. under the fpdf2-equipped venv) intact — generation still
# works there, the flow assertions hold either way.
_fake_dg = types.ModuleType("services.doc_generator")
_fake_dg.generate = lambda dt, data: b"%PDF-1.4 stub " + str(dt).encode()
_fake_dg.DOC_TYPES = {
    "pakta_integritas": "Pakta Integritas",
    "surat_permohonan": "Surat Permohonan",
    "surat_pesanan": "Surat Pesanan",
}
_fake_dg.PAKTA_INTEGRITAS = "pakta_integritas"
_fake_dg.SURAT_PERMOHONAN = "surat_permohonan"
_fake_dg.SURAT_PESANAN = "surat_pesanan"
sys.modules.setdefault("services.doc_generator", _fake_dg)

os.environ["BIMA_GUIDED_SUBMISSION_ENABLED"] = "true"


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


_ensure_stub("asyncpg")
_ensure_stub("dotenv", {"load_dotenv": lambda *a, **k: None})

from services import guided_submission as gs  # noqa: E402
from services import license_guides  # noqa: E402
from services.guided_submission import Stage, SubmissionSession  # noqa: E402

_DATA = {
    "applicant_name": "BUDI SANTOSO", "nik": "3301234567890001",
    "jabatan": "Direktur", "business_name": "PT Mina Bahari Jaya",
    "alamat": "Jl. Pelabuhan No. 10, Tegal", "nama_kapal": "KM Mina Jaya 01",
    "galangan": "PT Galangan Nusantara", "gt_kapal": "120 GT", "bahan_kapal": "Baja",
}


def _run(coro):
    return asyncio.run(coro)


class TestDocPrepFlow(unittest.TestCase):
    def setUp(self):
        self.sent: list = []

        async def fake_send(user_id, link, filename, *, caption=""):
            self.sent.append((filename, link, caption))
            return True

        async def fake_extract(text, sess):
            return dict(_DATA)

        self._orig_send = gs._send_doc_to_user
        self._orig_extract = gs._extract_doc_fields
        gs._send_doc_to_user = fake_send
        gs._extract_doc_fields = fake_extract

    def tearDown(self):
        gs._send_doc_to_user = self._orig_send
        gs._extract_doc_fields = self._orig_extract
        gs._sessions.clear()

    def test_intro_lists_generate_and_upload(self):
        sess = SubmissionSession(user_id="wa-628111", license_id=459,
                                 license_name="PPKP - Pembangunan")
        intro = gs._fmt_doc_prep_intro(sess, license_guides.get_guide(459))
        self.assertIn("Pakta Integritas", intro)
        self.assertIn("unggah", intro.lower())

    def test_complete_data_generates_and_advances(self):
        sess = SubmissionSession(user_id="wa-628111", license_id=459,
                                 license_name="PPKP - Pembangunan",
                                 stage=Stage.PREPARING_DOCS)
        gs._sessions[sess.user_id] = sess
        reply = _run(gs._handle_preparing_docs(sess, "ini semua datanya"))
        self.assertEqual(len(self.sent), 3)                  # 3 drafts delivered
        self.assertEqual(sess.stage, Stage.COLLECTING_DOCS)  # rejoined upload flow
        self.assertIn("Mekari", reply)                       # sign hand-off present
        self.assertEqual(sess.fields["applicant_name"], "BUDI SANTOSO")  # seeded for KTP step

    def test_missing_data_asks_not_generates(self):
        async def empty_extract(text, sess):
            return {}

        gs._extract_doc_fields = empty_extract
        sess = SubmissionSession(user_id="wa-628222", license_id=459,
                                 license_name="PPKP", stage=Stage.PREPARING_DOCS)
        gs._sessions[sess.user_id] = sess
        _run(gs._handle_preparing_docs(sess, "halo"))
        self.assertEqual(len(self.sent), 0)
        self.assertEqual(sess.stage, Stage.PREPARING_DOCS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
