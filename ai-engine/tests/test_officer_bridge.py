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
from unittest.mock import AsyncMock, MagicMock, patch

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

# Stub the REAL officer_copilot module's heavy deps (chromadb via rag_service,
# SIAP clients) so it imports cleanly — rather than stubbing the whole
# officer_copilot module. Stubbing the module itself would poison sys.modules
# for any later test in the same process (e.g. test_officer_copilot_docsend),
# which needs the real `_doc_context` / `send_document` symbols. These are the
# same fine-grained stubs test_officer_copilot_docsend installs, so the two
# suites share one consistent (real) copilot module. Tests override
# get_copilot per-case, so the real module's networked tools never run.
_ensure_stub("services.rag_service", {"query_regulations": lambda *a, **k: []})
_ensure_stub("services.siap_client", {"get_siap_client": lambda: None})
_ensure_stub("services.siap_tools", {"siap_get_status_timeline": None})
_ensure_stub("services.siap_write_client", {"get_siap_write_client": lambda: None})

from services import officer_bridge as ob  # noqa: E402
from services.agents import officer_copilot as oc  # noqa: E402
from services.agents.suitability_judge import (  # noqa: E402
    CompletenessSection,
    Issue,
    SuitabilityResult,
    TypeCorrectnessFinding,
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


def _score_dict(percent=72, with_result=True, with_type_findings=False):
    issues = [
        Issue(id="completeness:missing:NPWP", severity="critical", title="Dokumen wajib belum diunggah: NPWP", detail="d"),
        Issue(id="type:mismatch:doc-1", severity="high", title="Label dokumen tidak cocok: ktp.jpg", detail="d"),
    ]
    type_correctness = []
    if with_type_findings:
        type_correctness = [
            TypeCorrectnessFinding(
                file="ktp.jpg", file_id="doc-1", claimed_type="KTP",
                detected_type="KTP", confidence=0.96, matches=True,
                has_meterai=None,
            ),
            TypeCorrectnessFinding(
                file="surat_pesanan.pdf", file_id="doc-2",
                claimed_type="Surat_Pesanan", detected_type="Surat_Pesanan",
                confidence=0.88, matches=True, has_meterai=True,
            ),
        ]
    result = SuitabilityResult(
        completeness=CompletenessSection(score=0.66, missing=["NPWP"], required=["KTP", "NIB", "NPWP"]),
        type_correctness=type_correctness,
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


class TestEvidencePiiMaskedForOfficer(unittest.TestCase):
    """PII leak fix — the SAME SuitabilityResult that feeds the citizen score
    also feeds the officer-facing validation dict (brief + copilot
    get_validation_summary). NIK/phone quoted from documents into issue
    titles/messages/summary MUST be masked before it reaches the officer."""

    _NIK = "3327080511740081"
    _PHONE = "085117557091"

    def _score_with_pii_issue(self):
        # An issue whose title echoes a Gemini Vision evidence quote with a NIK
        # (this is exactly the shape that leaked: evidence → issue text).
        issues = [
            Issue(
                id=f"suitability:mismatch:doc-1:NIK {self._NIK}",
                severity="high",
                title=f"Dokumen tidak sesuai: NIK {self._NIK} tidak cocok",
                detail=f"Bukti: NIK {self._NIK} berbeda dari NIB.",
            ),
        ]
        result = SuitabilityResult(
            completeness=CompletenessSection(score=1.0, missing=[], required=["KTP"]),
            type_correctness=[],
            suitability=[],
            compatibility_findings=[],
            overall_suitability_score=0.7,
            issues=issues,
        )
        return {
            "ok": False,
            "status": "needs_fix",
            "score_percent": 70,
            "summary": f"Pemohon dengan NIK {self._NIK} perlu dicek.",
            "message": "m",
            "issues": [{"severity": i.severity, "message": i.title} for i in issues],
            "result": result,
        }

    def test_rich_path_masks_nik_in_message_field_and_summary(self):
        v = ob._score_to_validation(self._score_with_pii_issue())
        # Full NIK must NOT appear in ANY string of the projected validation.
        blob = repr(v)
        self.assertNotIn(self._NIK, blob)
        # The masked form is present in the issue message + the summary.
        self.assertIn("************81", v["issues"][0]["message"])
        self.assertIn("************81", v["summary"])
        # `field` carries the issue id which embedded the NIK — also masked.
        self.assertNotIn(self._NIK, v["issues"][0]["field"])

    def test_flattened_fallback_masks_phone(self):
        score = {
            "score_percent": 60,
            "status": "needs_fix",
            "summary": "ringkasan",
            "issues": [
                {"severity": "high", "message": f"hubungi {self._PHONE} untuk klarifikasi"},
            ],
            "result": None,
        }
        v = ob._score_to_validation(score)
        self.assertNotIn(self._PHONE, repr(v))
        self.assertIn("08", v["issues"][0]["message"])

    def test_non_pii_message_unchanged(self):
        score = {
            "score_percent": 90, "status": "ready", "summary": "berkas lengkap",
            "issues": [{"severity": "low", "message": "alamat usaha jelas"}],
            "result": None,
        }
        v = ob._score_to_validation(score)
        self.assertEqual(v["issues"][0]["message"], "alamat usaha jelas")
        self.assertEqual(v["summary"], "berkas lengkap")

    def test_notify_stores_masked_validation_in_session(self):
        # End-to-end: the masked validation is what lands in the officer
        # session (and therefore what the copilot/brief see).
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        ob._sessions.clear()
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ob, "_send", new=AsyncMock(return_value=True)):
                _run(ob.notify_officer_of_submission(
                    ticket="000123456", request_id=42, license_id=358,
                    license_name="Izin Penelitian", applicant_name="Budi",
                    score=self._score_with_pii_issue(), documents=[],
                ))
        sess = ob._sessions.get("628999000111")
        self.assertIsNotNone(sess)
        self.assertNotIn(self._NIK, repr(sess.validation))
        self.assertIn("************81", repr(sess.validation))


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
            # Force the free-form fallback (template off) so we can assert the
            # brief content + masking here; the template path is its own test.
            with patch.object(ob, "_OFFICER_TEMPLATE_NAME", ""), \
                    patch.object(ob, "_send", new=AsyncMock(return_value=True)) as send:
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

    def test_wa_notify_uses_approved_template(self):
        # Default WhatsApp officer-notify goes out as the APPROVED template so it
        # reaches a COLD officer number (bypassing the 24h window / 131047). Vars
        # are izin/tiket/skor only — never PII.
        import types as _types
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        calls: list = []

        async def _fake_send_template(**kw):
            calls.append(kw)
            return True

        fake_wt = _types.ModuleType("services.whatsapp_template")
        fake_wt.send_template = _fake_send_template
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ob, "_OFFICER_TEMPLATE_NAME", "bima_officer_new_submission"):
                with patch.dict("sys.modules", {"services.whatsapp_template": fake_wt}):
                    sent = _run(ob.notify_officer_of_submission(
                        ticket="000123456", request_id=42, license_id=459,
                        license_name="PKPP", applicant_name="Budi Santoso",
                        score=_score_dict(percent=92), documents=[],
                    ))
        self.assertTrue(sent)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["template_name"], "bima_officer_new_submission")
        self.assertEqual(list(calls[0]["body_params"]), ["PKPP", "000123456", "92%"])
        # No PII in template variables.
        self.assertNotIn("Budi Santoso", " ".join(map(str, calls[0]["body_params"])))
        # Session still registered so the officer's reply reaches the copilot.
        self.assertIsNotNone(ob._sessions.get("628999000111"))

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

    def _run_turn_with_tool_calls(self, env, tool_calls):
        copilot = type("C", (), {})()
        copilot.chat = AsyncMock(return_value={
            "reply": "Berkas diteruskan.",
            "tool_calls": tool_calls,
            "history": [{"role": "user", "text": "ya"},
                        {"role": "model", "text": "Berkas diteruskan."}],
        })
        with patch.dict("os.environ", env, clear=False):
            with patch.object(oc, "get_copilot", return_value=copilot):
                return _run(ob.maybe_handle_officer_reply(
                    channel=ob.CHANNEL_WHATSAPP,
                    channel_id="628999000111",
                    message="ya",
                ))

    def test_session_cleared_after_confirmed_forward(self):
        # Fix 3: a successful CONFIRMED forward_case clears the session so a
        # stale ticket can't hijack the officer's next message.
        env = self._arm_session()
        self.assertIn("628999000111", ob._sessions)
        out = self._run_turn_with_tool_calls(env, [{
            "name": "forward_case",
            "args": {"ticket": "000123456", "confirmed": True},
            "result_preview": '{"executed": true, "ok": true, "result": {"id": 7}}',
        }])
        self.assertIsNotNone(out)
        self.assertNotIn("628999000111", ob._sessions)

    def test_session_cleared_after_confirmed_decision(self):
        env = self._arm_session()
        out = self._run_turn_with_tool_calls(env, [{
            "name": "record_decision",
            "args": {"ticket": "000123456", "decision": "approved",
                     "notes": "ok", "confirmed": True},
            "result_preview": '{"executed":true,"ok":true,"result":{}}',
        }])
        self.assertIsNotNone(out)
        self.assertNotIn("628999000111", ob._sessions)

    def test_session_kept_on_draft_forward(self):
        # A draft (confirmed not true, executed false) must NOT clear the session.
        env = self._arm_session()
        self._run_turn_with_tool_calls(env, [{
            "name": "forward_case",
            "args": {"ticket": "000123456"},   # no confirmed
            "result_preview": '{"executed": false, "needs_confirmation": true}',
        }])
        self.assertIn("628999000111", ob._sessions)

    def test_session_kept_on_failed_confirmed_write(self):
        # Confirmed but the write FAILED (ok false) → keep the session so the
        # officer can retry; the case did not leave the desk.
        env = self._arm_session()
        self._run_turn_with_tool_calls(env, [{
            "name": "forward_case",
            "args": {"ticket": "000123456", "confirmed": True},
            "result_preview": '{"executed": true, "ok": false, "note": "SIAP error"}',
        }])
        self.assertIn("628999000111", ob._sessions)

    def test_session_kept_on_readonly_tool(self):
        # A read-only tool turn keeps the session (the existing follow-up Q&A
        # behaviour) — only a confirmed successful write closes it.
        env = self._arm_session()
        self._run_turn_with_tool_calls(env, [{
            "name": "get_validation_summary", "args": {}, "result_preview": "{}",
        }])
        self.assertIn("628999000111", ob._sessions)


class TestCaseClosedDetector(unittest.TestCase):
    """Unit cover for the _case_was_closed helper that gates the session clear."""

    def test_true_only_for_confirmed_successful_write(self):
        self.assertTrue(ob._case_was_closed([{
            "name": "forward_case", "args": {"confirmed": True},
            "result_preview": '{"executed": true, "ok": true}',
        }]))
        self.assertTrue(ob._case_was_closed([{
            "name": "record_decision", "args": {"confirmed": True},
            "result_preview": '{"ok":true,"executed":true}',
        }]))

    def test_false_for_draft_failed_unconfirmed_or_other_tools(self):
        self.assertFalse(ob._case_was_closed([]))
        self.assertFalse(ob._case_was_closed([{
            "name": "forward_case", "args": {},
            "result_preview": '{"executed": false}',
        }]))
        self.assertFalse(ob._case_was_closed([{
            "name": "forward_case", "args": {"confirmed": True},
            "result_preview": '{"executed": true, "ok": false}',
        }]))
        self.assertFalse(ob._case_was_closed([{
            "name": "get_doc_summary", "args": {"confirmed": True},
            "result_preview": '{"executed": true, "ok": true}',
        }]))


class TestDocumentsDigest(unittest.TestCase):
    """Task F — the compact per-doc read digest is derived from the rich
    SuitabilityResult, threaded onto the validation dict + session, and
    survives a Redis encode/decode round-trip."""

    def test_digest_derived_from_type_correctness(self):
        digest = ob._documents_digest(_score_dict(with_type_findings=True))
        self.assertEqual(len(digest), 2)
        self.assertEqual(digest[0]["detected_type"], "KTP")
        self.assertEqual(digest[0]["matches"], True)
        # meterai visibility is captured per doc.
        self.assertEqual(digest[1]["has_meterai"], True)
        self.assertEqual(digest[1]["detected_type"], "Surat_Pesanan")
        self.assertEqual(digest[1]["confidence"], 0.88)

    def test_digest_empty_without_result(self):
        self.assertEqual(ob._documents_digest(_score_dict(with_result=False)), [])
        self.assertEqual(ob._documents_digest(None), [])
        # rich result but no type_correctness rows → empty.
        self.assertEqual(ob._documents_digest(_score_dict(with_type_findings=False)), [])

    def test_digest_survives_encode_decode_round_trip(self):
        sess = ob.OfficerCaseSession(
            channel_id="628999000111",
            channel=ob.CHANNEL_WHATSAPP,
            ticket="000123456",
            request_id=42,
            license_name="Izin Penelitian",
            validation={"score_percent": 72, "status": "minor_issues",
                        "summary": "s", "issues": []},
            documents={"doc-1": {"filename": "ktp.jpg",
                                 "mime_type": "image/jpeg",
                                 "content": b"bytes", "claimed_type": "KTP"}},
            documents_digest=ob._documents_digest(_score_dict(with_type_findings=True)),
        )
        blob = ob._encode_officer_session(sess)
        restored = ob._decode_officer_session(blob)
        self.assertEqual(len(restored.documents_digest), 2)
        self.assertEqual(restored.documents_digest[0]["detected_type"], "KTP")
        self.assertEqual(restored.documents_digest[1]["has_meterai"], True)
        self.assertEqual(restored.documents_digest[1]["detected_type"], "Surat_Pesanan")

    def test_digest_carries_file_id_for_cross_reference(self):
        digest = ob._documents_digest(_score_dict(with_type_findings=True))
        self.assertEqual(digest[0]["file_id"], "doc-1")
        self.assertEqual(digest[1]["file_id"], "doc-2")

    def test_doc_context_enriched_with_detected_type(self):
        # _documents_for_copilot cross-references the digest's detected_type onto
        # each doc so the copilot can resolve the officer's "KTP" even when the
        # citizen mislabelled the upload.
        docs = [_Doc("doc-1", "identitas", "scan.jpg", "image/jpeg", b"bytes")]
        digest = ob._documents_digest(_score_dict(with_type_findings=True))
        out = ob._documents_for_copilot(docs, digest)
        self.assertEqual(out["doc-1"]["claimed_type"], "identitas")
        self.assertEqual(out["doc-1"]["detected_type"], "KTP")

    def test_doc_context_detected_type_survives_redis_round_trip(self):
        sess = ob.OfficerCaseSession(
            channel_id="628999000111",
            channel=ob.CHANNEL_WHATSAPP,
            ticket="000123456",
            documents={"doc-1": {"filename": "scan.jpg",
                                 "mime_type": "image/jpeg",
                                 "content": b"bytes", "claimed_type": "identitas",
                                 "detected_type": "KTP"}},
        )
        restored = ob._decode_officer_session(ob._encode_officer_session(sess))
        self.assertEqual(restored.documents["doc-1"]["detected_type"], "KTP")
        self.assertEqual(restored.documents["doc-1"]["content"], b"bytes")

    def test_small_doc_bytes_survive_redis_round_trip(self):
        # Edge 3: a normally-sized doc's bytes must survive encode→decode so
        # send_document works after a restart. (The proof the headline "kirim
        # KTP" flow is durable.)
        sess = ob.OfficerCaseSession(
            channel_id="628999000111",
            channel=ob.CHANNEL_WHATSAPP,
            ticket="000123456",
            documents={
                "doc-1": {"filename": "ktp.jpg", "mime_type": "image/jpeg",
                          "content": b"small-ktp-bytes", "claimed_type": "KTP",
                          "detected_type": "KTP"},
                "doc-3": {"filename": "surat_permohonan.pdf",
                          "mime_type": "application/pdf",
                          "content": b"small-perm-bytes",
                          "claimed_type": "Surat_Permohonan",
                          "detected_type": "Surat_Permohonan"},
            },
        )
        restored = ob._decode_officer_session(ob._encode_officer_session(sess))
        # BOTH docs keep their bytes — not just the first one.
        self.assertEqual(restored.documents["doc-1"]["content"], b"small-ktp-bytes")
        self.assertEqual(restored.documents["doc-3"]["content"], b"small-perm-bytes")

    def test_oversize_doc_bytes_dropped_but_metadata_and_others_survive(self):
        # Edge 3 size guard: an oversize doc's bytes are dropped from the durable
        # copy (so one huge file can't blow up the whole session's Redis SET and
        # take EVERY doc's bytes down with it), while its metadata + the digest
        # survive and OTHER docs keep their bytes intact.
        big = b"x" * (ob._MAX_REDIS_DOC_BYTES + 1)
        sess = ob.OfficerCaseSession(
            channel_id="628999000111",
            channel=ob.CHANNEL_WHATSAPP,
            ticket="000123456",
            documents={
                "doc-1": {"filename": "ktp.jpg", "mime_type": "image/jpeg",
                          "content": b"small-ktp", "claimed_type": "KTP",
                          "detected_type": "KTP"},
                "doc-big": {"filename": "scan_hires.pdf",
                            "mime_type": "application/pdf",
                            "content": big, "claimed_type": "Surat_Permohonan",
                            "detected_type": "Surat_Permohonan"},
            },
        )
        restored = ob._decode_officer_session(ob._encode_officer_session(sess))
        # Small doc: bytes intact → still sendable after a restart.
        self.assertEqual(restored.documents["doc-1"]["content"], b"small-ktp")
        # Big doc: metadata survives (so it degrades gracefully) but bytes gone.
        self.assertIn("doc-big", restored.documents)
        self.assertEqual(restored.documents["doc-big"]["filename"], "scan_hires.pdf")
        self.assertEqual(restored.documents["doc-big"]["detected_type"], "Surat_Permohonan")
        self.assertEqual(restored.documents["doc-big"]["content"], b"")

    def test_notify_threads_digest_into_validation_and_session(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        ob._sessions.clear()
        docs = [_Doc("doc-1", "KTP", "ktp.jpg", "image/jpeg", b"bytes")]
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ob, "_send", new=AsyncMock(return_value=True)):
                _run(ob.notify_officer_of_submission(
                    ticket="000123456", request_id=42, license_id=358,
                    license_name="Izin Penelitian", applicant_name="Budi",
                    score=_score_dict(percent=72, with_type_findings=True),
                    documents=docs,
                ))
        sess = ob._sessions.get("628999000111")
        self.assertIsNotNone(sess)
        # On the session field...
        self.assertEqual(len(sess.documents_digest), 2)
        # ...and threaded onto the validation dict the copilot reads.
        self.assertIn("documents_digest", sess.validation)
        self.assertEqual(sess.validation["documents_digest"][0]["detected_type"], "KTP")


class TestCompareIdentityWired(unittest.TestCase):
    """Edge 2 — the cross-doc identity comparator is a real, mode-allowed
    officer tool (so the model can call it instead of dead-ending on 'bandingkan
    NIK di KTP dengan surat permohonan')."""

    def test_compare_identity_is_a_declared_officer_tool(self):
        self.assertIn("compare_identity", oc._TOOL_DISPATCH)
        self.assertIn("compare_identity", oc._OFFICER_TOOL_NAMES)
        names = {d["name"] for d in oc._FUNCTION_DECLARATIONS}
        self.assertIn("compare_identity", names)
        # It is a READ tool — never in the confirmation-gated write set, and NOT
        # a case-closing tool (comparing identity must not clear the session).
        self.assertNotIn("compare_identity", oc._REQUIRES_CONFIRMATION)
        self.assertNotIn("compare_identity", ob._CASE_CLOSING_TOOLS)

    def test_signature_assistant_does_not_expose_compare_identity(self):
        # The signing assistant is chain-synthesis only; the doc comparator is a
        # desk-copilot tool. Keep the surfaces distinct.
        self.assertNotIn("compare_identity", oc._SIGNATURE_TOOL_NAMES)


class TestOfficerDocDelivery(unittest.TestCase):
    """Task G — when the copilot turn surfaces documents_to_send, the bridge
    hosts the bytes at /dl/{token} and sends the file on the officer's channel."""

    def setUp(self):
        ob._sessions.clear()

    def _arm_session(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        docs = [_Doc("doc-1", "KTP", "ktp_budi.jpg", "image/jpeg", b"ktp-bytes")]
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ob, "_send", new=AsyncMock(return_value=True)):
                _run(ob.notify_officer_of_submission(
                    ticket="000123456", request_id=42, license_id=358,
                    license_name="Izin Penelitian", applicant_name="Budi",
                    score=_score_dict(percent=72), documents=docs,
                ))
        return env

    def _drive_reply_with_docs_to_send(self, env, documents_to_send):
        copilot = type("C", (), {})()
        copilot.chat = AsyncMock(return_value={
            "reply": "Baik, saya kirimkan dokumen KTP ke chat ini.",
            "tool_calls": [{"name": "send_document",
                            "args": {"doc_ref": "KTP"}, "result_preview": ""}],
            "documents_to_send": documents_to_send,
            "history": [{"role": "user", "text": "kirim KTP-nya"},
                        {"role": "model", "text": "Baik, saya kirimkan dokumen KTP."}],
        })
        # Stub generated_docs (imported lazily inside _deliver_documents) and the
        # WhatsApp document sender.
        _ensure_stub("services.generated_docs")
        import services.generated_docs as gd
        store_mock = lambda content, filename: "TESTTOKEN123"  # noqa: E731
        with patch.dict("os.environ", env, clear=False):
            with patch.object(oc, "get_copilot", return_value=copilot):
                with patch.object(gd, "store", side_effect=store_mock, create=True) as store_spy:
                    with patch("services.whatsapp_sender.send_document",
                               new=AsyncMock(return_value=True)) as senddoc:
                        out = _run(ob.maybe_handle_officer_reply(
                            channel=ob.CHANNEL_WHATSAPP,
                            channel_id="628999000111",
                            message="kirim KTP-nya",
                        ))
        return out, store_spy, senddoc

    def test_delivers_requested_doc_on_whatsapp(self):
        env = self._arm_session()
        out, store_spy, senddoc = self._drive_reply_with_docs_to_send(env, ["doc-1"])

        # The reply is still returned to the officer.
        self.assertIn("kirimkan", out.lower())

        # generated_docs.store was called with the resolved doc's BYTES + name.
        store_spy.assert_called_once()
        s_args, _ = store_spy.call_args
        self.assertEqual(s_args[0], b"ktp-bytes")
        self.assertEqual(s_args[1], "ktp_budi.jpg")

        # send_document fired on WhatsApp with the /dl link + filename.
        senddoc.assert_awaited_once()
        _, kwargs = senddoc.call_args
        self.assertEqual(kwargs["recipient_phone"], "628999000111")
        self.assertEqual(kwargs["filename"], "ktp_budi.jpg")
        self.assertTrue(kwargs["link"].endswith("/dl/TESTTOKEN123"))

    def test_unknown_file_id_does_not_send(self):
        env = self._arm_session()
        out, store_spy, senddoc = self._drive_reply_with_docs_to_send(env, ["doc-nope"])
        # file_id not held → neither hosting nor sending happens.
        store_spy.assert_not_called()
        senddoc.assert_not_awaited()
        # Reply still returned.
        self.assertIsNotNone(out)

    def test_no_docs_to_send_is_noop(self):
        env = self._arm_session()
        out, store_spy, senddoc = self._drive_reply_with_docs_to_send(env, [])
        store_spy.assert_not_called()
        senddoc.assert_not_awaited()
        self.assertIsNotNone(out)


class TestSkContextThreading(unittest.TestCase):
    """Officer deputy — the session now carries license_id + applicant identity,
    survives the Redis round-trip, and the bridge threads an sk_context into the
    copilot so draft_sk can render SIAP's PKPP approval-letter template."""

    def setUp(self):
        ob._sessions.clear()

    def test_session_carries_license_id_and_identity(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ob, "_send", new=AsyncMock(return_value=True)):
                _run(ob.notify_officer_of_submission(
                    ticket="000123456", request_id=42, license_id=459,
                    license_name="Pengadaan Kapal (Pembangunan)",
                    applicant_name="Budi Santoso",
                    applicant_alamat="Jl. Laut No. 1, Tegal",
                    score=_score_dict(percent=92), documents=[],
                ))
        sess = ob._sessions.get("628999000111")
        self.assertIsNotNone(sess)
        self.assertEqual(sess.license_id, 459)
        self.assertEqual(sess.applicant_name, "Budi Santoso")
        self.assertEqual(sess.alamat, "Jl. Laut No. 1, Tegal")

    def test_license_id_and_identity_survive_redis_round_trip(self):
        sess = ob.OfficerCaseSession(
            channel_id="628999000111",
            channel=ob.CHANNEL_WHATSAPP,
            ticket="000123456",
            request_id=42,
            license_id=459,
            license_name="Pengadaan Kapal (Pembangunan)",
            applicant_name="Budi Santoso",
            alamat="Jl. Laut No. 1",
        )
        restored = ob._decode_officer_session(ob._encode_officer_session(sess))
        self.assertEqual(restored.license_id, 459)
        self.assertEqual(restored.applicant_name, "Budi Santoso")
        self.assertEqual(restored.alamat, "Jl. Laut No. 1")

    def test_reply_threads_sk_context_into_copilot(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ob, "_send", new=AsyncMock(return_value=True)):
                _run(ob.notify_officer_of_submission(
                    ticket="000123456", request_id=42, license_id=459,
                    license_name="Pengadaan Kapal (Pembangunan)",
                    applicant_name="Budi", applicant_alamat="Jl. X",
                    score=_score_dict(percent=92), documents=[],
                ))
        copilot = type("C", (), {})()
        copilot.chat = AsyncMock(return_value={
            "reply": "ok", "tool_calls": [],
            "history": [{"role": "user", "text": "draftkan SK"},
                        {"role": "model", "text": "ok"}],
        })
        with patch.dict("os.environ", env, clear=False):
            with patch.object(oc, "get_copilot", return_value=copilot):
                _run(ob.maybe_handle_officer_reply(
                    channel=ob.CHANNEL_WHATSAPP,
                    channel_id="628999000111",
                    message="draftkan SK",
                ))
        _, kwargs = copilot.chat.call_args
        sk = kwargs["sk_context"]
        self.assertEqual(sk["license_id"], 459)
        self.assertEqual(sk["ticket"], "000123456")
        self.assertEqual(sk["applicant_name"], "Budi")
        self.assertEqual(sk["alamat"], "Jl. X")
        self.assertEqual(sk["license_name"], "Pengadaan Kapal (Pembangunan)")


class TestSkDraftDelivery(unittest.TestCase):
    """When draft_sk queues an INLINE-dict doc (generated SK bytes, not an
    in-session file_id), the bridge hosts + sends it on the officer channel."""

    def setUp(self):
        ob._sessions.clear()

    def _arm_session(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ob, "_send", new=AsyncMock(return_value=True)):
                _run(ob.notify_officer_of_submission(
                    ticket="000123456", request_id=42, license_id=459,
                    license_name="Pengadaan Kapal (Pembangunan)",
                    applicant_name="Budi", applicant_alamat="Jl X",
                    score=_score_dict(percent=92), documents=[],
                ))
        return env

    def test_inline_generated_docx_is_delivered(self):
        env = self._arm_session()
        copilot = type("C", (), {})()
        copilot.chat = AsyncMock(return_value={
            "reply": "Draf SK sudah saya siapkan.",
            "tool_calls": [{"name": "draft_sk", "args": {}, "result_preview": ""}],
            "documents_to_send": [{
                "filename": "SK_PPKP_000123456.docx",
                "content": b"PKdocxbytes",
                "mime_type": ("application/vnd.openxmlformats-officedocument."
                              "wordprocessingml.document"),
            }],
            "history": [{"role": "user", "text": "draftkan SK"},
                        {"role": "model", "text": "Draf SK sudah saya siapkan."}],
        })
        _ensure_stub("services.generated_docs")
        import services.generated_docs as gd
        with patch.dict("os.environ", env, clear=False):
            with patch.object(oc, "get_copilot", return_value=copilot):
                with patch.object(gd, "store",
                                  side_effect=lambda c, f: "SKTOKEN", create=True) as store_spy:
                    with patch("services.whatsapp_sender.send_document",
                               new=AsyncMock(return_value=True)) as senddoc:
                        out = _run(ob.maybe_handle_officer_reply(
                            channel=ob.CHANNEL_WHATSAPP,
                            channel_id="628999000111",
                            message="draftkan SK",
                        ))
        self.assertIn("Draf SK", out)
        # The GENERATED bytes (not an upload doc) were hosted + sent.
        store_spy.assert_called_once()
        s_args, _ = store_spy.call_args
        self.assertEqual(s_args[0], b"PKdocxbytes")
        self.assertEqual(s_args[1], "SK_PPKP_000123456.docx")
        senddoc.assert_awaited_once()
        _, kwargs = senddoc.call_args
        self.assertEqual(kwargs["filename"], "SK_PPKP_000123456.docx")
        self.assertTrue(kwargs["link"].endswith("/dl/SKTOKEN"))


class TestDeputyPromptGrounding(unittest.TestCase):
    """The officer system prompt steers the recommend-then-execute deputy:
    recommend grounded in the validation, execute on YA via the existing
    confirm-gated writes, and offer draft_sk after a positive review — without
    inventing decision notes."""

    _P = oc._SYSTEM_PROMPT_TEMPLATE

    def test_prompt_directs_grounded_recommendation(self):
        self.assertIn("Rekomendasi", self._P)
        # Grounded in real signals, not invented.
        self.assertIn("TERUSKAN", self._P)
        self.assertIn("TOLAK", self._P)
        # Must not fabricate decision notes.
        self.assertIn("mengarang", self._P.lower())

    def test_prompt_describes_real_siap_workflow(self):
        # forward = advance a desk (note optional); decision approved/rejected
        # (note required); reject routes back one desk; SK issued ber-TTE by SIAP.
        low = self._P.lower()
        self.assertIn("forward", low)
        self.assertIn("opsional", low)          # forward note optional
        self.assertIn("wajib", low)             # decision note required
        self.assertIn("mundur", low)            # reject routes back one desk
        self.assertIn("tte", low)               # SK issued ber-TTE by SIAP

    def test_prompt_offers_draft_sk_after_positive_review(self):
        self.assertIn("draft_sk", self._P)
        # And never claims BIMA issues/signs the SK.
        low = self._P.lower()
        self.assertIn("bukan oleh anda", low)

    def test_prompt_forbids_hallucination_with_siap_language(self):
        # Anti-hallucination reinforcement: must instruct to say the datum is
        # not available in SIAP rather than invent it.
        self.assertIn("tidak tersedia di SIAP", self._P)


# ===========================================================================
# SIAP-grounded case loader ([[Multi-step Officer Copilot]])
# ===========================================================================


class TestSkSignUrl(unittest.TestCase):
    """_build_sk_sign_url — the env-configurable SIAP signing magic-link."""

    def test_default_template_uses_ticket(self):
        url = ob._build_sk_sign_url(request_id=42, ticket="123")
        self.assertIsNotNone(url)
        # ticket zero-padded to 9 digits.
        self.assertIn("000000123", url)

    def test_custom_template_with_request_id(self):
        with patch.object(
            ob, "_SK_SIGN_URL_TEMPLATE",
            "https://siap.example/sk/{request_id}/sign",
        ):
            url = ob._build_sk_sign_url(request_id=77, ticket="000000123")
        self.assertEqual(url, "https://siap.example/sk/77/sign")

    def test_none_when_required_field_missing(self):
        # Template needs request_id but none supplied → None, not a broken URL.
        with patch.object(
            ob, "_SK_SIGN_URL_TEMPLATE",
            "https://siap.example/sk/{request_id}/sign",
        ):
            url = ob._build_sk_sign_url(request_id=None, ticket="123")
        self.assertIsNone(url)

    def test_none_when_template_blank(self):
        with patch.object(ob, "_SK_SIGN_URL_TEMPLATE", ""):
            url = ob._build_sk_sign_url(request_id=1, ticket="1")
        self.assertIsNone(url)


def _install_siap_case(meta, docs=None, doc_bytes=b"FILEBYTES"):
    """Patch the SIAP reads load_case_from_siap depends on."""
    return patch.multiple(
        "services.siap_db",
        get_request_case_meta=AsyncMock(return_value=meta),
        get_submission_doc_refs=AsyncMock(return_value=docs or []),
    )


class TestLoadCaseFromSiap(unittest.TestCase):
    def setUp(self):
        ob._sessions.clear()

    def _meta(self, **over):
        base = {
            "found": True, "request_id": 900, "profile_id": 321,
            "license_id": 459, "approval_step_id": 5001, "ticket": "000077591",
            "current_sort_order": 1, "max_sort_order": 4, "is_final_step": False,
            "group_id": 6, "stereotype": None, "owner_desk": "Petugas SKPD",
        }
        base.update(over)
        return base

    def test_assembles_docs_and_ticket_from_siap(self):
        docs = [{"requirements_id": 10, "requirement_name": "Fotokopi KTP",
                 "file_ref": "berkas/ktp.pdf", "status": "OK"}]
        with _install_siap_case(self._meta(), docs=docs):
            with patch(
                "services.siap_templates.fetch_submission_file_bytes",
                new=AsyncMock(return_value=b"KTPBYTES"),
            ):
                sess = _run(ob.load_case_from_siap(900))
        self.assertIsNotNone(sess)
        self.assertEqual(sess.ticket, "000077591")
        self.assertEqual(sess.request_id, 900)
        self.assertEqual(sess.license_id, 459)
        self.assertFalse(sess.is_final_step)
        # Doc keyed by requirement, bytes threaded into the copilot doc-context.
        self.assertIn("siap:req:10", sess.documents)
        self.assertEqual(sess.documents["siap:req:10"]["content"], b"KTPBYTES")
        self.assertEqual(sess.documents["siap:req:10"]["filename"], "Fotokopi KTP")
        # No BIMA validator score exists in SIAP → validation absent (honest).
        self.assertIsNone(sess.validation)

    def test_final_step_flag_propagates(self):
        with _install_siap_case(self._meta(is_final_step=True, current_sort_order=4)):
            sess = _run(ob.load_case_from_siap(900))
        self.assertTrue(sess.is_final_step)

    def test_none_when_request_unresolvable_in_siap(self):
        # No hallucination: if SIAP can't identify the request, return None
        # rather than an empty/faked case.
        with _install_siap_case({"found": False, "ticket": None}):
            sess = _run(ob.load_case_from_siap(900))
        self.assertIsNone(sess)

    def test_doc_with_unfetchable_bytes_is_skipped_not_faked(self):
        docs = [{"requirements_id": 10, "requirement_name": "KTP",
                 "file_ref": "berkas/gone.pdf", "status": "OK"}]
        with _install_siap_case(self._meta(), docs=docs):
            with patch(
                "services.siap_templates.fetch_submission_file_bytes",
                new=AsyncMock(return_value=None),   # storage miss
            ):
                sess = _run(ob.load_case_from_siap(900))
        # Never surface a doc BIMA couldn't actually read.
        self.assertEqual(sess.documents, {})


class TestNextStepNotify(unittest.TestCase):
    def setUp(self):
        ob._sessions.clear()
        ob._officer_cache = set()

    def _meta(self, **over):
        base = {
            "found": True, "request_id": 900, "profile_id": 321,
            "license_id": 459, "approval_step_id": 5002, "ticket": "000077591",
            "current_sort_order": 2, "max_sort_order": 4, "is_final_step": False,
            "group_id": 7, "stereotype": None, "owner_desk": "Kabid",
        }
        base.update(over)
        return base

    def test_advance_notifies_next_desk_officer(self):
        env = {"BIMA_OFFICER_NOTIFY_ENABLED": "true"}
        resolution = {
            "wa_active": True, "is_applicant_step": False,
            "officer_whatsapps": ["628111222333"], "group_id": 7, "sort_order": 2,
        }
        with patch.dict("os.environ", env, clear=False):
            with patch(
                "services.siap_db.resolve_step_officers",
                new=AsyncMock(return_value=resolution),
            ):
                with _install_siap_case(self._meta()):
                    with patch.object(
                        ob, "_send_officer_notify", new=AsyncMock(return_value=True)
                    ) as notify:
                        sent = _run(ob.notify_next_step(900))
        self.assertTrue(sent)
        notify.assert_awaited_once()
        # A fresh SIAP-grounded session was registered for the next officer.
        self.assertIn("628111222333", ob._sessions)
        self.assertEqual(ob._sessions["628111222333"].ticket, "000077591")
        # And that officer is now recognizable on their first reply.
        self.assertIn("628111222333", ob._officer_cache)

    def test_last_step_sends_magic_link_not_forward_brief(self):
        env = {"BIMA_OFFICER_NOTIFY_ENABLED": "true"}
        resolution = {
            "wa_active": True, "is_applicant_step": False,
            "officer_whatsapps": ["628444555666"], "group_id": 12, "sort_order": 4,
        }
        with patch.dict("os.environ", env, clear=False):
            with patch(
                "services.siap_db.resolve_step_officers",
                new=AsyncMock(return_value=resolution),
            ):
                with _install_siap_case(self._meta(is_final_step=True, current_sort_order=4)):
                    with patch.object(
                        ob, "_send_officer_notify", new=AsyncMock(return_value=True)
                    ) as forward_notify, patch.object(
                        ob, "_send_final_step_notify", new=AsyncMock(return_value=True)
                    ) as final_notify:
                        sent = _run(ob.notify_next_step(900))
        self.assertTrue(sent)
        # LAST step → signing handoff, NOT the forward brief.
        final_notify.assert_awaited_once()
        forward_notify.assert_not_awaited()
        self.assertTrue(ob._sessions["628444555666"].is_final_step)

    def test_no_next_officer_is_noop(self):
        env = {"BIMA_OFFICER_NOTIFY_ENABLED": "true"}
        resolution = {
            "wa_active": False, "is_applicant_step": True,
            "officer_whatsapps": [], "group_id": 99, "sort_order": 0,
        }
        with patch.dict("os.environ", env, clear=False):
            with patch(
                "services.siap_db.resolve_step_officers",
                new=AsyncMock(return_value=resolution),
            ):
                sent = _run(ob.notify_next_step(900))
        self.assertFalse(sent)

    def test_suppressed_when_flag_off(self):
        with patch.dict("os.environ", {"BIMA_OFFICER_NOTIFY_ENABLED": "false"}, clear=False):
            sent = _run(ob.notify_next_step(900))
        self.assertFalse(sent)


class TestAdvanceDetection(unittest.TestCase):
    """_case_advanced — forward / approved advance the chain; reject does not."""

    def _call(self, name, args, ok=True):
        preview = '{"executed": true, "ok": %s}' % ("true" if ok else "false")
        return [{"name": name, "args": args, "result_preview": preview}]

    def test_confirmed_forward_advances(self):
        self.assertTrue(ob._case_advanced(self._call("forward_case", {"confirmed": True})))

    def test_confirmed_approved_advances(self):
        self.assertTrue(ob._case_advanced(
            self._call("record_decision", {"confirmed": True, "decision": "approved"})
        ))

    def test_confirmed_rejected_does_not_advance(self):
        # Reject routes BACK a desk — no next-step notify.
        self.assertFalse(ob._case_advanced(
            self._call("record_decision", {"confirmed": True, "decision": "rejected"})
        ))

    def test_draft_does_not_advance(self):
        self.assertFalse(ob._case_advanced(self._call("forward_case", {"confirmed": False})))

    def test_failed_write_does_not_advance(self):
        self.assertFalse(ob._case_advanced(
            self._call("forward_case", {"confirmed": True}, ok=False)
        ))


class TestFinalStepMode(unittest.TestCase):
    """The last-step session drives the copilot in signature mode (signing
    handoff, no forward/write tools)."""

    def setUp(self):
        ob._sessions.clear()

    def test_final_step_session_uses_signature_mode(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        # Arm a final-step session directly.
        sess = ob.OfficerCaseSession(
            channel_id="628999000111", channel=ob.CHANNEL_WHATSAPP,
            ticket="000077591", request_id=900, license_id=459,
            is_final_step=True,
        )
        _run(ob._put_session(sess))

        copilot = type("C", (), {})()
        chat_mock = AsyncMock(return_value={
            "reply": "SK siap ditandatangani.",
            "tool_calls": [], "history": [],
        })
        copilot.chat = chat_mock
        with patch.dict("os.environ", env, clear=False):
            with patch.object(oc, "get_copilot", return_value=copilot):
                _run(ob.maybe_handle_officer_reply(
                    channel=ob.CHANNEL_WHATSAPP,
                    channel_id="628999000111",
                    message="bagaimana cara tanda tangan?",
                ))
        _, kwargs = chat_mock.call_args
        self.assertEqual(kwargs["mode"], "signature")


class TestLoadSiapDocumentsMerge(unittest.TestCase):
    """`_load_siap_documents` merges TWO sources into the copilot `_doc_context`
    shape ({file_id: {filename, mime_type, content, claimed_type,
    detected_type}}):
      (a) profile_requirements docs (bytes from Beta storage), and
      (b) the per-request document API (applicant-upload docs).
    Docs present in both are deduped by (filename/label). When the document
    client is not configured, behaviour is byte-for-byte the current (a)-only.
    """

    @staticmethod
    def _profile_refs():
        # Source (a): one requirement, label "Fotokopi KTP".
        return [
            {"requirements_id": 11, "requirement_name": "Fotokopi KTP",
             "file_ref": "storage/ktp.pdf", "status": "uploaded"},
        ]

    @staticmethod
    def _doc_client(documents, download_map):
        client = MagicMock()
        # is_configured is a METHOD now (parity with the other SIAP clients).
        client.is_configured.return_value = True
        # `documents` here is list_documents' NORMALISED output — SIAP's real
        # file_name/file_type keys have already been mapped to filename/mime by
        # the client (SiapDocumentClient._normalise_document), which is exactly
        # what the officer loader consumes.
        client.list_documents = AsyncMock(return_value={
            "ok": True, "configured": True, "documents": documents, "note": ""})

        async def _download(file_id):
            return download_map.get(int(file_id))

        client.download_document = AsyncMock(side_effect=_download)
        return client

    def _patch_sources(self, *, refs, storage_bytes, doc_client):
        return [
            patch("services.siap_db.get_submission_doc_refs",
                  new=AsyncMock(return_value=refs)),
            patch("services.siap_templates.fetch_submission_file_bytes",
                  new=AsyncMock(return_value=storage_bytes)),
            patch("services.siap_document_client.get_siap_document_client",
                  return_value=doc_client),
        ]

    def test_merges_both_sources(self):
        # (a) yields "Fotokopi KTP"; (b) yields a distinct "NIB.pdf".
        # list_documents' normalised shape: file_id/filename/mime (mapped from
        # SIAP's real file_id/file_name/file_type).
        doc_client = self._doc_client(
            documents=[{"file_id": 501, "filename": "NIB.pdf",
                        "mime": "application/pdf", "created_at": "2026-05-10"}],
            download_map={501: b"NIBBYTES"},
        )
        patches = self._patch_sources(
            refs=self._profile_refs(), storage_bytes=b"KTPBYTES",
            doc_client=doc_client)
        with _Ctx_ob(patches):
            out = _run(ob._load_siap_documents(profile_id=7, request_id=555))
        # Both docs present, each once, exact shape preserved.
        self.assertEqual(len(out), 2)
        labels = {v["filename"] for v in out.values()}
        self.assertEqual(labels, {"Fotokopi KTP", "NIB.pdf"})
        for v in out.values():
            self.assertEqual(
                set(v.keys()),
                {"filename", "mime_type", "content", "claimed_type", "detected_type"})
        # (b) doc carried its bytes + mime through.
        nib = next(v for v in out.values() if v["filename"] == "NIB.pdf")
        self.assertEqual(nib["content"], b"NIBBYTES")
        self.assertEqual(nib["mime_type"], "application/pdf")

    def test_dedupes_doc_present_in_both_sources(self):
        # (b) also returns a "Fotokopi KTP" (formatted as "Fotokopi_KTP") — it
        # must NOT be emitted twice; source (a) wins, (b)'s dup is skipped and
        # never even downloaded.
        doc_client = self._doc_client(
            documents=[{"file_id": 777, "filename": "Fotokopi_KTP",
                        "mime": "application/pdf"}],
            download_map={777: b"SHOULD-NOT-APPEAR"},
        )
        patches = self._patch_sources(
            refs=self._profile_refs(), storage_bytes=b"KTPBYTES",
            doc_client=doc_client)
        with _Ctx_ob(patches):
            out = _run(ob._load_siap_documents(profile_id=7, request_id=555))
        self.assertEqual(len(out), 1)
        only = next(iter(out.values()))
        self.assertEqual(only["filename"], "Fotokopi KTP")
        self.assertEqual(only["content"], b"KTPBYTES")   # (a)'s bytes, not (b)'s
        doc_client.download_document.assert_not_awaited()  # dup never fetched

    def test_not_configured_client_is_byte_for_byte_source_a_only(self):
        doc_client = MagicMock()
        doc_client.is_configured.return_value = False
        doc_client.list_documents = AsyncMock()
        doc_client.download_document = AsyncMock()
        patches = self._patch_sources(
            refs=self._profile_refs(), storage_bytes=b"KTPBYTES",
            doc_client=doc_client)
        with _Ctx_ob(patches):
            out = _run(ob._load_siap_documents(profile_id=7, request_id=555))
        # Exactly the pre-existing (a)-only result.
        self.assertEqual(len(out), 1)
        only = next(iter(out.values()))
        self.assertEqual(only["filename"], "Fotokopi KTP")
        self.assertEqual(only["content"], b"KTPBYTES")
        doc_client.list_documents.assert_not_awaited()
        doc_client.download_document.assert_not_awaited()

    def test_undownloadable_doc_api_file_is_skipped_not_faked(self):
        # (b) lists a doc but its bytes come back None → it is NOT emitted.
        doc_client = self._doc_client(
            documents=[{"file_id": 900, "filename": "SIUP.pdf",
                        "mime": "application/pdf"}],
            download_map={},  # download returns None
        )
        patches = self._patch_sources(
            refs=self._profile_refs(), storage_bytes=b"KTPBYTES",
            doc_client=doc_client)
        with _Ctx_ob(patches):
            out = _run(ob._load_siap_documents(profile_id=7, request_id=555))
        self.assertEqual(len(out), 1)  # only (a)'s KTP
        self.assertNotIn("SIUP.pdf", {v["filename"] for v in out.values()})

    def test_real_siap_keys_end_to_end_yield_nonempty_filename(self):
        # END-TO-END through the REAL SiapDocumentClient normaliser: SIAP sends
        # data.documents[*].file_name/file_type; the officer loader must still
        # get a NON-EMPTY filename/label so dedupe + _resolve_doc_ref work. Here
        # list_documents is NOT mocked — only httpx is — so the real
        # _normalise_document runs.
        from services.siap_document_client import SiapDocumentClient

        real_client = SiapDocumentClient(
            base="http://siap.test", token="tok-real", timeout=5.0)

        # httpx list response in SIAP's real shape.
        list_resp = MagicMock()
        list_resp.status_code = 200
        list_resp.text = ""
        list_resp.json.return_value = {
            "status": "success",
            "data": {"request_id": 555, "count": 1, "documents": [
                {"file_id": 42, "file_name": "Fotokopi NIB.pdf",
                 "file_type": "application/pdf", "created_on": "2026-05-10"},
            ]},
        }

        # httpx download stream (size-capped path) yielding the NIB bytes.
        dl_resp = MagicMock()
        dl_resp.status_code = 200
        dl_resp.headers = {}
        dl_resp.text = ""

        async def _aiter():
            yield b"NIBBYTES"

        dl_resp.aiter_bytes = _aiter
        dl_resp.__aenter__ = AsyncMock(return_value=dl_resp)
        dl_resp.__aexit__ = AsyncMock(return_value=False)

        fake_httpx_client = MagicMock()
        fake_httpx_client.get = AsyncMock(return_value=list_resp)
        fake_httpx_client.stream = MagicMock(return_value=dl_resp)
        httpx_ctx = MagicMock()
        httpx_ctx.__aenter__ = AsyncMock(return_value=fake_httpx_client)
        httpx_ctx.__aexit__ = AsyncMock(return_value=False)

        patches = [
            # No source (a) — isolate the doc-API path.
            patch("services.siap_db.get_submission_doc_refs",
                  new=AsyncMock(return_value=[])),
            patch("services.siap_templates.fetch_submission_file_bytes",
                  new=AsyncMock(return_value=None)),
            patch("services.siap_document_client.get_siap_document_client",
                  return_value=real_client),
            patch("services.siap_document_client.httpx.AsyncClient",
                  return_value=httpx_ctx),
        ]
        with _Ctx_ob(patches):
            out = _run(ob._load_siap_documents(profile_id=None, request_id=555))
        self.assertEqual(len(out), 1)
        only = next(iter(out.values()))
        # The load-bearing assertion: SIAP's file_name surfaced as a non-empty
        # filename/label the officer loader can dedupe + resolve on.
        self.assertEqual(only["filename"], "Fotokopi NIB.pdf")
        self.assertTrue(only["filename"])
        self.assertEqual(only["mime_type"], "application/pdf")
        self.assertEqual(only["content"], b"NIBBYTES")


class _Ctx_ob:
    """Enter a list of context managers together (local to officer-bridge tests)."""
    def __init__(self, patches):
        self._p = patches

    def __enter__(self):
        for p in self._p:
            p.__enter__()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.__exit__(*exc)
        return False


class TestScoreSurvivesTheForward(unittest.TestCase):
    """BIMA's score is a property of the CASE, not of one officer's session.

    Live 2026-07-17, ticket 000077767: Betty forwarded to the Kabid, whose alert
    read "Skor BIMA: -" and whose session carried validation=None. Root cause:
    the score lived ONLY inside the officer session; the forward CLOSES (deletes)
    that session (logged closed=True), and notify_next_step rebuilds the next
    desk from SIAP — which has no BIMA score. So every desk after the first
    reviewed the file blind, and the first desk's score was unrecoverable.
    """

    def setUp(self):
        ob._sessions.clear() if hasattr(ob, "_sessions") else None

    def test_validation_round_trips_by_request_id(self):
        # The fakes MUST mirror session_store's real signatures —
        # save(key, obj, *, encode, ttl_seconds) and load(key, *, decode).
        # A stub with the wrong arity tests the mock, not the code: the first
        # version of this test used `save(key, blob, *, ttl_seconds)`, passed
        # green, and the real call raised
        # "TypeError: save() missing 1 required keyword-only argument: 'encode'"
        # in the container — swallowed by _put_case_validation's except, so the
        # score silently stayed "-": the exact bug this class exists to prevent.
        store = {}

        async def fake_save(key, obj, *, encode, ttl_seconds):
            store[key] = encode(obj)          # exercise the encoder for real
            self.assertGreater(ttl_seconds, 0)
            return True

        async def fake_load(key, *, decode):
            raw = store.get(key)
            return decode(raw) if raw is not None else None

        v = {"score_percent": 92, "status": "lengkap", "issues": []}
        with patch.object(ob.session_store, "save", new=fake_save), \
             patch.object(ob.session_store, "load", new=fake_load):
            _run(ob._put_case_validation(77767, v))
            got = _run(ob._get_case_validation(77767))
        self.assertEqual(got, v)
        # Keyed by the CASE, never by an officer channel.
        self.assertIn("bima:case_validation:77767", store)
        self.assertIsInstance(store["bima:case_validation:77767"], str)

    def test_helpers_bind_to_the_real_session_store_signature(self):
        # The guard the round-trip test cannot give us: bind the ACTUAL
        # session_store.save/load signatures against how we call them, so a
        # drift in either fails HERE instead of silently in production.
        import inspect

        inspect.signature(ob.session_store.save).bind(
            "k", {"a": 1}, encode=lambda v: "{}", ttl_seconds=1)
        inspect.signature(ob.session_store.load).bind("k", decode=lambda s: {})

    def test_missing_validation_degrades_to_none_not_crash(self):
        async def fake_load(key, *, decode):
            return None

        with patch.object(ob.session_store, "load", new=fake_load):
            self.assertIsNone(_run(ob._get_case_validation(77767)))
        self.assertIsNone(_run(ob._get_case_validation(None)))

    def test_persist_is_best_effort_and_never_raises(self):
        # A store failure must never break a submission — it only costs a "-".
        async def boom(key, obj, *, encode, ttl_seconds):
            raise RuntimeError("redis down")

        with patch.object(ob.session_store, "save", new=boom):
            _run(ob._put_case_validation(77767, {"score_percent": 92}))  # no raise

    def test_nothing_persisted_without_a_score(self):
        store = {}

        async def fake_save(key, obj, *, encode, ttl_seconds):
            store[key] = encode(obj)
            return True

        with patch.object(ob.session_store, "save", new=fake_save):
            _run(ob._put_case_validation(77767, None))
            _run(ob._put_case_validation(77767, {}))
            _run(ob._put_case_validation(None, {"score_percent": 92}))
        self.assertEqual(store, {})

    def test_the_template_renders_the_score_when_present(self):
        # The exact symptom: score_percent -> "92%", absent -> "-".
        sent = {}

        async def fake_send_template(*, recipient_phone, template_name,
                                     body_params, language_code):
            sent["params"] = body_params
            return True

        with patch("services.whatsapp_template.send_template", new=fake_send_template):
            _run(ob._send_officer_notify(
                ob.CHANNEL_WHATSAPP, "628123",
                license_name="PKPP", ticket="000077767",
                validation={"score_percent": 92}, brief="x"))
        self.assertEqual(sent["params"][2], "92%")

        with patch("services.whatsapp_template.send_template", new=fake_send_template):
            _run(ob._send_officer_notify(
                ob.CHANNEL_WHATSAPP, "628123",
                license_name="PKPP", ticket="000077767",
                validation=None, brief="x"))
        self.assertEqual(sent["params"][2], "-")


if __name__ == "__main__":
    unittest.main(verbosity=2)
