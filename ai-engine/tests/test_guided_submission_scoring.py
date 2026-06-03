"""
Tests for the live content-scoring additions to the guided-submission flow —
the new document intake + suitability-judge wiring in
`services/guided_submission.py`.

Run standalone (no pytest):

    python -m tests.test_guided_submission_scoring     # from ai-engine/
    python tests/test_guided_submission_scoring.py     # also works

Covers:
  * attach_documents — attaches in-session bytes to an active session, caps
    count, no-ops on a terminal/missing session.
  * _load_demo_packet — reads a directory of files, derives claimed types
    from filenames, respects the env flag.
  * _run_content_score — calls the citizen scorer (mocked) and normalises the
    result; returns None when there are no in-session docs.
  * _enter_doc_scoring — packet present → live score shown + REVIEW;
    no packet → fixture fallback path.
  * _submit officer hand-off — on a successful SIAP create, the officer
    bridge is invoked with the score + documents (mocked).

No real Gemini, no real DB, no real SIAP. The citizen scorer, the SIAP
submission client, and the officer bridge are all patched.
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


def _suitability_result(percent=90, critical=False):
    issues = []
    if critical:
        issues = [Issue(id="c", severity="critical", title="Dokumen wajib hilang", detail="d")]
    return SuitabilityResult(
        completeness=CompletenessSection(score=1.0, missing=[], required=["KTP"]),
        type_correctness=[],
        suitability=[],
        compatibility_findings=[],
        overall_suitability_score=percent / 100.0,
        issues=issues,
    )


def _make_session(stage=gs.Stage.COLLECTING_FIELDS):
    sess = gs.SubmissionSession(user_id="wa-628123")
    sess.license_id = 358
    sess.license_name = "Izin Penelitian"
    sess.stage = stage
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

    def test_attaches_to_active_session(self):
        sess = _make_session(gs.Stage.COLLECTING_DOCS)
        _run(gs._put_session(sess))
        ok = _run(gs.attach_documents(sess.user_id, [
            gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x"),
        ]))
        self.assertTrue(ok)
        self.assertEqual(len(gs._sessions[sess.user_id].documents), 1)

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


class TestEnterDocScoring(unittest.TestCase):
    def setUp(self):
        gs._sessions.clear()

    def test_with_packet_shows_score_and_enters_review(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "ktp.jpg").write_bytes(b"\xff\xd8\xff fake")
            sess = _make_session()
            _run(gs._put_session(sess))
            fake = _suitability_result(percent=82)
            env = {"GUIDED_SUBMISSION_DEMO_PACKET": d}
            with patch.dict(os.environ, env, clear=False):
                with patch("services.citizen_scorer.score_session_documents",
                           new=AsyncMock(return_value=fake)):
                    reply = _run(gs._enter_doc_scoring(sess))
            self.assertEqual(sess.stage, gs.Stage.REVIEW)
            self.assertIsNotNone(sess.last_score)
            # The live score message AND the review summary are both shown.
            self.assertIn("82%", reply)
            self.assertIn("Jenis izin", reply)

    def test_without_packet_falls_back_to_fixture_review(self):
        sess = _make_session()
        _run(gs._put_session(sess))
        with patch.dict(os.environ, {"GUIDED_SUBMISSION_DEMO_PACKET": ""}, clear=False):
            reply = _run(gs._enter_doc_scoring(sess))
        self.assertEqual(sess.stage, gs.Stage.REVIEW)
        self.assertIsNone(sess.last_score)
        self.assertIn("Jenis izin", reply)


class TestSubmitOfficerHandoff(unittest.TestCase):
    def setUp(self):
        gs._sessions.clear()

    def test_officer_bridge_invoked_on_success(self):
        sess = _make_session(gs.Stage.REVIEW)
        sess.documents = [gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")]
        sess.last_score = {
            "ok": True, "status": "ready", "score_percent": 90,
            "summary": "s", "message": "m", "issues": [], "result": _suitability_result(90),
        }
        _run(gs._put_session(sess))

        # SIAP submission client returns a ticket.
        fake_client = types.SimpleNamespace(
            is_configured=lambda: True,
            create_request=AsyncMock(return_value={
                "ok": True, "ticket": "000999888", "request_id": 77,
            }),
        )
        notify_mock = AsyncMock(return_value=True)
        bridge_stub = types.ModuleType("services.officer_bridge")
        bridge_stub.notify_officer_of_submission = notify_mock

        # _submit does `from services import officer_bridge`. If the REAL
        # officer_bridge was already imported (e.g. another test file ran
        # first under pytest), the `services` package holds it as an ATTRIBUTE
        # and the from-import returns that attribute, NOT sys.modules — so
        # patching sys.modules alone is silently ineffective. Patch BOTH the
        # package attribute and sys.modules so this is order-independent.
        import services as _services_pkg
        env = {"GUIDED_SUBMISSION_PROFILE_ID": "12345"}
        with patch.dict(os.environ, env, clear=False):
            with patch("services.siap_submission_client.get_siap_submission_client",
                       return_value=fake_client):
                with patch.dict(sys.modules, {"services.officer_bridge": bridge_stub}):
                    with patch.object(_services_pkg, "officer_bridge", bridge_stub,
                                      create=True):
                        reply = _run(gs._submit(sess))

        self.assertIn("000999888", reply)
        notify_mock.assert_awaited_once()
        _, kwargs = notify_mock.call_args
        self.assertEqual(kwargs["ticket"], "000999888")
        self.assertEqual(kwargs["request_id"], 77)
        self.assertEqual(len(kwargs["documents"]), 1)
        self.assertEqual(kwargs["score"]["score_percent"], 90)

    def test_request_id_resolved_from_ticket_when_siap_omits_it(self):
        # FIX 1: SIAP returns a ticket but a None request_id on a successful
        # submit. _submit must recover the request_id from the ticket (so
        # flow-based officer resolution, which is gated on request_id, runs) and
        # pass the recovered id into the officer hand-off.
        sess = _make_session(gs.Stage.REVIEW)
        sess.documents = [gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")]
        sess.last_score = {
            "ok": True, "status": "ready", "score_percent": 90,
            "summary": "s", "message": "m", "issues": [], "result": _suitability_result(90),
        }
        _run(gs._put_session(sess))

        fake_client = types.SimpleNamespace(
            is_configured=lambda: True,
            create_request=AsyncMock(return_value={
                "ok": True, "ticket": "000999888", "request_id": None,  # omitted!
            }),
        )
        notify_mock = AsyncMock(return_value=True)
        bridge_stub = types.ModuleType("services.officer_bridge")
        bridge_stub.notify_officer_of_submission = notify_mock
        # The ticket→request_id recovery seam.
        resolve_mock = AsyncMock(return_value=4242)

        import services as _services_pkg
        env = {"GUIDED_SUBMISSION_PROFILE_ID": "12345"}
        with patch.dict(os.environ, env, clear=False):
            with patch("services.siap_submission_client.get_siap_submission_client",
                       return_value=fake_client):
                with patch("services.siap_tools.siap_resolve_request_id", resolve_mock):
                    with patch.dict(sys.modules, {"services.officer_bridge": bridge_stub}):
                        with patch.object(_services_pkg, "officer_bridge", bridge_stub,
                                          create=True):
                            reply = _run(gs._submit(sess))

        self.assertIn("000999888", reply)
        # Recovery was attempted with the freshly-allocated ticket.
        resolve_mock.assert_awaited_once_with("000999888")
        # ...and the recovered id flows into the officer hand-off.
        notify_mock.assert_awaited_once()
        _, kwargs = notify_mock.call_args
        self.assertEqual(kwargs["request_id"], 4242)

    def test_submit_succeeds_when_request_id_unrecoverable(self):
        # FIX 1 degradation: ticket present, request_id None, and the ticket
        # lookup also yields nothing. _submit must NOT crash — it proceeds with
        # request_id=None (notify falls back to env as today) and the citizen
        # still gets the success reply with the ticket.
        sess = _make_session(gs.Stage.REVIEW)
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
        resolve_mock = AsyncMock(return_value=None)  # ticket lookup misses too

        import services as _services_pkg
        env = {"GUIDED_SUBMISSION_PROFILE_ID": "12345"}
        with patch.dict(os.environ, env, clear=False):
            with patch("services.siap_submission_client.get_siap_submission_client",
                       return_value=fake_client):
                with patch("services.siap_tools.siap_resolve_request_id", resolve_mock):
                    with patch.dict(sys.modules, {"services.officer_bridge": bridge_stub}):
                        with patch.object(_services_pkg, "officer_bridge", bridge_stub,
                                          create=True):
                            reply = _run(gs._submit(sess))

        self.assertIn("000999888", reply)
        notify_mock.assert_awaited_once()
        _, kwargs = notify_mock.call_args
        self.assertIsNone(kwargs["request_id"])


class TestSubmitRescoresAfterRehydrate(unittest.TestCase):
    """FIX 3: after a Redis rehydrate, last_score is a non-None dict whose rich
    `result` was stripped on encode. _submit must re-score (so the officer brief
    / sub-threshold message get the full result), not reuse the trimmed dict."""

    def setUp(self):
        gs._sessions.clear()

    def test_rescore_when_result_missing_from_rehydrated_score(self):
        sess = _make_session(gs.Stage.REVIEW)
        sess.documents = [gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")]
        # Rehydrated score: JSON-safe fields only, NO `result` key.
        sess.last_score = {
            "ok": True, "status": "ready", "score_percent": 90,
            "summary": "s", "message": "m", "issues": [],
        }
        _run(gs._put_session(sess))

        # SIAP unconfigured → _submit stops right after scoring (before any
        # network), which is all we need to observe the re-score decision.
        unconfigured = types.SimpleNamespace(is_configured=lambda: False)
        fresh = _suitability_result(percent=90)
        score_mock = AsyncMock(return_value=fresh)
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            with patch("services.citizen_scorer.score_session_documents", score_mock):
                with patch("services.siap_submission_client.get_siap_submission_client",
                           return_value=unconfigured):
                    _run(gs._submit(sess))
        # Re-scored from the rehydrated document bytes...
        score_mock.assert_awaited_once()
        # ...and the rich result is now present on the session score.
        self.assertIn("result", sess.last_score)
        self.assertIs(sess.last_score["result"], fresh)

    def test_no_rescore_when_result_already_present(self):
        sess = _make_session(gs.Stage.REVIEW)
        sess.documents = [gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")]
        # A complete in-memory score (rich result intact) → no re-score.
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
    """Part C: a compact 'Skor terkini: X% (status)' line is prepended to a
    REVIEW-stage clarifying message (not YA/KIRIM) once a score exists, using
    the persisted last_score."""

    def setUp(self):
        gs._sessions.clear()

    def test_reminder_prepended_in_review(self):
        sess = _make_session(gs.Stage.REVIEW)
        sess.last_score = {
            "ok": False, "status": "needs_fix", "score_percent": 73,
            "summary": "s", "message": "m", "issues": [],
        }
        _run(gs._put_session(sess))
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            # A non-YA / non-KIRIM message in REVIEW → fallback reply with the
            # compact score reminder prepended.
            reply = _run(gs.maybe_handle(sess.user_id, "apakah ini sudah benar?"))
        self.assertIsNotNone(reply)
        self.assertIn("Skor terkini: 73%", reply)
        self.assertIn("perlu diperbaiki", reply)
        # The standard confirm instruction still follows.
        self.assertIn("YA", reply.upper())

    def test_no_reminder_without_score(self):
        sess = _make_session(gs.Stage.REVIEW)
        sess.last_score = None
        _run(gs._put_session(sess))
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            reply = _run(gs.maybe_handle(sess.user_id, "halo?"))
        self.assertNotIn("Skor terkini", reply)


class TestHandleInboundDocuments(unittest.TestCase):
    """Part B seam: handle_inbound_documents attaches + scores + returns a
    reply, or acknowledges while still collecting fields."""

    def setUp(self):
        gs._sessions.clear()

    def test_scores_when_in_review(self):
        sess = _make_session(gs.Stage.REVIEW)
        _run(gs._put_session(sess))
        fake = _suitability_result(percent=91)
        doc = gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            with patch("services.citizen_scorer.score_session_documents",
                       new=AsyncMock(return_value=fake)):
                reply = _run(gs.handle_inbound_documents(sess.user_id, [doc]))
        self.assertIsNotNone(reply)
        self.assertIn("91%", reply)
        self.assertEqual(len(gs._sessions[sess.user_id].documents), 1)

    def test_acknowledges_when_collecting_fields(self):
        sess = _make_session(gs.Stage.COLLECTING_FIELDS)
        # Make one field missing so there's a "next question".
        sess.fields.pop("phone", None)
        _run(gs._put_session(sess))
        doc = gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            reply = _run(gs.handle_inbound_documents(sess.user_id, [doc]))
        self.assertIsNotNone(reply)
        self.assertIn("Dokumen diterima", reply)
        # Document is stored for later scoring.
        self.assertEqual(len(gs._sessions[sess.user_id].documents), 1)

    def test_none_when_no_active_session(self):
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            doc = gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"x")
            self.assertIsNone(_run(gs.handle_inbound_documents("wa-nobody", [doc])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
