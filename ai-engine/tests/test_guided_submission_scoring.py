"""
Tests for the scoring + field-extraction + submit internals of the REDESIGNED
guided-submission flow (`services/guided_submission.py`).

Run standalone (no pytest):

    python3 tests/test_guided_submission_scoring.py

Covers the helper surface the new flow exposes:
  * attach_documents / _attach_documents — attach in-session bytes, cap count,
    no-op on a terminal/missing session, stamp last_doc_at.
  * _load_demo_packet — reads a directory of files, derives claimed types.
  * _run_content_score — calls the citizen scorer (mocked), normalises the
    result; None when no in-session docs.
  * _extract_fields_from_documents — KTP Vision (mocked) → name + NIK; phone
    from the msisdn; business name from a NIB filename hint.
  * _process_collected_documents — the consolidated pass: score + extract →
    CONFIRM (or ask for a missing field, staying at COLLECTING_DOCS).
  * _submit officer hand-off — on a successful SIAP create the officer bridge
    is invoked with the score + documents; request_id recovered from ticket.
  * _submit re-score after a Redis rehydrate dropped the rich `result`.
  * the CONFIRM-stage score reminder on a non-AFFIRM clarifying turn.

No real Gemini, no real DB, no real SIAP.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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
from services.agents.suitability_judge import (  # noqa: E402
    CompletenessSection,
    Issue,
    SuitabilityResult,
)


def _run(coro):
    return asyncio.run(coro)


def _suitability_result(percent=90, critical=False, blocking=False):
    """Build a SuitabilityResult fixture.

    critical → a critical issue whose id is NOT a blocking prefix (a soft
               critical; won't trip the hard gate).
    blocking → a missing-mandatory-doc issue whose id IS a blocking prefix
               (a real HARD block: no submit offer, non-overridable).
    """
    issues = []
    missing = []
    required = ["KTP"]
    if critical:
        issues.append(Issue(id="c", severity="critical", title="Dokumen wajib hilang", detail="d"))
    if blocking:
        required = ["KTP", "NPWP"]
        missing = ["NPWP"]
        issues.append(Issue(
            id="completeness:missing:NPWP", severity="critical",
            title="Dokumen wajib belum diunggah: NPWP", detail="Mohon lengkapi.",
        ))
    return SuitabilityResult(
        completeness=CompletenessSection(score=1.0 if not missing else 0.5,
                                         missing=missing, required=required),
        type_correctness=[],
        suitability=[],
        compatibility_findings=[],
        overall_suitability_score=percent / 100.0,
        issues=issues,
    )


_KTP = {"is_ktp": True, "name": "Budi Santoso", "nik": "3374012345678901", "gender": "LAKI-LAKI"}


def _make_session(stage=gs.Stage.COLLECTING_DOCS, *, with_fields=True):
    sess = gs.SubmissionSession(user_id="wa-628123")
    sess.license_id = 358
    sess.license_name = "Izin Penelitian"
    sess.stage = stage
    if with_fields:
        sess.fields = {
            "applicant_name": "Budi",
            "nik": "3374012345678901",
            "business_name": "CV Riset",
            "phone": "628123",
        }
    return sess


class TestAttachDocuments(unittest.TestCase):
    def setUp(self):
        gs._sessions.clear()

    def test_attaches_to_active_session_and_stamps_last_doc_at(self):
        sess = _make_session(gs.Stage.COLLECTING_DOCS)
        _run(gs._put_session(sess))
        ok = _run(gs.attach_documents(sess.user_id, [
            gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x"),
        ]))
        self.assertTrue(ok)
        self.assertEqual(len(gs._sessions[sess.user_id].documents), 1)
        self.assertGreater(gs._sessions[sess.user_id].last_doc_at, 0.0)

    def test_noop_when_no_session(self):
        self.assertFalse(_run(gs.attach_documents("wa-nobody", [
            gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x"),
        ])))

    def test_dedup_and_cap(self):
        sess = _make_session(gs.Stage.COLLECTING_DOCS)
        _run(gs._put_session(sess))
        docs = [gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")] * 2
        _run(gs.attach_documents(sess.user_id, docs))
        self.assertEqual(len(gs._sessions[sess.user_id].documents), 1)


class TestDemoPacketLoader(unittest.TestCase):
    def setUp(self):
        gs._sessions.clear()

    def test_loads_files_with_claimed_types(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "surat_permohonan_materai.pdf").write_bytes(b"%PDF-1.4 fake")
            (Path(d) / "ktp.jpg").write_bytes(b"\xff\xd8\xff fake")
            (Path(d) / ".hidden").write_bytes(b"skip me")
            sess = _make_session()
            with patch.dict(os.environ, {"GUIDED_SUBMISSION_DEMO_PACKET": d}, clear=False):
                n = gs._load_demo_packet(sess)
            self.assertEqual(n, 2)
            claimed = {doc.claimed_type for doc in sess.documents}
            self.assertIn("surat permohonan materai", claimed)
            self.assertIn("ktp", claimed)

    def test_no_packet_env_returns_zero(self):
        sess = _make_session()
        with patch.dict(os.environ, {"GUIDED_SUBMISSION_DEMO_PACKET": ""}, clear=False):
            self.assertEqual(gs._load_demo_packet(sess), 0)


class TestContentScore(unittest.TestCase):
    def setUp(self):
        gs._sessions.clear()

    def test_none_when_no_documents(self):
        sess = _make_session()
        self.assertIsNone(_run(gs._run_content_score(sess)))

    def test_scores_in_session_docs(self):
        sess = _make_session()
        sess.documents = [gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")]
        fake = _suitability_result(percent=88)
        with patch("services.citizen_scorer.score_session_documents",
                   new=AsyncMock(return_value=fake)):
            out = _run(gs._run_content_score(sess))
        self.assertIsNotNone(out)
        self.assertEqual(out["score_percent"], 88)
        self.assertTrue(out["ok"])               # >=85 + no critical
        self.assertIn("message", out)
        self.assertIs(out["result"], fake)

    def test_critical_issue_blocks_ok(self):
        sess = _make_session()
        sess.documents = [gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")]
        fake = _suitability_result(percent=95, critical=True)
        with patch("services.citizen_scorer.score_session_documents",
                   new=AsyncMock(return_value=fake)):
            out = _run(gs._run_content_score(sess))
        self.assertFalse(out["ok"])


class TestFieldExtraction(unittest.TestCase):
    """Fields come from the documents + msisdn, not from Q&A."""

    def setUp(self):
        gs._sessions.clear()

    def test_ktp_populates_name_and_nik(self):
        sess = _make_session(with_fields=False)
        sess.documents = [gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"\xff\xd8\xff")]
        with patch("services.gemini_vision.extract_ktp_fields",
                   new=AsyncMock(return_value=_KTP)), \
             patch("services.gemini_vision.is_configured", return_value=True):
            _run(gs._extract_fields_from_documents(sess))
        self.assertEqual(sess.fields.get("applicant_name"), "Budi Santoso")
        self.assertEqual(sess.fields.get("nik"), "3374012345678901")
        # Phone always comes from the msisdn ('wa-628123').
        self.assertEqual(sess.fields.get("phone"), "628123")

    def test_phone_from_msisdn_without_documents(self):
        sess = _make_session(with_fields=False)
        with patch("services.gemini_vision.is_configured", return_value=False):
            _run(gs._extract_fields_from_documents(sess))
        self.assertEqual(sess.fields.get("phone"), "628123")
        # No KTP read (vision off) → name/NIK remain missing → required gap.
        self.assertEqual(sess.missing_required_fields(), ["applicant_name", "nik"])

    def test_business_name_none_when_nib_read_fails(self):
        # A filename is NOT a business name. When the NIB read fails, business_name
        # stays unset and falls back to the person's name at render — correct for a
        # Usaha Perorangan.
        sess = _make_session(with_fields=False)
        sess.documents = [
            gs.SessionDocument("d1", "ktp", "ktp.jpg", "image/jpeg", b"\xff\xd8\xff"),
            gs.SessionDocument("d2", "nib cv maju jaya", "nib_cv_maju_jaya.pdf",
                               "application/pdf", b"%PDF"),
        ]
        with patch("services.gemini_vision.extract_ktp_fields",
                   new=AsyncMock(return_value=_KTP)), \
             patch("services.gemini_vision.extract_business_fields",
                   new=AsyncMock(return_value=None)), \
             patch("services.gemini_vision.is_configured", return_value=True):
            _run(gs._extract_fields_from_documents(sess))
        self.assertIsNone(sess.fields.get("business_name"))

    def test_business_name_from_nib_extraction(self):
        # A Badan: nama usaha + jenis come from the NIB/SIUP via Vision, not a filename.
        sess = _make_session(with_fields=False)
        sess.documents = [
            gs.SessionDocument("d1", "ktp", "ktp.jpg", "image/jpeg", b"\xff\xd8\xff"),
            gs.SessionDocument("d2", "siup", "siup_scan.pdf", "application/pdf", b"%PDF"),
        ]
        biz = {"is_nib": True, "nama_usaha": "CV Maju Jaya", "nib": "1234", "jenis_usaha": "Badan"}
        with patch("services.gemini_vision.extract_ktp_fields",
                   new=AsyncMock(return_value=_KTP)), \
             patch("services.gemini_vision.extract_business_fields",
                   new=AsyncMock(return_value=biz)), \
             patch("services.gemini_vision.is_configured", return_value=True):
            _run(gs._extract_fields_from_documents(sess))
        self.assertEqual(sess.fields.get("business_name"), "CV Maju Jaya")
        self.assertEqual(sess.fields.get("jenis_usaha"), "Badan")

    def test_vision_failure_leaves_field_missing_not_raises(self):
        sess = _make_session(with_fields=False)
        sess.documents = [gs.SessionDocument("d1", "ktp", "ktp.jpg", "image/jpeg", b"\xff\xd8\xff")]
        with patch("services.gemini_vision.extract_ktp_fields",
                   new=AsyncMock(side_effect=RuntimeError("vision boom"))), \
             patch("services.gemini_vision.is_configured", return_value=True):
            _run(gs._extract_fields_from_documents(sess))   # must NOT raise
        self.assertIsNone(sess.fields.get("applicant_name"))
        self.assertIsNone(sess.fields.get("nik"))


class TestProcessCollectedDocuments(unittest.TestCase):
    """The consolidated pass: score + extract → CONFIRM, or ask for a gap."""

    def setUp(self):
        gs._sessions.clear()

    def test_complete_packet_moves_to_confirm_with_masked_nik(self):
        sess = _make_session(gs.Stage.COLLECTING_DOCS, with_fields=False)
        sess.documents = [gs.SessionDocument("d1", "ktp", "ktp.jpg", "image/jpeg", b"\xff\xd8\xff")]
        _run(gs._put_session(sess))
        with patch("services.citizen_scorer.score_session_documents",
                   new=AsyncMock(return_value=_suitability_result(91))), \
             patch("services.gemini_vision.extract_ktp_fields",
                   new=AsyncMock(return_value=_KTP)), \
             patch("services.gemini_vision.is_configured", return_value=True):
            reply = _run(gs._process_collected_documents(sess))
        self.assertEqual(sess.stage, gs.Stage.CONFIRM)
        self.assertIsNotNone(sess.last_score)
        self.assertIn("91%", reply)
        self.assertIn("Budi Santoso", reply)
        self.assertNotIn("3374012345678901", reply)   # NIK masked in the read-back
        self.assertIn("yakin", reply.lower())

    def test_missing_ktp_asks_for_field_stays_collecting(self):
        sess = _make_session(gs.Stage.COLLECTING_DOCS, with_fields=False)
        sess.documents = [gs.SessionDocument("d1", "foto", "foto.jpg", "image/jpeg", b"\xff\xd8\xff")]
        _run(gs._put_session(sess))
        no_ktp = {"is_ktp": False, "name": None, "nik": None, "gender": None}
        with patch("services.citizen_scorer.score_session_documents",
                   new=AsyncMock(return_value=_suitability_result(60))), \
             patch("services.gemini_vision.extract_ktp_fields",
                   new=AsyncMock(return_value=no_ktp)), \
             patch("services.gemini_vision.is_configured", return_value=True):
            reply = _run(gs._process_collected_documents(sess))
        self.assertEqual(sess.stage, gs.Stage.COLLECTING_DOCS)
        self.assertIn("ktp", reply.lower())

    def test_no_documents_nudges(self):
        sess = _make_session(gs.Stage.COLLECTING_DOCS, with_fields=False)
        _run(gs._put_session(sess))
        with patch.dict(os.environ, {"GUIDED_SUBMISSION_DEMO_PACKET": ""}, clear=False):
            reply = _run(gs._process_collected_documents(sess))
        self.assertEqual(sess.stage, gs.Stage.COLLECTING_DOCS)
        self.assertIn("belum menerima", reply.lower())


class TestHardGate(unittest.TestCase):
    """BIMA-as-verificator: a BLOCKING issue (missing mandatory doc / wrong
    type / sign-doc missing meterai-ttd-stamp) must BLOCK submission — no
    'ajukan sekarang?' offer, non-overridable, stays COLLECTING_DOCS."""

    def setUp(self):
        gs._sessions.clear()

    def _sla_session(self):
        sess = _make_session(gs.Stage.COLLECTING_DOCS, with_fields=False)
        sess.license_id = 459
        sess.license_name = "PPKP"
        sess.sla_working_days = 7
        sess.documents = [gs.SessionDocument("d1", "ktp", "ktp.jpg", "image/jpeg", b"\xff\xd8\xff")]
        return sess

    def test_missing_mandatory_doc_blocks_and_guides_no_offer(self):
        sess = self._sla_session()
        _run(gs._put_session(sess))
        blocked = _suitability_result(60, blocking=True)
        with patch("services.citizen_scorer.score_session_documents",
                   new=AsyncMock(return_value=blocked)), \
             patch("services.gemini_vision.extract_ktp_fields",
                   new=AsyncMock(return_value=_KTP)), \
             patch("services.gemini_vision.is_configured", return_value=True):
            reply = _run(gs._process_collected_documents(sess))
        # Stays collecting — NOT confirm.
        self.assertEqual(sess.stage, gs.Stage.COLLECTING_DOCS)
        # No submit offer of any kind.
        self.assertNotIn("ajukan sekarang", reply.lower())
        self.assertNotIn("apa adanya", reply.lower())
        # Lists what's wrong + how to fix + the closing "complete then resend".
        self.assertIn("NPWP", reply)
        self.assertIn("Silakan lengkapi lalu kirim lagi ya.", reply)
        # Officer grounding — the SLA is surfaced.
        self.assertIn("7 hari kerja", reply)

    def test_clean_packet_offers_submission(self):
        sess = self._sla_session()
        _run(gs._put_session(sess))
        clean = _suitability_result(90)  # 1/1 complete, no issues
        with patch("services.citizen_scorer.score_session_documents",
                   new=AsyncMock(return_value=clean)), \
             patch("services.gemini_vision.extract_ktp_fields",
                   new=AsyncMock(return_value=_KTP)), \
             patch("services.gemini_vision.is_configured", return_value=True):
            reply = _run(gs._process_collected_documents(sess))
        self.assertEqual(sess.stage, gs.Stage.CONFIRM)
        self.assertIn("ajukan sekarang", reply.lower())
        self.assertIn("7 hari kerja", reply)  # officer grounding on confirm too

    def test_submit_never_overrides_blocking(self):
        # The gate has NO override: an AFFIRM on a blocking packet must not
        # submit, however firmly it is given. The client is never reached.
        sess = _make_session(gs.Stage.CONFIRM)
        sess.license_id = 459
        sess.license_name = "PPKP"
        sess.sla_working_days = 7
        sess.documents = [gs.SessionDocument("d1", "ktp", "ktp.jpg", "image/jpeg", b"x")]
        sess.last_score = {
            "ok": False, "status": "needs_fix", "score_percent": 60,
            "summary": "s", "message": "m", "issues": [],
            "result": _suitability_result(60, blocking=True),
        }
        _run(gs._put_session(sess))

        client_probe = {"configured": 0}

        def _is_configured():
            client_probe["configured"] += 1
            return True

        fake_client = types.SimpleNamespace(
            is_configured=_is_configured,
            create_request=AsyncMock(return_value={"ok": True, "ticket": "X"}),
        )
        with patch.dict(os.environ, {"GUIDED_SUBMISSION_PROFILE_ID": "12345"}, clear=False):
            with patch("services.siap_submission_client.get_siap_submission_client",
                       return_value=fake_client):
                reply = _run(gs._submit(sess))
        # Blocked before any SIAP call — client never consulted.
        self.assertEqual(client_probe["configured"], 0)
        fake_client.create_request.assert_not_awaited()
        # Dropped back to collecting with guidance naming the missing doc.
        self.assertEqual(sess.stage, gs.Stage.COLLECTING_DOCS)
        self.assertIn("NPWP", reply)
        self.assertIn("Silakan lengkapi lalu kirim lagi ya.", reply)
        # NPWP is still missing, so the escape must NOT be advertised: there is
        # nothing for a human to arbitrate until the citizen has actually
        # finished uploading. (It still WORKS if they ask — see the ESCALATE
        # tests — it is just not dangled in front of an unfinished upload.)
        self.assertNotIn("minta tinjau petugas", reply.lower())

    def test_sub_threshold_is_hard_blocked_with_no_override(self):
        # A sub-threshold packet is BLOCKED even when every mandatory document
        # is present+valid. The "ajukan apa adanya" bypass is gone: the only
        # exits are fix it, or ask a human. Guards the 84%-document thesis —
        # a weak packet must not reach SIAP's queue just because it was insisted on.
        sess = _make_session(gs.Stage.CONFIRM)
        sess.license_id = 459
        sess.license_name = "PPKP"
        sess.documents = [gs.SessionDocument("d1", "ktp", "ktp.jpg", "image/jpeg", b"x")]
        sess.last_score = {
            "ok": False, "status": "needs_fix", "score_percent": 70,
            "summary": "s", "message": "m", "issues": [],
            "result": _suitability_result(70),  # no blocking issue — score alone
        }
        _run(gs._put_session(sess))
        probe = {"configured": 0}

        def _is_configured():
            probe["configured"] += 1
            return True

        fake_client = types.SimpleNamespace(
            is_configured=_is_configured,
            create_request=AsyncMock(return_value={"ok": True, "ticket": "X"}),
        )
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            with patch("services.siap_submission_client.get_siap_submission_client",
                       return_value=fake_client):
                reply = _run(gs._submit(sess))
        # Never filed, dropped to collecting, no bypass offered, escape named.
        self.assertEqual(probe["configured"], 0)
        fake_client.create_request.assert_not_awaited()
        self.assertEqual(sess.stage, gs.Stage.COLLECTING_DOCS)
        self.assertNotIn("apa adanya", reply.lower())
        # Every mandatory doc IS present here — only the content score is short.
        # THIS is where the escape earns its place: the block is BIMA's
        # judgement, which the citizen can legitimately dispute.
        self.assertIn("minta tinjau petugas", reply.lower())

    def test_escape_is_offered_only_once_uploading_is_finished(self):
        # The two halves of the rule, side by side. Same sub-threshold score;
        # the ONLY difference is whether a required document is still missing.
        sess = _make_session(gs.Stage.COLLECTING_DOCS)
        sess.license_name = "PPKP"

        still_missing = {
            "ok": False, "status": "needs_fix", "score_percent": 58,
            "summary": "s", "message": "m", "issues": [],
            "result": _suitability_result(58, blocking=True),  # missing=["NPWP"]
        }
        finished = {
            "ok": False, "status": "needs_fix", "score_percent": 70,
            "summary": "s", "message": "m", "issues": [],
            "result": _suitability_result(70),                 # missing=[]
        }
        self.assertEqual(gs._missing_documents(still_missing), ["NPWP"])
        self.assertEqual(gs._missing_documents(finished), [])

        blocked = gs._blocking_from_score(still_missing)
        self.assertNotIn(
            "minta tinjau petugas",
            gs._fmt_blocking_guidance(sess, still_missing, blocked).lower(),
        )
        self.assertIn(
            "minta tinjau petugas",
            gs._fmt_blocking_guidance(sess, finished, []).lower(),
        )

    def test_missing_documents_survives_a_redis_rehydrate(self):
        # `_score_for_redis` strips the `result` dataclass, so the escape gate
        # would silently flip on a restart if `missing` didn't round-trip.
        fresh = {
            "ok": False, "status": "needs_fix", "score_percent": 58,
            "summary": "s", "message": "m", "issues": [],
            "missing": ["NPWP"],
            "result": _suitability_result(58, blocking=True),
        }
        rehydrated = gs._score_for_redis(fresh)
        self.assertNotIn("result", rehydrated)
        self.assertEqual(gs._missing_documents(rehydrated), ["NPWP"])


class TestHandleInboundDocumentsSilent(unittest.TestCase):
    """handle_inbound_documents attaches SILENTLY (returns None) at the
    document-collection stages — the debounce produces the consolidated reply
    out-of-band; there is no per-file ack."""

    def setUp(self):
        gs._sessions.clear()
        gs._debounce_tasks.clear()

    def test_collecting_docs_returns_none_and_arms_debounce(self):
        async def scenario():
            sess = _make_session(gs.Stage.COLLECTING_DOCS)
            await gs._put_session(sess)
            with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true",
                                         "GUIDED_SUBMISSION_DEBOUNCE_SECONDS": "30"}, clear=False):
                r1 = await gs.handle_inbound_documents(
                    sess.user_id, [gs.SessionDocument("d1", "ktp", "ktp.jpg", "image/jpeg", b"x")])
                r2 = await gs.handle_inbound_documents(
                    sess.user_id, [gs.SessionDocument("d2", "nib", "nib.pdf", "application/pdf", b"%PDF")])
                # Both silent; ONE debounce task; both attached.
                self.assertIsNone(r1)
                self.assertIsNone(r2)
                self.assertEqual(len(gs._debounce_tasks), 1)
                self.assertEqual(len(gs._sessions[sess.user_id].documents), 2)
        _run(scenario())
        # Cleanup the long-debounce task left running.
        gs._debounce_tasks.clear()

    def test_resolving_license_upload_nudges_for_licence(self):
        sess = _make_session(gs.Stage.RESOLVING_LICENSE, with_fields=False)
        sess.license_id = None
        sess.license_name = None
        _run(gs._put_session(sess))
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            reply = _run(gs.handle_inbound_documents(
                sess.user_id, [gs.SessionDocument("d1", "ktp", "ktp.jpg", "image/jpeg", b"x")]))
        self.assertIsNotNone(reply)
        self.assertIn("izin", reply.lower())
        # No doc attached to a license-less session.
        self.assertEqual(len(gs._sessions[sess.user_id].documents), 0)

    def test_none_when_no_active_session(self):
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            doc = gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")
            self.assertIsNone(_run(gs.handle_inbound_documents("wa-nobody", [doc])))


class TestSubmitOfficerHandoff(unittest.TestCase):
    def setUp(self):
        gs._sessions.clear()

    def test_officer_bridge_invoked_on_success(self):
        sess = _make_session(gs.Stage.CONFIRM)
        sess.documents = [gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")]
        sess.last_score = {
            "ok": True, "status": "ready", "score_percent": 90,
            "summary": "s", "message": "m", "issues": [], "result": _suitability_result(90),
        }
        _run(gs._put_session(sess))

        fake_client = types.SimpleNamespace(
            is_configured=lambda: True,
            create_request=AsyncMock(return_value={
                "ok": True, "ticket": "000999888", "request_id": 77,
            }),
        )
        notify_mock = AsyncMock(return_value=True)
        bridge_stub = types.ModuleType("services.officer_bridge")
        bridge_stub.notify_officer_of_submission = notify_mock

        import services as _services_pkg
        env = {"GUIDED_SUBMISSION_PROFILE_ID": "12345"}
        with patch.dict(os.environ, env, clear=False):
            with patch("services.siap_submission_client.get_siap_submission_client",
                       return_value=fake_client):
                with patch.dict(sys.modules, {"services.officer_bridge": bridge_stub}):
                    with patch.object(_services_pkg, "officer_bridge", bridge_stub, create=True):
                        reply = _run(gs._submit(sess))

        self.assertIn("000999888", reply)
        notify_mock.assert_awaited_once()
        _, kwargs = notify_mock.call_args
        self.assertEqual(kwargs["ticket"], "000999888")
        self.assertEqual(kwargs["request_id"], 77)
        self.assertEqual(len(kwargs["documents"]), 1)
        self.assertEqual(kwargs["score"]["score_percent"], 90)

    def test_request_id_resolved_from_ticket_when_siap_omits_it(self):
        sess = _make_session(gs.Stage.CONFIRM)
        sess.documents = [gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")]
        sess.last_score = {
            "ok": True, "status": "ready", "score_percent": 90,
            "summary": "s", "message": "m", "issues": [], "result": _suitability_result(90),
        }
        _run(gs._put_session(sess))

        fake_client = types.SimpleNamespace(
            is_configured=lambda: True,
            create_request=AsyncMock(return_value={
                "ok": True, "ticket": "000999888", "request_id": None,
            }),
        )
        notify_mock = AsyncMock(return_value=True)
        bridge_stub = types.ModuleType("services.officer_bridge")
        bridge_stub.notify_officer_of_submission = notify_mock
        resolve_mock = AsyncMock(return_value=4242)

        import services as _services_pkg
        env = {"GUIDED_SUBMISSION_PROFILE_ID": "12345"}
        with patch.dict(os.environ, env, clear=False):
            with patch("services.siap_submission_client.get_siap_submission_client",
                       return_value=fake_client):
                with patch("services.siap_tools.siap_resolve_request_id", resolve_mock):
                    with patch.dict(sys.modules, {"services.officer_bridge": bridge_stub}):
                        with patch.object(_services_pkg, "officer_bridge", bridge_stub, create=True):
                            reply = _run(gs._submit(sess))

        self.assertIn("000999888", reply)
        resolve_mock.assert_awaited_once_with("000999888")
        notify_mock.assert_awaited_once()
        _, kwargs = notify_mock.call_args
        self.assertEqual(kwargs["request_id"], 4242)

    def test_submit_succeeds_when_request_id_unrecoverable(self):
        sess = _make_session(gs.Stage.CONFIRM)
        sess.documents = [gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")]
        sess.last_score = {
            "ok": True, "status": "ready", "score_percent": 90,
            "summary": "s", "message": "m", "issues": [], "result": _suitability_result(90),
        }
        _run(gs._put_session(sess))

        fake_client = types.SimpleNamespace(
            is_configured=lambda: True,
            create_request=AsyncMock(return_value={
                "ok": True, "ticket": "000999888", "request_id": None,
            }),
        )
        notify_mock = AsyncMock(return_value=False)
        bridge_stub = types.ModuleType("services.officer_bridge")
        bridge_stub.notify_officer_of_submission = notify_mock
        resolve_mock = AsyncMock(return_value=None)

        import services as _services_pkg
        env = {"GUIDED_SUBMISSION_PROFILE_ID": "12345"}
        with patch.dict(os.environ, env, clear=False):
            with patch("services.siap_submission_client.get_siap_submission_client",
                       return_value=fake_client):
                with patch("services.siap_tools.siap_resolve_request_id", resolve_mock):
                    with patch.dict(sys.modules, {"services.officer_bridge": bridge_stub}):
                        with patch.object(_services_pkg, "officer_bridge", bridge_stub, create=True):
                            reply = _run(gs._submit(sess))

        self.assertIn("000999888", reply)
        notify_mock.assert_awaited_once()
        _, kwargs = notify_mock.call_args
        self.assertIsNone(kwargs["request_id"])


class TestSubmitRescoresAfterRehydrate(unittest.TestCase):
    """After a Redis rehydrate, last_score is a non-None dict whose rich
    `result` was stripped on encode. _submit must re-score, not reuse the trim."""

    def setUp(self):
        gs._sessions.clear()

    def test_rescore_when_result_missing_from_rehydrated_score(self):
        sess = _make_session(gs.Stage.CONFIRM)
        sess.documents = [gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")]
        sess.last_score = {
            "ok": True, "status": "ready", "score_percent": 90,
            "summary": "s", "message": "m", "issues": [],
        }
        _run(gs._put_session(sess))

        unconfigured = types.SimpleNamespace(is_configured=lambda: False)
        fresh = _suitability_result(percent=90)
        score_mock = AsyncMock(return_value=fresh)
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            with patch("services.citizen_scorer.score_session_documents", score_mock):
                with patch("services.siap_submission_client.get_siap_submission_client",
                           return_value=unconfigured):
                    _run(gs._submit(sess))
        score_mock.assert_awaited_once()
        self.assertIn("result", sess.last_score)
        self.assertIs(sess.last_score["result"], fresh)

    def test_no_rescore_when_result_already_present(self):
        sess = _make_session(gs.Stage.CONFIRM)
        sess.documents = [gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")]
        sess.last_score = {
            "ok": True, "status": "ready", "score_percent": 90,
            "summary": "s", "message": "m", "issues": [], "result": _suitability_result(90),
        }
        _run(gs._put_session(sess))
        unconfigured = types.SimpleNamespace(is_configured=lambda: False)
        score_mock = AsyncMock(return_value=_suitability_result(90))
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            with patch("services.citizen_scorer.score_session_documents", score_mock):
                with patch("services.siap_submission_client.get_siap_submission_client",
                           return_value=unconfigured):
                    _run(gs._submit(sess))
        score_mock.assert_not_awaited()


class TestScoreReminder(unittest.TestCase):
    """A compact 'Skor terkini: X% (status)' line is prepended to an UNCLEAR
    CONFIRM re-prompt once a score exists (using the persisted last_score)."""

    def setUp(self):
        gs._sessions.clear()

    def test_reminder_prepended_on_unclear_confirm(self):
        sess = _make_session(gs.Stage.CONFIRM)
        sess.last_score = {
            "ok": False, "status": "needs_fix", "score_percent": 73,
            "summary": "s", "message": "m", "issues": [],
        }
        _run(gs._put_session(sess))
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            with patch("services.submission_intent.classify_confirm_intent",
                       new=AsyncMock(return_value="UNCLEAR")):
                reply = _run(gs.maybe_handle(sess.user_id, "hmm bagaimana ya"))
        self.assertIsNotNone(reply)
        self.assertIn("Skor terkini: 73%", reply)
        self.assertIn("perlu diperbaiki", reply)

    def test_no_reminder_without_score(self):
        sess = _make_session(gs.Stage.CONFIRM)
        sess.last_score = None
        _run(gs._put_session(sess))
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            with patch("services.submission_intent.classify_confirm_intent",
                       new=AsyncMock(return_value="UNCLEAR")):
                reply = _run(gs.maybe_handle(sess.user_id, "hmm"))
        self.assertNotIn("Skor terkini", reply)


if __name__ == "__main__":
    unittest.main(verbosity=2)
