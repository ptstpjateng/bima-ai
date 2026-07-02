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


if __name__ == "__main__":
    unittest.main(verbosity=2)
