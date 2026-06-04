"""
Tests for the Wave 3 guided-submission flow — `services/guided_submission.py`
and `services/siap_submission_client.py`.

Run standalone (no pytest needed — matches tests/test_siap_write.py):

    python -m tests.test_guided_submission     # from ai-engine/
    python tests/test_guided_submission.py     # also works

What it covers:
  * detect_submission_intent — positive + negative phrasing.
  * SiapSubmissionClient — not-configured, 2xx success, 4xx, timeout,
    network error, all with `httpx.AsyncClient` mocked.
  * The guided-submission state machine end to end with the LLM licence
    resolver (services.license_resolver.resolve_license_intent) / SIAP
    requirements and the SIAP submission client mocked:
      - feature flag off → maybe_handle returns None;
      - happy path: intent → single licence → collect 4 fields → review →
        confirm → validate (clean fixture) → submit → ticket reply;
      - field validation: a bad NIK is rejected and re-asked;
      - validation issues (name_mismatch fixture) → flow does NOT submit;
      - submission client not configured → graceful "channel not ready";
      - cancel at any step clears the session.

No real network, no SIAP instance, no Gemini. The validator integration is
exercised through the on-disk demo fixtures, which is the flow's real path.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Make `services` importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Stub heavy deps so importing guided_submission (which transitively can pull
# siap_tools → siap_db → asyncpg) succeeds without the full stack installed.
# Every code path that would USE them is mocked in the individual tests.
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


_ensure_stub("asyncpg")
_ensure_stub("dotenv", {"load_dotenv": lambda *a, **k: None})

from services import guided_submission as gs  # noqa: E402
from services.siap_submission_client import SiapSubmissionClient  # noqa: E402
from services.agents.suitability_judge import (  # noqa: E402
    CompletenessSection,
    Issue,
    SuitabilityResult,
)


def _run(coro):
    return asyncio.run(coro)


def _suitability_result(percent=96, critical=False):
    """A minimal SuitabilityResult for mocking citizen_scorer in the flow
    tests (the live-score path is now the production path after the explicit
    document-collection step)."""
    issues = []
    if critical:
        issues = [Issue(id="c", severity="critical",
                        title="Dokumen wajib hilang", detail="d")]
    return SuitabilityResult(
        completeness=CompletenessSection(score=1.0, missing=[], required=["KTP"]),
        type_correctness=[],
        suitability=[],
        compatibility_findings=[],
        overall_suitability_score=percent / 100.0,
        issues=issues,
    )


def _doc(file_id="doc-1"):
    return gs.SessionDocument(file_id, "ktp", "ktp.jpg", "image/jpeg", b"\xff\xd8\xff")


# ===========================================================================
# Intent detection
# ===========================================================================

class TestIntentDetection(unittest.TestCase):

    def test_positive_indonesian(self):
        for msg in (
            "saya mau ajukan izin pemakaian tanah",
            "tolong bantu daftarkan izin lingkungan untuk usaha saya",
            "saya ingin mengajukan permohonan izin trayek",
            "mau buatkan NIB dong",
        ):
            self.assertTrue(
                gs.detect_submission_intent(msg), f"should detect: {msg!r}"
            )

    def test_positive_english(self):
        self.assertTrue(
            gs.detect_submission_intent("I want to apply for a new permit")
        )

    def test_negative(self):
        for msg in (
            "apa itu izin usaha?",
            "status izin 000077591",
            "halo bima",
            "saya mau daftar antrean",          # no licensing object
            "berapa biaya izin pemakaian tanah",  # a question, not a filing
        ):
            self.assertFalse(
                gs.detect_submission_intent(msg), f"should NOT detect: {msg!r}"
            )


# ===========================================================================
# SiapSubmissionClient
# ===========================================================================

class TestSiapSubmissionClient(unittest.TestCase):

    def test_not_configured(self):
        client = SiapSubmissionClient(base="", token="")
        self.assertFalse(client.is_configured())
        res = _run(client.create_request(license_id=5, profile_id=9))
        self.assertFalse(res["ok"])
        self.assertFalse(res["configured"])

    def test_success(self):
        client = SiapSubmissionClient(base="http://siap.test", token="tok")
        self.assertTrue(client.is_configured())

        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {
            "data": {"request_id": 4321, "ticket": "000099001"}
        }
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            res = _run(client.create_request(license_id=5, profile_id=9,
                                             description="test"))
        self.assertTrue(res["ok"])
        self.assertEqual(res["request_id"], 4321)
        self.assertEqual(res["ticket"], "000099001")

    def test_http_error(self):
        client = SiapSubmissionClient(base="http://siap.test", token="tok")
        resp = MagicMock()
        resp.status_code = 403
        resp.json.return_value = {"message": "ability missing"}
        resp.text = '{"message": "ability missing"}'
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            res = _run(client.create_request(license_id=5, profile_id=9))
        self.assertFalse(res["ok"])
        self.assertEqual(res["http_status"], 403)
        self.assertIn("submission:create", res["note"])

    def test_timeout(self):
        import httpx
        client = SiapSubmissionClient(base="http://siap.test", token="tok")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("t"))
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            res = _run(client.create_request(license_id=5, profile_id=9))
        self.assertFalse(res["ok"])
        self.assertTrue(res["configured"])

    def test_bad_ids(self):
        client = SiapSubmissionClient(base="http://siap.test", token="tok")
        res = _run(client.create_request(license_id="abc", profile_id=9))  # type: ignore[arg-type]
        self.assertFalse(res["ok"])


# ===========================================================================
# State machine
# ===========================================================================

# A single, unambiguous SIAP licence match.
_ONE_MATCH = {
    "found": True,
    "count": 1,
    "matches": [{"license_id": 42, "name": "Izin Pemakaian Tanah",
                 "sektor": "Pekerjaan Umum"}],
}
_REQUIREMENTS = {
    "found": True,
    "license_id": 42,
    "license_name": "Izin Pemakaian Tanah",
    "requirements": ["Fotokopi KTP", "Surat permohonan"],
    "sla_working_days": 14,
    "retribution_fee": "Rp 0",
}


class _GuidedFlowBase(unittest.TestCase):
    """Shared setup: flag ON, clean session table, patched SIAP tools."""

    def setUp(self):
        gs._sessions.clear()
        os.environ["BIMA_GUIDED_SUBMISSION_ENABLED"] = "true"
        os.environ["GUIDED_SUBMISSION_DEMO_FIXTURE"] = "clean"
        os.environ["GUIDED_SUBMISSION_PROFILE_ID"] = "7"

    def tearDown(self):
        gs._sessions.clear()
        os.environ.pop("BIMA_GUIDED_SUBMISSION_ENABLED", None)
        os.environ.pop("GUIDED_SUBMISSION_DEMO_FIXTURE", None)
        os.environ.pop("GUIDED_SUBMISSION_PROFILE_ID", None)


class TestFeatureFlag(_GuidedFlowBase):

    def test_flag_off_returns_none(self):
        os.environ["BIMA_GUIDED_SUBMISSION_ENABLED"] = "false"
        res = _run(gs.maybe_handle("u1", "saya mau ajukan izin pemakaian tanah"))
        self.assertIsNone(res)

    def test_non_submission_message_returns_none(self):
        # Flag ON, but no active session and no intent → None (untouched).
        res = _run(gs.maybe_handle("u1", "apa itu KBLI?"))
        self.assertIsNone(res)


class TestHappyPath(_GuidedFlowBase):

    def test_full_flow_to_ticket(self):
        submit_result = {
            "ok": True, "configured": True,
            "request_id": 555, "ticket": "000088002",
        }
        mock_client = MagicMock()
        mock_client.is_configured.return_value = True
        mock_client.create_request = AsyncMock(return_value=submit_result)

        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)), \
             patch("services.citizen_scorer.score_session_documents",
                   new=AsyncMock(return_value=_suitability_result(96))), \
             patch("services.siap_submission_client.get_siap_submission_client",
                   return_value=mock_client):

            uid = "wa-628"
            # 1. intent → licence resolved; requirements shown + readiness gate.
            r1 = _run(gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
            self.assertIsNotNone(r1)
            self.assertIn("Izin Pemakaian Tanah", r1)
            # FIX C: do NOT ask field 1 yet — ask the citizen to confirm "SIAP".
            self.assertIn("SIAP", r1.upper())
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.CONFIRMING_START)
            self.assertTrue(_run(gs.has_active_session(uid)))

            # 2. readiness affirmative → first field asked.
            rc = _run(gs.maybe_handle(uid, "SIAP"))
            self.assertIn("1.", rc)
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.COLLECTING_FIELDS)

            # 3. applicant name.
            r2 = _run(gs.maybe_handle(uid, "Budi Santoso"))
            self.assertIn("NIK", r2)

            # 4. NIK (16 digits).
            r3 = _run(gs.maybe_handle(uid, "3374012345678901"))
            self.assertIsNotNone(r3)

            # 5. business name.
            r4 = _run(gs.maybe_handle(uid, "Warung Budi"))
            self.assertIsNotNone(r4)

            # 6. phone → all fields collected → FIX D: explicit document step.
            r5 = _run(gs.maybe_handle(uid, "081234567890"))
            self.assertIn("kirim dokumen", r5.lower())
            self.assertIn("SELESAI", r5)
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.COLLECTING_DOCS)

            # 7. a document arrives → running count, NOT scored yet.
            rdoc = _run(gs.handle_inbound_documents(uid, [_doc()]))
            self.assertIn("1 total", rdoc)
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.COLLECTING_DOCS)

            # 8. SELESAI → live score → REVIEW.
            r6 = _run(gs.maybe_handle(uid, "SELESAI"))
            self.assertIn("YA", r6.upper())
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.REVIEW)

            # 9. confirm → submit → ticket.
            r7 = _run(gs.maybe_handle(uid, "ya"))
            self.assertIn("000088002", r7)
            self.assertIn("track/000088002", r7)
            # Session cleared after a successful submit.
            self.assertFalse(_run(gs.has_active_session(uid)))
            mock_client.create_request.assert_awaited_once()

    def test_bad_nik_is_rejected_and_reasked(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)):
            uid = "wa-629"
            _run(gs.maybe_handle(uid, "mau ajukan izin pemakaian tanah"))
            _run(gs.maybe_handle(uid, "SIAP"))   # pass the readiness gate
            _run(gs.maybe_handle(uid, "Budi Santoso"))
            # Bad NIK — too short.
            bad = _run(gs.maybe_handle(uid, "123"))
            self.assertIn("16 digit", bad)
            # Session still collecting, NIK not stored.
            sess = gs._sessions[uid]
            self.assertNotIn("nik", sess.fields)


def _advance_to_review(uid, *, score):
    """Drive a session from intent → readiness → fields → one doc → SELESAI,
    leaving it at REVIEW with `score` as the mocked content-score. Caller must
    be inside the license_resolver / siap_get_requirements / citizen_scorer
    patches. Returns the last reply (the REVIEW message)."""
    _run(gs.maybe_handle(uid, "mau ajukan izin pemakaian tanah"))
    _run(gs.maybe_handle(uid, "SIAP"))
    _run(gs.maybe_handle(uid, "Budi Santoso"))
    _run(gs.maybe_handle(uid, "3374012345678901"))
    _run(gs.maybe_handle(uid, "Warung Budi"))
    _run(gs.maybe_handle(uid, "081234567890"))   # → COLLECTING_DOCS
    _run(gs.handle_inbound_documents(uid, [_doc()]))
    return _run(gs.maybe_handle(uid, "SELESAI"))   # → REVIEW


class TestValidationGate(_GuidedFlowBase):

    def test_validation_issues_block_submit(self):
        # A sub-threshold live score (62%, no critical) → flow must NOT submit
        # on the first YA; it offers the "send as-is" override instead.
        mock_client = MagicMock()
        mock_client.is_configured.return_value = True
        mock_client.create_request = AsyncMock()

        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)), \
             patch("services.citizen_scorer.score_session_documents",
                   new=AsyncMock(return_value=_suitability_result(62))), \
             patch("services.siap_submission_client.get_siap_submission_client",
                   return_value=mock_client):
            uid = "wa-630"
            _advance_to_review(uid, score=62)
            reply = _run(gs.maybe_handle(uid, "ya"))

        # The sub-threshold packet is not auto-submitted; an override is offered.
        self.assertIn("KIRIM", reply.upper())
        mock_client.create_request.assert_not_awaited()
        # Session stays at REVIEW so the citizen can retry / override.
        self.assertTrue(_run(gs.has_active_session(uid)))


class TestDegradeGracefully(_GuidedFlowBase):

    def test_submission_client_not_configured(self):
        mock_client = MagicMock()
        mock_client.is_configured.return_value = False

        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)), \
             patch("services.citizen_scorer.score_session_documents",
                   new=AsyncMock(return_value=_suitability_result(96))), \
             patch("services.siap_submission_client.get_siap_submission_client",
                   return_value=mock_client):
            uid = "wa-631"
            _advance_to_review(uid, score=96)
            reply = _run(gs.maybe_handle(uid, "ya"))

        self.assertIn("belum aktif", reply)
        self.assertIn("belum terkirim", reply)


class TestCancel(_GuidedFlowBase):

    def test_cancel_clears_session(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)):
            uid = "wa-632"
            _run(gs.maybe_handle(uid, "mau ajukan izin pemakaian tanah"))
            self.assertTrue(_run(gs.has_active_session(uid)))
            reply = _run(gs.maybe_handle(uid, "batal"))
            self.assertIn("dibatalkan", reply)
            self.assertFalse(_run(gs.has_active_session(uid)))


# ===========================================================================
# FIX C — readiness gate (CONFIRMING_START)
# ===========================================================================

class TestReadinessGate(_GuidedFlowBase):
    """After the licence is locked and requirements are shown, the flow parks
    in CONFIRMING_START and asks for an explicit SIAP before field 1."""

    def _start(self, uid):
        return _run(gs.maybe_handle(uid, "mau ajukan izin pemakaian tanah"))

    def test_lock_shows_requirements_and_asks_siap_not_field1(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)):
            uid = "wa-c1"
            r = self._start(uid)
            # Requirements are listed...
            self.assertIn("Fotokopi KTP", r)
            # ...and the readiness prompt is shown, NOT the first field question.
            self.assertIn("siap saya pandu", r)
            self.assertIn("SIAP", r)
            self.assertNotIn("nama lengkap pemohon", r)
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.CONFIRMING_START)

    def test_siap_advances_to_field_collection(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)):
            uid = "wa-c2"
            self._start(uid)
            for word in ("SIAP", "ya", "mulai", "oke", "siap dipandu"):
                # Each affirmative variant advances a fresh session.
                gs._sessions.clear()
                self._start(uid)
                r = _run(gs.maybe_handle(uid, word))
                self.assertEqual(gs._sessions[uid].stage,
                                 gs.Stage.COLLECTING_FIELDS, f"word={word!r}")
                self.assertIn("1.", r)
                self.assertIn("nama lengkap pemohon", r)

    def test_junk_reprompts_with_siap_batal(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)):
            uid = "wa-c3"
            self._start(uid)
            r = _run(gs.maybe_handle(uid, "hmm apa ini"))
            # Stays in CONFIRMING_START and re-prompts with SIAP/BATAL.
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.CONFIRMING_START)
            self.assertIn("SIAP", r)
            self.assertIn("BATAL", r)

    def test_batal_cancels_from_confirming_start(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)):
            uid = "wa-c4"
            self._start(uid)
            r = _run(gs.maybe_handle(uid, "BATAL"))
            self.assertIn("dibatalkan", r)
            self.assertFalse(_run(gs.has_active_session(uid)))


# ===========================================================================
# FIX D — explicit document-collection step (COLLECTING_DOCS + SELESAI)
# ===========================================================================

class TestDocumentCollectionStep(_GuidedFlowBase):
    """After the last applicant field, the flow asks for documents and waits
    for SELESAI before scoring — it does not silently jump to scoring."""

    def _collect_fields(self, uid):
        """Intent → SIAP → all 4 fields. Leaves the session at COLLECTING_DOCS
        and returns the document-prompt reply."""
        _run(gs.maybe_handle(uid, "mau ajukan izin pemakaian tanah"))
        _run(gs.maybe_handle(uid, "SIAP"))
        _run(gs.maybe_handle(uid, "Budi Santoso"))
        _run(gs.maybe_handle(uid, "3374012345678901"))
        _run(gs.maybe_handle(uid, "Warung Budi"))
        return _run(gs.maybe_handle(uid, "081234567890"))

    def test_last_field_asks_for_documents(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)):
            uid = "wa-d1"
            r = self._collect_fields(uid)
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.COLLECTING_DOCS)
            self.assertIn("Data pemohon lengkap", r)
            self.assertIn("kirim dokumen", r.lower())
            self.assertIn("JPG/PNG", r)
            self.assertIn("PDF", r)
            self.assertIn("SELESAI", r)

    def test_each_upload_acknowledged_with_running_count(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)), \
             patch("services.citizen_scorer.score_session_documents",
                   new=AsyncMock(return_value=_suitability_result(96))) as scorer:
            uid = "wa-d2"
            self._collect_fields(uid)
            r1 = _run(gs.handle_inbound_documents(uid, [_doc("doc-1")]))
            self.assertIn("1 total", r1)
            r2 = _run(gs.handle_inbound_documents(uid, [_doc("doc-2")]))
            self.assertIn("2 total", r2)
            # No scoring happened on either upload — only on SELESAI.
            scorer.assert_not_awaited()
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.COLLECTING_DOCS)

    def test_selesai_with_docs_triggers_scoring_and_review(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)), \
             patch("services.citizen_scorer.score_session_documents",
                   new=AsyncMock(return_value=_suitability_result(96))) as scorer:
            uid = "wa-d3"
            self._collect_fields(uid)
            _run(gs.handle_inbound_documents(uid, [_doc()]))
            r = _run(gs.maybe_handle(uid, "SELESAI"))
            scorer.assert_awaited_once()
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.REVIEW)
            self.assertIn("96%", r)
            self.assertIn("YA", r.upper())

    def test_selesai_with_zero_docs_nudges(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)), \
             patch("services.citizen_scorer.score_session_documents",
                   new=AsyncMock(return_value=_suitability_result(96))) as scorer:
            uid = "wa-d4"
            self._collect_fields(uid)
            r = _run(gs.maybe_handle(uid, "SELESAI"))
            # No documents → nudge, no scoring, stay at COLLECTING_DOCS.
            scorer.assert_not_awaited()
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.COLLECTING_DOCS)
            self.assertIn("minimal", r.lower())
            self.assertIn("SELESAI", r)

    def test_non_selesai_text_reshows_doc_prompt(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)), \
             patch("services.citizen_scorer.score_session_documents",
                   new=AsyncMock(return_value=_suitability_result(96))) as scorer:
            uid = "wa-d5"
            self._collect_fields(uid)
            _run(gs.handle_inbound_documents(uid, [_doc()]))
            # A clarifying message (not SELESAI) must NOT score — re-show prompt.
            r = _run(gs.maybe_handle(uid, "apakah harus berwarna?"))
            scorer.assert_not_awaited()
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.COLLECTING_DOCS)
            self.assertIn("SELESAI", r)


# A SECOND, distinct licence the new-submission-intent restart resolves to, so
# the MULAI BARU test can prove the session re-resolved a DIFFERENT licence.
_OTHER_MATCH = {
    "found": True,
    "count": 1,
    "matches": [{"license_id": 99, "name": "Izin PKPP",
                 "sektor": "Perhubungan"}],
}
_OTHER_REQUIREMENTS = {
    "found": True,
    "license_id": 99,
    "license_name": "Izin PKPP",
    "requirements": ["KTP", "Surat kuasa"],
    "sla_working_days": 7,
    "retribution_fee": "Rp 0",
}


# ===========================================================================
# FIX A — new-submission intent interception mid-form + MULAI BARU
# ===========================================================================

class TestMidFormNewIntentInterception(_GuidedFlowBase):
    """A clear new-submission intent sent WHILE a form is active must be
    intercepted (offer MULAI BARU) and NOT eaten as a field answer / SELESAI /
    doc trigger. This is the exact incident: a half-finished form at
    COLLECTING_FIELDS (applicant_name) + "saya mau ajukan izin PKPP"."""

    def _to_collecting_name(self, uid):
        """Drive intent → readiness gate → COLLECTING_FIELDS, parked on the
        applicant_name question (no field collected yet)."""
        _run(gs.maybe_handle(uid, "mau ajukan izin pemakaian tanah"))
        _run(gs.maybe_handle(uid, "SIAP"))

    def test_new_intent_not_recorded_as_name_offers_mulai_baru(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)):
            uid = "wa-a1"
            self._to_collecting_name(uid)
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.COLLECTING_FIELDS)

            # The incident message — sent where the name answer is expected.
            reply = _run(gs.maybe_handle(uid, "saya mau ajukan izin PKPP"))

            # It was NOT recorded as the applicant_name field...
            sess = gs._sessions[uid]
            self.assertNotIn("applicant_name", sess.fields)
            # ...the flow did NOT advance to NIK...
            self.assertEqual(sess.stage, gs.Stage.COLLECTING_FIELDS)
            self.assertNotIn("NIK", reply)
            # ...and BIMA offered MULAI BARU, naming the in-progress licence.
            self.assertIn("MULAI BARU", reply.upper())
            self.assertIn("Izin Pemakaian Tanah", reply)
            # The intent was stashed for the restart.
            self.assertEqual(sess.pending_new_intent, "saya mau ajukan izin PKPP")

    def test_intercept_also_fires_in_confirming_start(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)):
            uid = "wa-a2"
            _run(gs.maybe_handle(uid, "mau ajukan izin pemakaian tanah"))
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.CONFIRMING_START)
            reply = _run(gs.maybe_handle(uid, "tolong ajukan izin lingkungan"))
            self.assertIn("MULAI BARU", reply.upper())
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.CONFIRMING_START)

    def test_normal_name_answer_is_recorded_and_advances(self):
        # Conservative classifier: a normal full name is NOT a new-intent, so it
        # IS recorded and the flow advances to NIK.
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)):
            uid = "wa-a3"
            self._to_collecting_name(uid)
            reply = _run(gs.maybe_handle(uid, "Budi Santoso"))
            sess = gs._sessions[uid]
            self.assertEqual(sess.fields.get("applicant_name"), "Budi Santoso")
            self.assertIn("NIK", reply)
            # No spurious pending intent left behind.
            self.assertIsNone(sess.pending_new_intent)

    def test_answering_after_intercept_clears_pending_and_proceeds(self):
        # After the intercept offer, if the citizen answers the actual question
        # (instead of MULAI BARU), proceed normally and clear the pending flag.
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)):
            uid = "wa-a4"
            self._to_collecting_name(uid)
            _run(gs.maybe_handle(uid, "saya mau ajukan izin PKPP"))   # intercepted
            self.assertEqual(gs._sessions[uid].pending_new_intent,
                             "saya mau ajukan izin PKPP")
            reply = _run(gs.maybe_handle(uid, "Budi Santoso"))        # real answer
            sess = gs._sessions[uid]
            self.assertEqual(sess.fields.get("applicant_name"), "Budi Santoso")
            self.assertIsNone(sess.pending_new_intent)
            self.assertIn("NIK", reply)


class TestMulaiBaru(_GuidedFlowBase):
    """MULAI BARU clears the in-progress form and re-resolves the new licence
    from the stashed intercepted intent; with no pending intent it just cancels
    and asks for a licence name."""

    def test_mulai_baru_restarts_with_stashed_new_licence(self):
        # First resolver call returns Izin Pemakaian Tanah; the restart call
        # (with the stashed "PKPP" text) returns Izin PKPP. side_effect lets us
        # return different matches per call.
        resolver = AsyncMock(side_effect=[_ONE_MATCH, _OTHER_MATCH])
        reqs = AsyncMock(side_effect=[_REQUIREMENTS, _OTHER_REQUIREMENTS])
        with patch("services.license_resolver.resolve_license_intent", resolver), \
             patch("services.siap_tools.siap_get_requirements", reqs):
            uid = "wa-a5"
            # Start a Pemakaian-Tanah form, advance into field collection.
            _run(gs.maybe_handle(uid, "mau ajukan izin pemakaian tanah"))
            _run(gs.maybe_handle(uid, "SIAP"))
            old_sess = gs._sessions[uid]
            self.assertEqual(old_sess.license_id, 42)

            # Mid-form new intent → intercepted + stashed.
            _run(gs.maybe_handle(uid, "saya mau ajukan izin PKPP"))
            self.assertEqual(gs._sessions[uid].pending_new_intent,
                             "saya mau ajukan izin PKPP")

            # MULAI BARU → old form gone, NEW licence resolved from the stash.
            reply = _run(gs.maybe_handle(uid, "MULAI BARU"))
            new_sess = gs._sessions[uid]
            self.assertEqual(new_sess.license_id, 99)
            self.assertEqual(new_sess.license_name, "Izin PKPP")
            # Fresh session: previous applicant fields are gone.
            self.assertEqual(new_sess.fields, {})
            self.assertIsNone(new_sess.pending_new_intent)
            self.assertIn("Izin PKPP", reply)
            # The restart resolved from the stashed PKPP text, not the original.
            resolver.assert_awaited_with("saya mau ajukan izin PKPP")

    def test_mulai_baru_without_pending_cancels_and_asks_for_name(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)):
            uid = "wa-a6"
            _run(gs.maybe_handle(uid, "mau ajukan izin pemakaian tanah"))
            _run(gs.maybe_handle(uid, "SIAP"))
            # No new-intent stashed → MULAI BARU behaves like BATAL + re-prompt.
            reply = _run(gs.maybe_handle(uid, "MULAI BARU"))
            self.assertFalse(_run(gs.has_active_session(uid)))
            self.assertIn("nama izin", reply.lower())


class TestApplicantNameIntentGuard(unittest.TestCase):
    """FIX A defense-in-depth: _v_name rejects a value that reads as a
    new-submission intent (so an intent phrase can never be stored as a name),
    while accepting ordinary names."""

    def test_rejects_ajukan_izin_value(self):
        for bad in (
            "saya mau ajukan izin PKPP",
            "ajukan izin lingkungan",
            "tolong ajukan izin pemakaian tanah",
        ):
            ok, msg = gs._v_name(bad)
            self.assertFalse(ok, f"should reject: {bad!r}")
            self.assertIn("nama lengkap sesuai KTP", msg)

    def test_accepts_ordinary_names(self):
        for good in ("Budi Santoso", "Sri Wahyuni", "Andi"):
            ok, cleaned = gs._v_name(good)
            self.assertTrue(ok, f"should accept: {good!r}")
            self.assertEqual(cleaned, good)


class TestFieldCollectedLogHasNoPII(_GuidedFlowBase):
    """FIX B: the 'Guided-submission field collected' log line must NOT carry
    the field VALUE — only the field name + a length indicator. Asserted two
    ways: (1) the captured log text never contains the value; (2) the logging
    call's format string has no `value=%s` placeholder."""

    def test_collected_log_omits_value(self):
        import io
        import logging

        log = logging.getLogger("bima_ai.guided_submission")
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.INFO)
        log.addHandler(handler)
        old_level = log.level
        log.setLevel(logging.INFO)
        try:
            with patch("services.license_resolver.resolve_license_intent",
                       new=AsyncMock(return_value=_ONE_MATCH)), \
                 patch("services.siap_tools.siap_get_requirements",
                       new=AsyncMock(return_value=_REQUIREMENTS)):
                uid = "wa-a7"
                _run(gs.maybe_handle(uid, "mau ajukan izin pemakaian tanah"))
                _run(gs.maybe_handle(uid, "SIAP"))
                # Distinctive name + NIK we can grep for in the log buffer.
                _run(gs.maybe_handle(uid, "Zulfikar Rahmananto"))
                _run(gs.maybe_handle(uid, "3374019988776655"))
        finally:
            log.removeHandler(handler)
            log.setLevel(old_level)

        contents = buf.getvalue()
        # The field-collected line was emitted...
        self.assertIn("Guided-submission field collected", contents)
        self.assertIn("field=applicant_name", contents)
        self.assertIn("field=nik", contents)
        # ...but NEITHER the applicant name NOR the NIK value appears anywhere.
        self.assertNotIn("Zulfikar Rahmananto", contents)
        self.assertNotIn("3374019988776655", contents)
        # The non-PII length indicator is present instead of the value.
        self.assertIn("value_len=", contents)

    def test_log_format_string_has_no_value_placeholder(self):
        # Static guard: the source format string must not interpolate a value.
        src = Path(gs.__file__).read_text(encoding="utf-8")
        self.assertIn("Guided-submission field collected", src)
        # The old leaky format had "value=%s"; assert it is gone.
        self.assertNotIn("field collected | user=%s | field=%s | value=%s", src)


class TestNoEmojiInGuidedMessages(_GuidedFlowBase):
    """FIX B — the key citizen-facing guided/scoring messages carry no emoji."""

    # Emoji ranges that must never appear in a citizen reply (pictographs,
    # symbols, dingbats, flags, keycap combiner, variation selector).
    _EMOJI = re.compile(
        "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
        "\U0001F1E6-\U0001F1FF\U0000FE0F\U000020E3\U00002190-\U000021FF]"
    )

    def _assert_clean(self, text):
        found = self._EMOJI.findall(text or "")
        self.assertEqual(found, [], f"emoji leaked: {found!r} in {text!r}")

    def test_lock_review_score_and_success_are_emoji_free(self):
        submit_result = {"ok": True, "configured": True,
                         "request_id": 1, "ticket": "000088002"}
        mock_client = MagicMock()
        mock_client.is_configured.return_value = True
        mock_client.create_request = AsyncMock(return_value=submit_result)
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)), \
             patch("services.citizen_scorer.score_session_documents",
                   new=AsyncMock(return_value=_suitability_result(96))), \
             patch("services.siap_submission_client.get_siap_submission_client",
                   return_value=mock_client):
            uid = "wa-e1"
            self._assert_clean(_run(gs.maybe_handle(uid, "mau ajukan izin pemakaian tanah")))
            self._assert_clean(_run(gs.maybe_handle(uid, "SIAP")))
            self._assert_clean(_run(gs.maybe_handle(uid, "Budi Santoso")))
            self._assert_clean(_run(gs.maybe_handle(uid, "3374012345678901")))
            self._assert_clean(_run(gs.maybe_handle(uid, "Warung Budi")))
            self._assert_clean(_run(gs.maybe_handle(uid, "081234567890")))   # doc prompt
            self._assert_clean(_run(gs.handle_inbound_documents(uid, [_doc()])))  # count
            self._assert_clean(_run(gs.maybe_handle(uid, "SELESAI")))        # score+review
            self._assert_clean(_run(gs.maybe_handle(uid, "ya")))            # success+ticket

    def test_cancel_message_is_emoji_free(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)):
            uid = "wa-e2"
            _run(gs.maybe_handle(uid, "mau ajukan izin pemakaian tanah"))
            self._assert_clean(_run(gs.maybe_handle(uid, "batal")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
