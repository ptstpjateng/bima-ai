"""
Tests for the REDESIGNED guided-submission flow — `services/guided_submission.py`
and `services/siap_submission_client.py`.

Run standalone (no pytest needed):

    python3 tests/test_guided_submission.py

The redesign (2026-06) makes the flow feel like a human assistant:
  RESOLVING_LICENSE -> COLLECTING_DOCS (debounced, silent uploads) -> CONFIRM
  (intent-based, no rigid keyword). The 4-question Q&A, the SIAP/SELESAI gates,
  and the per-file ack are GONE; applicant fields are EXTRACTED from documents.

What this covers:
  * detect_submission_intent — positive + negative phrasing (unchanged).
  * SiapSubmissionClient — not-configured / 2xx / 4xx / timeout / bad ids.
  * The state machine end to end with the LLM resolver, SIAP requirements, the
    citizen scorer, the KTP Vision extractor, the confirm-intent classifier and
    the SIAP submission client all mocked:
      - flag off -> maybe_handle returns None
      - resolve -> warm requirements + upload invite, NO SIAP gate, NO field Q&A
      - upload burst -> ONE consolidated reply (out-of-band send), masked NIK
      - phone taken from the msisdn (never asked)
      - AFFIRM intent -> submit -> ticket; DECLINE -> hold; CORRECT -> re-confirm
      - sub-threshold -> offer "ajukan apa adanya", second AFFIRM force-submits
      - hard "batal" cancels anywhere
      - no emoji in the new citizen-facing messages

No real network, no SIAP, no Gemini, no Vision.
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
from services import submission_intent as si  # noqa: E402
from services.siap_submission_client import SiapSubmissionClient  # noqa: E402
from services.agents.suitability_judge import (  # noqa: E402
    CompletenessSection,
    Issue,
    SuitabilityResult,
)


def _run(coro):
    return asyncio.run(coro)


def _suitability_result(percent=96, critical=False):
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


def _doc(file_id="doc-1", claimed="ktp"):
    # Unique bytes per file_id — real documents differ; identical bytes are
    # collapsed by the content-dedup in _attach_documents.
    return gs.SessionDocument(file_id, claimed, f"{claimed}.jpg", "image/jpeg",
                              b"\xff\xd8\xff" + file_id.encode())


# A KTP Vision extraction result (mocked) — name + NIK come from HERE, not Q&A.
_KTP = {"is_ktp": True, "name": "Budi Santoso", "nik": "3374012345678901", "gender": "LAKI-LAKI"}


# ===========================================================================
# Intent detection (start trigger) — unchanged
# ===========================================================================

class TestIntentDetection(unittest.TestCase):

    def test_positive_indonesian(self):
        for msg in (
            "saya mau ajukan izin pemakaian tanah",
            "tolong bantu daftarkan izin lingkungan untuk usaha saya",
            "saya ingin mengajukan permohonan izin trayek",
            "mau buatkan NIB dong",
        ):
            self.assertTrue(gs.detect_submission_intent(msg), f"should detect: {msg!r}")

    def test_positive_english(self):
        self.assertTrue(gs.detect_submission_intent("I want to apply for a new permit"))

    def test_negative(self):
        for msg in (
            "apa itu izin usaha?",
            "status izin 000077591",
            "halo bima",
            "saya mau daftar antrean",
            "berapa biaya izin pemakaian tanah",
        ):
            self.assertFalse(gs.detect_submission_intent(msg), f"should NOT detect: {msg!r}")


# ===========================================================================
# SiapSubmissionClient — unchanged surface
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
        resp.json.return_value = {"data": {"request_id": 4321, "ticket": "000099001"}}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=mock_ctx):
            res = _run(client.create_request(license_id=5, profile_id=9, description="test"))
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
# State machine fixtures
# ===========================================================================

_ONE_MATCH = {
    "found": True,
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
    """Shared setup: flag ON, clean session table, demo packet OFF, short
    debounce, and a deterministic confirm-intent classifier per test."""

    def setUp(self):
        gs._sessions.clear()
        gs._debounce_tasks.clear()
        os.environ["BIMA_GUIDED_SUBMISSION_ENABLED"] = "true"
        os.environ["GUIDED_SUBMISSION_PROFILE_ID"] = "7"
        os.environ["GUIDED_SUBMISSION_DEMO_PACKET"] = ""
        os.environ["GUIDED_SUBMISSION_DEBOUNCE_SECONDS"] = "0.2"

    def tearDown(self):
        gs._sessions.clear()
        gs._debounce_tasks.clear()
        for k in ("BIMA_GUIDED_SUBMISSION_ENABLED", "GUIDED_SUBMISSION_PROFILE_ID",
                  "GUIDED_SUBMISSION_DEMO_PACKET", "GUIDED_SUBMISSION_DEBOUNCE_SECONDS"):
            os.environ.pop(k, None)


def _flow_patches(*, score=96, ktp=_KTP, confirm_intent="AFFIRM",
                  resolver=None, requirements=None):
    """Bundle the common patches a flow test needs. `confirm_intent` may be a
    single string or an AsyncMock for multi-call sequences."""
    resolver = resolver if resolver is not None else AsyncMock(return_value=_ONE_MATCH)
    requirements = requirements if requirements is not None else AsyncMock(return_value=_REQUIREMENTS)
    ci = (confirm_intent if isinstance(confirm_intent, AsyncMock)
          else AsyncMock(return_value=confirm_intent))
    return [
        patch("services.license_resolver.resolve_license_intent", resolver),
        patch("services.siap_tools.siap_get_requirements", requirements),
        patch("services.citizen_scorer.score_session_documents",
              new=AsyncMock(return_value=_suitability_result(score))),
        patch("services.gemini_vision.extract_ktp_fields", new=AsyncMock(return_value=ktp)),
        patch("services.gemini_vision.is_configured", return_value=True),
        patch("services.submission_intent.classify_confirm_intent", ci),
    ]


class _Ctx:
    """Enter a list of context managers together."""
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


class TestFeatureFlag(_GuidedFlowBase):

    def test_flag_off_returns_none(self):
        os.environ["BIMA_GUIDED_SUBMISSION_ENABLED"] = "false"
        res = _run(gs.maybe_handle("u1", "saya mau ajukan izin pemakaian tanah"))
        self.assertIsNone(res)

    def test_non_submission_message_returns_none(self):
        res = _run(gs.maybe_handle("u1", "apa itu KBLI?"))
        self.assertIsNone(res)


class TestResolveInvitesUpload(_GuidedFlowBase):
    """On a single match BIMA lists requirements and invites an upload directly
    — NO readiness gate, NO field questions."""

    def test_resolve_lists_requirements_and_invites_upload(self):
        with _Ctx(_flow_patches()):
            uid = "wa-628"
            r = _run(gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
        self.assertIn("Izin Pemakaian Tanah", r)
        self.assertIn("Fotokopi KTP", r)            # requirement listed
        self.assertIn("upload", r.lower())          # invited to upload directly
        # No rigid SIAP/SELESAI gate, no per-field question.
        self.assertNotIn("Balas *SIAP*", r)
        self.assertNotIn("nama lengkap pemohon", r.lower())
        self.assertEqual(gs._sessions[uid].stage, gs.Stage.COLLECTING_DOCS)
        self.assertTrue(_run(gs.has_active_session(uid)))


class TestUploadDebounceAndExtract(_GuidedFlowBase):
    """A burst of uploads attaches SILENTLY and yields exactly ONE consolidated
    reply (sent out-of-band), with fields extracted from the docs."""

    def test_three_docs_one_consolidated_reply_with_masked_nik(self):
        sent = []

        async def fake_send(recipient_phone=None, body=None, **k):
            sent.append(body)
            return True

        async def scenario():
            patches = _flow_patches()
            with _Ctx(patches), patch("services.whatsapp_sender.send_text", new=fake_send):
                uid = "wa-628123"
                await gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah")  # noqa: F841
                # 3 docs arrive as 3 separate webhooks → 3 handle_inbound_documents
                r1 = await gs.handle_inbound_documents(uid, [_doc("doc-1", "ktp")])
                r2 = await gs.handle_inbound_documents(uid, [_doc("doc-2", "nib")])
                r3 = await gs.handle_inbound_documents(uid, [_doc("doc-3", "surat")])
                # Each attach is SILENT (no per-file reply) and only ONE debounce
                # task is spawned for the burst.
                self.assertEqual((r1, r2, r3), (None, None, None))
                self.assertEqual(len(gs._debounce_tasks), 1)
                self.assertEqual(len(gs._sessions[uid].documents), 3)
                # Wait for the debounce to settle and fire the consolidated send.
                await asyncio.sleep(0.8)
                return uid

        uid = _run(scenario())
        # Exactly ONE consolidated reply was sent (not 3).
        self.assertEqual(len(sent), 1, f"expected 1 consolidated reply, got {len(sent)}")
        body = sent[0]
        # The data BIMA read is shown, with the NIK MASKED.
        self.assertIn("Budi Santoso", body)
        self.assertIn("33", body)
        self.assertNotIn("3374012345678901", body)   # full NIK must NOT appear
        self.assertIn("yakin", body.lower())          # natural confirm question
        # Session advanced to CONFIRM.
        self.assertEqual(gs._sessions[uid].stage, gs.Stage.CONFIRM)

    def test_phone_taken_from_msisdn_never_asked(self):
        with _Ctx(_flow_patches()):
            uid = "wa-628777111"
            _run(gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
            _run(gs.handle_inbound_documents(uid, [_doc()]))
            _run(gs._process_collected_documents(gs._sessions[uid]))
        # Phone is the citizen's OWN msisdn (from 'wa-{msisdn}'), never asked.
        self.assertEqual(gs._sessions[uid].fields.get("phone"), "628777111")

    def test_name_and_nik_extracted_from_ktp(self):
        with _Ctx(_flow_patches()):
            uid = "wa-628"
            _run(gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
            _run(gs.handle_inbound_documents(uid, [_doc()]))
            _run(gs._process_collected_documents(gs._sessions[uid]))
        f = gs._sessions[uid].fields
        self.assertEqual(f.get("applicant_name"), "Budi Santoso")
        self.assertEqual(f.get("nik"), "3374012345678901")


class TestMissingFieldFallback(_GuidedFlowBase):
    """When the KTP can't be read, BIMA asks for just that field naturally and
    does NOT restart; a typed name+NIK then advances to CONFIRM."""

    def test_unreadable_ktp_asks_for_field_then_proceeds(self):
        no_ktp = {"is_ktp": False, "name": None, "nik": None, "gender": None}
        with _Ctx(_flow_patches(ktp=no_ktp)):
            uid = "wa-629"
            _run(gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
            _run(gs.handle_inbound_documents(uid, [_doc()]))
            r = _run(gs._process_collected_documents(gs._sessions[uid]))
            # Asked for the KTP/field, still collecting (not restarted).
            self.assertIn("ktp", r.lower())
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.COLLECTING_DOCS)
            # Citizen types the name + NIK → advances to CONFIRM.
            r2 = _run(gs.maybe_handle(uid, "nama saya Andi Wijaya, NIK 3312345678901234"))
        s = gs._sessions[uid]
        self.assertEqual(s.fields.get("applicant_name"), "Andi Wijaya")
        self.assertEqual(s.fields.get("nik"), "3312345678901234")
        self.assertEqual(s.stage, gs.Stage.CONFIRM)
        self.assertIn("yakin", r2.lower())


class TestConfirmIntent(_GuidedFlowBase):
    """The CONFIRM stage is INTENT-based — no rigid 'YA' keyword."""

    def _to_confirm(self, uid, *, score=96):
        # Drive to CONFIRM (intent classifier patched separately per assertion).
        with _Ctx(_flow_patches(score=score)):
            _run(gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
            _run(gs.handle_inbound_documents(uid, [_doc()]))
            _run(gs._process_collected_documents(gs._sessions[uid]))
        self.assertEqual(gs._sessions[uid].stage, gs.Stage.CONFIRM)

    def test_affirm_natural_language_submits(self):
        for phrase in ("oke pak lanjut", "ya saya yakin"):
            gs._sessions.clear()
            self._to_confirm("wa-630")
            mc = MagicMock()
            mc.is_configured.return_value = True
            mc.create_request = AsyncMock(return_value={
                "ok": True, "configured": True, "request_id": 555, "ticket": "000088002"})
            with patch("services.submission_intent.classify_confirm_intent",
                       new=AsyncMock(return_value="AFFIRM")), \
                 patch("services.siap_submission_client.get_siap_submission_client",
                       return_value=mc), \
                 patch("services.officer_bridge.notify_officer_of_submission",
                       new=AsyncMock(return_value=True)):
                r = _run(gs.maybe_handle("wa-630", phrase))
            self.assertIn("000088002", r, f"phrase={phrase!r}")
            self.assertIn("track/000088002", r)
            self.assertFalse(_run(gs.has_active_session("wa-630")))
            mc.create_request.assert_awaited_once()

    def test_decline_holds_without_submit(self):
        self._to_confirm("wa-631")
        mc = MagicMock()
        mc.is_configured.return_value = True
        mc.create_request = AsyncMock()
        with patch("services.submission_intent.classify_confirm_intent",
                   new=AsyncMock(return_value="DECLINE")), \
             patch("services.siap_submission_client.get_siap_submission_client",
                   return_value=mc):
            r = _run(gs.maybe_handle("wa-631", "nanti dulu deh"))
        self.assertIn("tahan dulu", r.lower())
        mc.create_request.assert_not_awaited()
        self.assertFalse(_run(gs.has_active_session("wa-631")))

    def test_correction_updates_field_and_reconfirms(self):
        self._to_confirm("wa-632")
        with patch("services.submission_intent.classify_confirm_intent",
                   new=AsyncMock(return_value="CORRECT")):
            r = _run(gs.maybe_handle("wa-632", "nama usaha PT Maju Jaya Sentosa"))
        s = gs._sessions["wa-632"]
        self.assertEqual(s.fields.get("business_name"), "PT Maju Jaya Sentosa")
        self.assertEqual(s.stage, gs.Stage.CONFIRM)        # stays at confirm
        self.assertIn("yakin", r.lower())                  # re-asks confirmation
        self.assertIn("PT Maju Jaya Sentosa", r)           # re-shows the data

    def test_correction_of_nik_shows_masked(self):
        self._to_confirm("wa-632b")
        with patch("services.submission_intent.classify_confirm_intent",
                   new=AsyncMock(return_value="CORRECT")):
            r = _run(gs.maybe_handle("wa-632b", "NIK saya yang benar 3311009988776655"))
        self.assertEqual(gs._sessions["wa-632b"].fields.get("nik"), "3311009988776655")
        self.assertNotIn("3311009988776655", r)            # masked in the read-back
        self.assertIn("33", r)

    def test_question_answers_then_reasks(self):
        self._to_confirm("wa-633")
        with patch("services.submission_intent.classify_confirm_intent",
                   new=AsyncMock(return_value="QUESTION")):
            r = _run(gs.maybe_handle("wa-633", "berapa lama prosesnya?"))
        self.assertIn("hari kerja", r.lower())             # answered from SLA
        self.assertIn("yakin", r.lower())                  # then re-asks
        self.assertEqual(gs._sessions["wa-633"].stage, gs.Stage.CONFIRM)


class TestSubThresholdOverride(_GuidedFlowBase):
    """A sub-threshold packet is not auto-submitted on the first AFFIRM; BIMA
    offers 'ajukan apa adanya' and the next AFFIRM force-submits."""

    def test_first_affirm_offers_override_second_submits(self):
        gs._sessions.clear()
        with _Ctx(_flow_patches(score=62)):
            uid = "wa-634"
            _run(gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
            _run(gs.handle_inbound_documents(uid, [_doc()]))
            _run(gs._process_collected_documents(gs._sessions[uid]))
        self.assertEqual(gs._sessions[uid].stage, gs.Stage.CONFIRM)

        mc = MagicMock()
        mc.is_configured.return_value = True
        mc.create_request = AsyncMock(return_value={
            "ok": True, "configured": True, "request_id": 9, "ticket": "000077001"})
        with patch("services.submission_intent.classify_confirm_intent",
                   new=AsyncMock(return_value="AFFIRM")), \
             patch("services.siap_submission_client.get_siap_submission_client",
                   return_value=mc), \
             patch("services.officer_bridge.notify_officer_of_submission",
                   new=AsyncMock(return_value=True)):
            # First AFFIRM on a sub-threshold packet → override offered, no submit.
            r1 = _run(gs.maybe_handle(uid, "ya lanjut"))
            self.assertIn("apa adanya", r1.lower())
            mc.create_request.assert_not_awaited()
            self.assertTrue(_run(gs.has_active_session(uid)))
            # Second AFFIRM → force-submit past the gate.
            r2 = _run(gs.maybe_handle(uid, "ya kirim saja"))
        self.assertIn("000077001", r2)
        mc.create_request.assert_awaited_once()


class TestDegradeGracefully(_GuidedFlowBase):

    def test_submission_client_not_configured(self):
        gs._sessions.clear()
        with _Ctx(_flow_patches(score=96)):
            uid = "wa-635"
            _run(gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
            _run(gs.handle_inbound_documents(uid, [_doc()]))
            _run(gs._process_collected_documents(gs._sessions[uid]))
        mc = MagicMock()
        mc.is_configured.return_value = False
        with patch("services.submission_intent.classify_confirm_intent",
                   new=AsyncMock(return_value="AFFIRM")), \
             patch("services.siap_submission_client.get_siap_submission_client",
                   return_value=mc):
            r = _run(gs.maybe_handle(uid, "ya"))
        self.assertIn("belum aktif", r)
        self.assertIn("belum terkirim", r)


class TestCancel(_GuidedFlowBase):
    """The literal 'batal' is a strong cancel that works anywhere — no LLM."""

    def test_hard_batal_cancels_from_collecting_docs(self):
        with _Ctx(_flow_patches()):
            uid = "wa-636"
            _run(gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
            self.assertTrue(_run(gs.has_active_session(uid)))
            r = _run(gs.maybe_handle(uid, "batal"))
        self.assertIn("dibatalkan", r)
        self.assertFalse(_run(gs.has_active_session(uid)))

    def test_hard_batal_cancels_from_confirm(self):
        with _Ctx(_flow_patches()):
            uid = "wa-637"
            _run(gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
            _run(gs.handle_inbound_documents(uid, [_doc()]))
            _run(gs._process_collected_documents(gs._sessions[uid]))
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.CONFIRM)
            r = _run(gs.maybe_handle(uid, "batal"))
        self.assertIn("dibatalkan", r)
        self.assertFalse(_run(gs.has_active_session("wa-637")))


# ===========================================================================
# Disambiguation shortlist
# ===========================================================================

_TWO_MATCHES = {
    "found": True,
    "matches": [
        {"license_id": 42, "name": "Izin Pemakaian Tanah", "sektor": "PU"},
        {"license_id": 43, "name": "Izin Pemanfaatan Tanah", "sektor": "PU"},
    ],
}


class TestDisambiguation(_GuidedFlowBase):

    def test_numbered_pick_locks_and_invites_upload(self):
        with patch("services.license_resolver.resolve_license_intent",
                   new=AsyncMock(return_value=_TWO_MATCHES)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)):
            uid = "wa-638"
            r1 = _run(gs.maybe_handle(uid, "saya mau ajukan izin tanah"))
            self.assertIn("1.", r1)
            self.assertIn("2.", r1)
            self.assertEqual(gs._sessions[uid].stage, gs.Stage.RESOLVING_LICENSE)
            r2 = _run(gs.maybe_handle(uid, "1"))
        self.assertEqual(gs._sessions[uid].license_id, 42)
        self.assertIn("upload", r2.lower())
        self.assertEqual(gs._sessions[uid].stage, gs.Stage.COLLECTING_DOCS)


# ===========================================================================
# Field validators (the doc-extraction helpers replace the Q&A validators)
# ===========================================================================

class TestFieldValidators(unittest.TestCase):

    def test_nik_validator(self):
        self.assertEqual(gs._v_nik("3374 0123 4567 8901"), "3374012345678901")
        self.assertIsNone(gs._v_nik("123"))
        self.assertIsNone(gs._v_nik("33740123456789012"))  # 17 digits

    def test_name_validator_rejects_intent_phrase(self):
        self.assertIsNone(gs._v_name("saya mau ajukan izin PKPP"))
        self.assertIsNone(gs._v_name("ajukan izin lingkungan"))
        self.assertEqual(gs._v_name("Budi Santoso"), "Budi Santoso")

    def test_msisdn_from_user_id(self):
        self.assertEqual(gs._msisdn_from_user_id("wa-628123"), "628123")
        self.assertIsNone(gs._msisdn_from_user_id("tg-123"))
        self.assertIsNone(gs._msisdn_from_user_id("websess-x"))


# ===========================================================================
# No emoji in the new citizen-facing messages
# ===========================================================================

class TestNoEmojiInGuidedMessages(_GuidedFlowBase):

    _EMOJI = re.compile(
        "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
        "\U0001F1E6-\U0001F1FF\U0000FE0F\U000020E3\U00002190-\U000021FF]"
    )

    def _assert_clean(self, text):
        found = self._EMOJI.findall(text or "")
        self.assertEqual(found, [], f"emoji leaked: {found!r} in {text!r}")

    def test_key_messages_are_emoji_free(self):
        sent = []

        async def fake_send(recipient_phone=None, body=None, **k):
            sent.append(body)
            return True

        async def scenario():
            mc = MagicMock()
            mc.is_configured.return_value = True
            mc.create_request = AsyncMock(return_value={
                "ok": True, "configured": True, "request_id": 1, "ticket": "000088002"})
            patches = _flow_patches()
            with _Ctx(patches), \
                 patch("services.whatsapp_sender.send_text", new=fake_send), \
                 patch("services.siap_submission_client.get_siap_submission_client",
                       return_value=mc), \
                 patch("services.officer_bridge.notify_officer_of_submission",
                       new=AsyncMock(return_value=True)):
                uid = "wa-639"
                self._assert_clean(await gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
                await gs.handle_inbound_documents(uid, [_doc()])
                await asyncio.sleep(0.6)               # consolidated reply via fake_send
                self._assert_clean(await gs.maybe_handle(uid, "oke lanjut"))  # success

        _run(scenario())
        for body in sent:
            self._assert_clean(body)

    def test_cancel_message_is_emoji_free(self):
        with _Ctx(_flow_patches()):
            uid = "wa-640"
            _run(gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
            self._assert_clean(_run(gs.maybe_handle(uid, "batal")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
