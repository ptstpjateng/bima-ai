"""
Tests for the officer chat bridge — `services/officer_bridge.py`.

Run standalone (no pytest — matches tests/test_guided_submission.py):

    python -m tests.test_officer_bridge     # from ai-engine/
    python tests/test_officer_bridge.py     # also works

Covers:
  * Feature flag off → notify is a no-op, reply bridge returns None.
  * notify_officer_of_submission — registers a session + sends the brief on
    the right channel (WhatsApp preferred, Telegram fallback), with the score
    projected into the copilot validation shape.
  * _score_to_validation — SuitabilityResult issues → copilot issue shape;
    flattened fallback; None when no score.
  * maybe_handle_officer_reply — routes into the copilot with the injected
    score + in-session documents; persists rolling history; returns the reply.
  * No active session → friendly "nothing queued" message (not None, so the
    officer's text never leaks into the citizen AI).
  * Copilot crash → polite apology, never None.

No real network, no real Gemini. Copilot + senders are patched.
"""

from __future__ import annotations

import asyncio
import sys
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
_ensure_stub("httpx")

# Stub the heavy copilot module so officer_bridge's local
# `from services.agents.officer_copilot import get_copilot` resolves without
# pulling chromadb / rag_service. Tests override get_copilot per-case.
_ensure_stub("services.agents.officer_copilot", {"get_copilot": lambda: None})

from services import officer_bridge as ob  # noqa: E402
from services.agents import officer_copilot as oc  # noqa: E402
from services.agents.suitability_judge import (  # noqa: E402
    CompletenessSection,
    Issue,
    SuitabilityResult,
)


def _run(coro):
    return asyncio.run(coro)


class _Doc:
    def __init__(self, file_id, claimed_type, filename, mime_type, content):
        self.file_id = file_id
        self.claimed_type = claimed_type
        self.filename = filename
        self.mime_type = mime_type
        self.content = content


def _score_dict(percent=72, with_result=True):
    issues = [
        Issue(id="completeness:missing:NPWP", severity="critical", title="Dokumen wajib belum diunggah: NPWP", detail="d"),
        Issue(id="type:mismatch:doc-1", severity="high", title="Label dokumen tidak cocok: ktp.jpg", detail="d"),
    ]
    result = SuitabilityResult(
        completeness=CompletenessSection(score=0.66, missing=["NPWP"], required=["KTP", "NIB", "NPWP"]),
        type_correctness=[],
        suitability=[],
        compatibility_findings=[],
        overall_suitability_score=percent / 100.0,
        issues=issues,
    )
    return {
        "ok": False,
        "status": "needs_fix",
        "score_percent": percent,
        "summary": "ringkasan",
        "message": "pesan skor",
        "issues": [{"severity": i.severity, "message": i.title} for i in issues],
        "result": result if with_result else None,
    }


def _reset_env_and_sessions(monkeypatch_env: dict):
    ob._sessions.clear()


class TestFeatureFlagOff(unittest.TestCase):
    def setUp(self):
        ob._sessions.clear()

    def test_notify_noop_when_disabled(self):
        with patch.dict("os.environ", {"BIMA_OFFICER_NOTIFY_ENABLED": "false"}, clear=False):
            sent = _run(ob.notify_officer_of_submission(
                ticket="123", request_id=9, license_id=358,
                license_name="Izin Penelitian", applicant_name="Budi",
                score=_score_dict(), documents=[],
            ))
        self.assertFalse(sent)
        self.assertEqual(len(ob._sessions), 0)

    def test_reply_none_when_disabled(self):
        with patch.dict("os.environ", {"BIMA_OFFICER_NOTIFY_ENABLED": "false"}, clear=False):
            out = _run(ob.maybe_handle_officer_reply(
                channel=ob.CHANNEL_WHATSAPP, channel_id="628111", message="halo",
            ))
        self.assertIsNone(out)


class TestScoreToValidation(unittest.TestCase):
    def test_rich_path_maps_result_issues(self):
        v = ob._score_to_validation(_score_dict(percent=80))
        self.assertEqual(v["score_percent"], 80)
        self.assertEqual(len(v["issues"]), 2)
        self.assertEqual(v["issues"][0]["severity"], "critical")
        # field carries the issue id; message carries the title.
        self.assertIn("NPWP", v["issues"][0]["field"])
        self.assertIn("NPWP", v["issues"][0]["message"])

    def test_flattened_fallback(self):
        v = ob._score_to_validation(_score_dict(percent=55, with_result=False))
        self.assertEqual(v["score_percent"], 55)
        self.assertEqual(len(v["issues"]), 2)
        self.assertEqual(v["issues"][0]["severity"], "critical")

    def test_none_when_no_score(self):
        self.assertIsNone(ob._score_to_validation(None))
        self.assertIsNone(ob._score_to_validation({}))


class TestNotify(unittest.TestCase):
    def setUp(self):
        ob._sessions.clear()

    def test_registers_session_and_sends_wa(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        docs = [_Doc("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"bytes")]
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ob, "_send", new=AsyncMock(return_value=True)) as send:
                sent = _run(ob.notify_officer_of_submission(
                    ticket="000123456", request_id=42, license_id=358,
                    license_name="Izin Penelitian", applicant_name="Budi Santoso",
                    score=_score_dict(percent=72), documents=docs,
                ))
        self.assertTrue(sent)
        # session registered under the WA number.
        sess = ob._sessions.get("628999000111")
        self.assertIsNotNone(sess)
        self.assertEqual(sess.ticket, "000123456")
        self.assertEqual(sess.channel, ob.CHANNEL_WHATSAPP)
        self.assertEqual(sess.validation["score_percent"], 72)
        self.assertIn("doc-1", sess.documents)
        # brief sent on the WA channel.
        send.assert_awaited_once()
        args, _ = send.call_args
        self.assertEqual(args[0], ob.CHANNEL_WHATSAPP)
        self.assertEqual(args[1], "628999000111")
        brief = args[2]
        self.assertIn("000123456", brief)
        self.assertIn("72%", brief)
        # applicant name MASKED in the brief — full name must not appear.
        self.assertNotIn("Budi Santoso", brief)

    def test_telegram_fallback_when_no_wa(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "",
            "BIMA_OFFICER_TG_CHAT": "555000",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ob, "_send", new=AsyncMock(return_value=True)) as send:
                sent = _run(ob.notify_officer_of_submission(
                    ticket="789", request_id=1, license_id=358,
                    license_name="X", applicant_name="A", score=None, documents=[],
                ))
        self.assertTrue(sent)
        self.assertIn("555000", ob._sessions)
        args, _ = send.call_args
        self.assertEqual(args[0], ob.CHANNEL_TELEGRAM)

    def test_no_channel_configured_is_noop(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        with patch.dict("os.environ", env, clear=False):
            sent = _run(ob.notify_officer_of_submission(
                ticket="1", request_id=1, license_id=358,
                license_name="X", applicant_name="A", score=None, documents=[],
            ))
        self.assertFalse(sent)
        self.assertEqual(len(ob._sessions), 0)


class TestReplyBridge(unittest.TestCase):
    def setUp(self):
        ob._sessions.clear()

    def _arm_session(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        docs = [_Doc("doc-1", "proposal", "proposal.pdf", "application/pdf", b"pdfbytes")]
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ob, "_send", new=AsyncMock(return_value=True)):
                _run(ob.notify_officer_of_submission(
                    ticket="000123456", request_id=42, license_id=358,
                    license_name="Izin Penelitian", applicant_name="Budi",
                    score=_score_dict(percent=72), documents=docs,
                ))
        return env

    def test_routes_into_copilot_with_score_and_docs(self):
        env = self._arm_session()

        copilot = type("C", (), {})()
        chat_mock = AsyncMock(return_value={
            "reply": "Berkas ini memiliki 2 temuan.",
            "tool_calls": [{"name": "get_validation_summary", "args": {}, "result_preview": ""}],
            "history": [
                {"role": "user", "text": "ringkas temuan"},
                {"role": "model", "text": "Berkas ini memiliki 2 temuan."},
            ],
        })
        copilot.chat = chat_mock

        with patch.dict("os.environ", env, clear=False):
            with patch.object(oc, "get_copilot", return_value=copilot):
                out = _run(ob.maybe_handle_officer_reply(
                    channel=ob.CHANNEL_WHATSAPP,
                    channel_id="628999000111",
                    message="ringkas temuan",
                ))

        self.assertIn("2 temuan", out)
        chat_mock.assert_awaited_once()
        _, kwargs = chat_mock.call_args
        self.assertEqual(kwargs["ticket"], "000123456")
        self.assertEqual(kwargs["validation"]["score_percent"], 72)
        # In-session document bytes injected for get_doc_summary.
        self.assertIn("doc-1", kwargs["documents"])
        self.assertEqual(kwargs["documents"]["doc-1"]["content"], b"pdfbytes")
        # rolling history persisted.
        sess = ob._sessions["628999000111"]
        self.assertEqual(len(sess.history), 2)

    def test_no_active_session_returns_friendly_message(self):
        # Officer number configured + enabled, but no session armed.
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        with patch.dict("os.environ", env, clear=False):
            out = _run(ob.maybe_handle_officer_reply(
                channel=ob.CHANNEL_WHATSAPP,
                channel_id="628999000111",
                message="halo",
            ))
        self.assertIsNotNone(out)
        self.assertIn("Belum ada berkas", out)

    def test_non_officer_returns_none(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        with patch.dict("os.environ", env, clear=False):
            out = _run(ob.maybe_handle_officer_reply(
                channel=ob.CHANNEL_WHATSAPP,
                channel_id="628000111222",   # a citizen, not the officer
                message="apa itu izin penelitian",
            ))
        self.assertIsNone(out)

    def test_copilot_crash_returns_apology_not_none(self):
        env = self._arm_session()
        with patch.dict("os.environ", env, clear=False):
            with patch.object(oc, "get_copilot", side_effect=RuntimeError("boom")):
                out = _run(ob.maybe_handle_officer_reply(
                    channel=ob.CHANNEL_WHATSAPP,
                    channel_id="628999000111",
                    message="ringkas",
                ))
        self.assertIsNotNone(out)
        self.assertIn("gangguan", out.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
