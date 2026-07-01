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
_fake_dg.generate = lambda dt, data, **kw: b"%PDF-1.4 stub " + str(dt).encode()
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
        # Re-assert the flag each test — another test file toggles it off, and the
        # leak would otherwise make is_enabled() False here (the module-level set
        # runs only once at import).
        os.environ["BIMA_GUIDED_SUBMISSION_ENABLED"] = "true"
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
        self.assertIn("Mekari", reply)                       # digital sign option present
        self.assertIn("Manual", reply)                       # manual sign option present
        self.assertIn("tersendiri", reply)                   # unique-meterai-per-doc guidance
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

    def test_preparing_docs_counts_as_active_session(self):
        # Regression: uploads mid-doc-prep must NOT read as "no active session".
        sess = SubmissionSession(user_id="wa-628444", license_id=459,
                                 license_name="PPKP", stage=Stage.PREPARING_DOCS)
        gs._sessions[sess.user_id] = sess
        self.assertTrue(_run(gs.has_active_session(sess.user_id)))

    def test_upload_during_preparing_docs_switches_to_collecting(self):
        # A citizen who uploads their OWN documents mid doc-prep skips generation
        # and drops into the normal collect+score flow (silent attach, no per-file
        # ack, no "no active session" nudge).
        sess = SubmissionSession(user_id="wa-628333", license_id=459,
                                 license_name="PPKP", stage=Stage.PREPARING_DOCS)
        gs._sessions[sess.user_id] = sess

        async def _noop_arm(uid):
            return None

        orig_arm = gs._arm_debounce
        gs._arm_debounce = _noop_arm
        try:
            doc = gs.SessionDocument("f1", "ktp", "ktp.jpg", "image/jpeg", b"\xff\xd8\xff")
            reply = _run(gs.handle_inbound_documents(sess.user_id, [doc]))
        finally:
            gs._arm_debounce = orig_arm

        self.assertIsNone(reply)                             # silent attach, no per-file ack
        self.assertEqual(sess.stage, Stage.COLLECTING_DOCS)  # switched out of doc-prep
        self.assertEqual(len(sess.documents), 1)             # the upload attached


class TestSwitchKeywords(unittest.TestCase):
    """The mid-form switch offer tells the citizen to reply "ganti"/"lanjutkan";
    those literal words must resolve, not loop the disambiguation forever."""

    def setUp(self):
        os.environ["BIMA_GUIDED_SUBMISSION_ENABLED"] = "true"
        self._orig_start = gs._start_session
        self._orig_confirm = gs._handle_confirm
        self.started = []

        async def fake_start(uid, text):
            self.started.append((uid, text))
            return "STARTED"

        async def fake_confirm(sess, msg):
            return "CONFIRM_HANDLED"

        gs._start_session = fake_start
        gs._handle_confirm = fake_confirm

    def tearDown(self):
        gs._start_session = self._orig_start
        gs._handle_confirm = self._orig_confirm
        gs._sessions.clear()

    def _sess_with_pending(self, uid):
        s = SubmissionSession(user_id=uid, license_id=459, license_name="PPKP",
                              stage=Stage.CONFIRM)
        s.pending_new_intent = "saya mau ajukan izin lain"
        gs._sessions[uid] = s
        return s

    def test_ganti_switches(self):
        self._sess_with_pending("wa-777")
        reply = _run(gs.maybe_handle("wa-777", "ganti"))
        self.assertEqual(reply, "STARTED")          # switched to the stashed intent
        self.assertEqual(len(self.started), 1)

    def test_ya_ganti_switches(self):
        self._sess_with_pending("wa-779")
        _run(gs.maybe_handle("wa-779", "ya, ganti"))
        self.assertEqual(len(self.started), 1)       # BIMA's own suggested phrase works

    def test_lanjutkan_continues_no_switch(self):
        sess = self._sess_with_pending("wa-778")
        reply = _run(gs.maybe_handle("wa-778", "lanjutkan"))
        self.assertEqual(self.started, [])           # did NOT switch
        self.assertIsNone(sess.pending_new_intent)   # stash cleared
        self.assertEqual(reply, "CONFIRM_HANDLED")   # fell through to the stage handler


if __name__ == "__main__":
    unittest.main(verbosity=2)
