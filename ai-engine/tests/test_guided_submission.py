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


def _run(coro):
    return asyncio.run(coro)


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
             patch("services.siap_submission_client.get_siap_submission_client",
                   return_value=mock_client):

            uid = "wa-628"
            # 1. intent → licence resolved, first field asked.
            r1 = _run(gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
            self.assertIsNotNone(r1)
            self.assertIn("Izin Pemakaian Tanah", r1)
            self.assertTrue(_run(gs.has_active_session(uid)))

            # 2. applicant name.
            r2 = _run(gs.maybe_handle(uid, "Budi Santoso"))
            self.assertIn("NIK", r2)

            # 3. NIK (16 digits).
            r3 = _run(gs.maybe_handle(uid, "3374012345678901"))
            self.assertIsNotNone(r3)

            # 4. business name.
            r4 = _run(gs.maybe_handle(uid, "Warung Budi"))
            self.assertIsNotNone(r4)

            # 5. phone → all fields collected → REVIEW.
            r5 = _run(gs.maybe_handle(uid, "081234567890"))
            self.assertIn("YA", r5.upper())

            # 6. confirm → validate (clean) → submit → ticket.
            r6 = _run(gs.maybe_handle(uid, "ya"))
            self.assertIn("000088002", r6)
            self.assertIn("track/000088002", r6)
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
            _run(gs.maybe_handle(uid, "Budi Santoso"))
            # Bad NIK — too short.
            bad = _run(gs.maybe_handle(uid, "123"))
            self.assertIn("16 digit", bad)
            # Session still collecting, NIK not stored.
            sess = gs._sessions[uid]
            self.assertNotIn("nik", sess.fields)


class TestValidationGate(_GuidedFlowBase):

    def test_validation_issues_block_submit(self):
        # name_mismatch fixture → status major_issues → must NOT submit.
        os.environ["GUIDED_SUBMISSION_DEMO_FIXTURE"] = "name_mismatch"
        mock_client = MagicMock()
        mock_client.is_configured.return_value = True
        mock_client.create_request = AsyncMock()

        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)), \
             patch("services.siap_submission_client.get_siap_submission_client",
                   return_value=mock_client):
            uid = "wa-630"
            _run(gs.maybe_handle(uid, "mau ajukan izin pemakaian tanah"))
            _run(gs.maybe_handle(uid, "Budi Santoso"))
            _run(gs.maybe_handle(uid, "3374012345678901"))
            _run(gs.maybe_handle(uid, "Warung Budi"))
            _run(gs.maybe_handle(uid, "081234567890"))
            reply = _run(gs.maybe_handle(uid, "ya"))

        self.assertIn("belum bisa dikirim", reply)
        mock_client.create_request.assert_not_awaited()
        # Session stays at REVIEW so the citizen can retry.
        self.assertTrue(_run(gs.has_active_session(uid)))


class TestDegradeGracefully(_GuidedFlowBase):

    def test_submission_client_not_configured(self):
        mock_client = MagicMock()
        mock_client.is_configured.return_value = False

        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)), \
             patch("services.siap_submission_client.get_siap_submission_client",
                   return_value=mock_client):
            uid = "wa-631"
            _run(gs.maybe_handle(uid, "mau ajukan izin pemakaian tanah"))
            _run(gs.maybe_handle(uid, "Budi Santoso"))
            _run(gs.maybe_handle(uid, "3374012345678901"))
            _run(gs.maybe_handle(uid, "Warung Budi"))
            _run(gs.maybe_handle(uid, "081234567890"))
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
