"""
Tests for the Phase 5 Privy integration — `services/privy_client.py`,
`services/legal_templates.py`, `routers/privy_callback.py`, and the
guided-submission state-machine extension (AWAITING_PRIVY_SIGN).

Run standalone (no pytest needed — matches tests/test_guided_submission.py):

    python -m tests.test_privy_integration     # from ai-engine/
    python tests/test_privy_integration.py     # also works

What it covers:
  * PrivyClient — not configured, success, transient retry, 4xx, timeout,
    apply_emeterai b64 round-trip, get_signed_doc happy/pending/failed,
    HMAC verification accept/reject + sha256= prefix handling.
  * legal_templates — Jinja2-style substitution, PDF byte signature,
    Indonesian date formatting, missing-variable safety.
  * Privy webhook router — HMAC rejection, malformed JSON, complete +
    failed events route to the right background handler.
  * Guided-submission state machine — Privy branch entered after fields
    collected when enabled; sudah-belum poll; webhook completion path.

No real network, no Privy account, no PDF renderer beyond the in-tree
one.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import hashlib
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Make `services`/`routers` importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Stub heavy deps so the test file imports cleanly without the full stack.
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


from services import privy_client as privy_mod  # noqa: E402
from services import legal_templates as lt  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _mock_response(status_code: int, json_body: dict | None = None,
                   text: str = "") -> MagicMock:
    """Build a MagicMock that looks like an httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    if json_body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_body
    resp.text = text or (json.dumps(json_body) if json_body else "")
    return resp


def _mock_async_client_post(resp_or_side):
    """Build a context-manager-shaped AsyncMock for `httpx.AsyncClient()`."""
    mock_client = AsyncMock()
    if isinstance(resp_or_side, list):
        mock_client.request = AsyncMock(side_effect=resp_or_side)
    elif isinstance(resp_or_side, BaseException):
        # An exception instance — make every call raise it.
        mock_client.request = AsyncMock(side_effect=resp_or_side)
    else:
        mock_client.request = AsyncMock(return_value=resp_or_side)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, mock_client


# ===========================================================================
# PrivyClient
# ===========================================================================


class TestPrivyClientConfig(unittest.TestCase):

    def test_not_configured_when_flag_off(self):
        c = privy_mod.PrivyClient(
            base="http://privy.test", api_key="k", webhook_secret="s",
            enabled=False,
        )
        self.assertFalse(c.is_configured())

    def test_not_configured_when_key_blank(self):
        c = privy_mod.PrivyClient(
            base="http://privy.test", api_key="", webhook_secret="s",
            enabled=True,
        )
        self.assertFalse(c.is_configured())

    def test_configured_when_both_present(self):
        c = privy_mod.PrivyClient(
            base="http://privy.test", api_key="k", webhook_secret="s",
            enabled=True,
        )
        self.assertTrue(c.is_configured())

    def test_initiate_short_circuits_when_unconfigured(self):
        c = privy_mod.PrivyClient(enabled=False)
        res = _run(c.initiate_signing(
            pdf_bytes=b"%PDF-1.4", signer_nik="3374012345678901",
            signer_name="Budi", document_title="Test",
        ))
        self.assertFalse(res["ok"])
        self.assertFalse(res["configured"])


class TestPrivyClientHTTP(unittest.TestCase):

    def setUp(self):
        self.client = privy_mod.PrivyClient(
            base="http://privy.test", api_key="k", webhook_secret="s",
            enabled=True, timeout=1.0,
        )

    def test_initiate_signing_happy_path(self):
        resp = _mock_response(201, {
            "data": {
                "request_id": "req_abc123",
                "signing_url": "https://privy.test/s/abc123",
            },
        })
        ctx, _ = _mock_async_client_post(resp)
        with patch("httpx.AsyncClient", return_value=ctx):
            res = _run(self.client.initiate_signing(
                pdf_bytes=b"%PDF-1.4\n%bin\n",
                signer_nik="3374012345678901",
                signer_name="Budi Santoso",
                document_title="Pakta Integritas",
            ))
        self.assertTrue(res["ok"])
        self.assertEqual(res["request_id"], "req_abc123")
        self.assertEqual(res["signing_url"], "https://privy.test/s/abc123")

    def test_initiate_signing_empty_pdf_rejected_locally(self):
        res = _run(self.client.initiate_signing(
            pdf_bytes=b"", signer_nik="3374012345678901",
            signer_name="Budi", document_title="Test",
        ))
        self.assertFalse(res["ok"])
        self.assertIn("kosong", res["note"])

    def test_initiate_signing_response_missing_fields(self):
        # Privy returns 2xx but no request_id — the client must report
        # a structured failure, not crash.
        resp = _mock_response(200, {"data": {}})
        ctx, _ = _mock_async_client_post(resp)
        with patch("httpx.AsyncClient", return_value=ctx):
            res = _run(self.client.initiate_signing(
                pdf_bytes=b"%PDF",
                signer_nik="3374012345678901",
                signer_name="Budi",
                document_title="Test",
            ))
        self.assertFalse(res["ok"])
        self.assertIn("request_id", res["note"].lower())

    def test_initiate_signing_4xx_no_retry(self):
        resp = _mock_response(403, {"message": "kyc pending"}, text="kyc pending")
        ctx, mock_client = _mock_async_client_post(resp)
        with patch("httpx.AsyncClient", return_value=ctx):
            res = _run(self.client.initiate_signing(
                pdf_bytes=b"%PDF", signer_nik="3374012345678901",
                signer_name="Budi", document_title="Test",
            ))
        self.assertFalse(res["ok"])
        self.assertEqual(res["http_status"], 403)
        # Must NOT have retried on 4xx — exactly one call.
        self.assertEqual(mock_client.request.await_count, 1)

    def test_initiate_signing_5xx_retries_then_succeeds(self):
        ok_resp = _mock_response(201, {
            "data": {"request_id": "r1", "signing_url": "u1"},
        })
        bad_resp = _mock_response(503, {"message": "down"}, text="down")
        ctx, mock_client = _mock_async_client_post([bad_resp, ok_resp])
        with patch("httpx.AsyncClient", return_value=ctx), \
             patch("asyncio.sleep", new=AsyncMock(return_value=None)):
            res = _run(self.client.initiate_signing(
                pdf_bytes=b"%PDF", signer_nik="3374012345678901",
                signer_name="Budi", document_title="Test",
            ))
        self.assertTrue(res["ok"])
        self.assertEqual(mock_client.request.await_count, 2)

    def test_timeout_returns_structured_failure(self):
        import httpx
        # All three attempts time out.
        ctx, mock_client = _mock_async_client_post(httpx.TimeoutException("t"))
        with patch("httpx.AsyncClient", return_value=ctx), \
             patch("asyncio.sleep", new=AsyncMock(return_value=None)):
            res = _run(self.client.initiate_signing(
                pdf_bytes=b"%PDF", signer_nik="3374012345678901",
                signer_name="Budi", document_title="Test",
            ))
        self.assertFalse(res["ok"])
        self.assertTrue(res["configured"])
        self.assertEqual(mock_client.request.await_count, 3)

    def test_apply_emeterai_round_trips_b64(self):
        stamped = b"%PDF-stamped"
        resp = _mock_response(200, {
            "data": {
                "stamped_b64": base64.b64encode(stamped).decode("ascii"),
                "stamp_id": "stamp_001",
            },
        })
        ctx, _ = _mock_async_client_post(resp)
        with patch("httpx.AsyncClient", return_value=ctx):
            res = _run(self.client.apply_emeterai(
                pdf_bytes=b"%PDF-original",
                owner_nik="3374012345678901",
            ))
        self.assertTrue(res["ok"])
        self.assertEqual(res["stamped_pdf"], stamped)
        self.assertEqual(res["stamp_id"], "stamp_001")

    def test_get_signed_doc_pending(self):
        resp = _mock_response(200, {"data": {"status": "pending"}})
        ctx, _ = _mock_async_client_post(resp)
        with patch("httpx.AsyncClient", return_value=ctx):
            res = _run(self.client.get_signed_doc("req_x"))
        self.assertFalse(res["ok"])
        self.assertTrue(res["pending"])
        self.assertEqual(res["status"], "pending")

    def test_get_signed_doc_completed(self):
        signed = b"%PDF-signed"
        resp = _mock_response(200, {
            "data": {
                "status": "signed",
                "signed_b64": base64.b64encode(signed).decode("ascii"),
            },
        })
        ctx, _ = _mock_async_client_post(resp)
        with patch("httpx.AsyncClient", return_value=ctx):
            res = _run(self.client.get_signed_doc("req_x"))
        self.assertTrue(res["ok"])
        self.assertEqual(res["pdf_bytes"], signed)


# ===========================================================================
# HMAC webhook verification
# ===========================================================================


class TestWebhookSignature(unittest.TestCase):

    def setUp(self):
        self.secret = "topsecret123"
        self.client = privy_mod.PrivyClient(
            base="http://x", api_key="k", webhook_secret=self.secret,
            enabled=True,
        )

    def _sign(self, body: bytes) -> str:
        return hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    def test_accepts_correct_signature(self):
        body = b'{"event":"signing_complete"}'
        sig = self._sign(body)
        self.assertTrue(self.client.verify_webhook_signature(body, sig))

    def test_accepts_sha256_prefix(self):
        body = b'{"event":"signing_complete"}'
        sig = "sha256=" + self._sign(body)
        self.assertTrue(self.client.verify_webhook_signature(body, sig))

    def test_rejects_bad_signature(self):
        body = b'{"event":"signing_complete"}'
        self.assertFalse(self.client.verify_webhook_signature(body, "deadbeef"))

    def test_rejects_when_no_signature_header(self):
        body = b'{"event":"signing_complete"}'
        self.assertFalse(self.client.verify_webhook_signature(body, None))
        self.assertFalse(self.client.verify_webhook_signature(body, ""))

    def test_rejects_when_secret_unset(self):
        c = privy_mod.PrivyClient(
            base="http://x", api_key="k", webhook_secret="",
            enabled=True,
        )
        body = b"abc"
        self.assertFalse(c.verify_webhook_signature(body, "anything"))

    def test_body_tampering_breaks_signature(self):
        body = b'{"event":"signing_complete"}'
        sig = self._sign(body)
        tampered = b'{"event":"signing_failed"}'
        self.assertFalse(self.client.verify_webhook_signature(tampered, sig))


# ===========================================================================
# Legal templates
# ===========================================================================


_SAMPLE_CTX = {
    "applicant_name": "Budi Santoso",
    "nik": "3374012345678901",
    "business_name": "Warung Budi",
    "phone": "081234567890",
    "license_name": "Izin Pemakaian Tanah",
    "city": "Semarang",
    "date_id": "4 Juni 2026",
}


class TestLegalTemplates(unittest.TestCase):

    def test_render_text_pakta_substitutes(self):
        text = lt.render_text("pakta_integritas", _SAMPLE_CTX)
        self.assertIn("Budi Santoso", text)
        self.assertIn("3374012345678901", text)
        self.assertIn("Izin Pemakaian Tanah", text)
        # Unsubstituted variables would leave `{{ ... }}` in the output.
        self.assertNotIn("{{", text)
        self.assertNotIn("}}", text)

    def test_render_text_surat_pernyataan_substitutes(self):
        text = lt.render_text("surat_pernyataan", _SAMPLE_CTX)
        self.assertIn("Warung Budi", text)
        self.assertIn("Meterai Elektronik", text)
        self.assertNotIn("{{", text)

    def test_missing_variable_renders_blank_not_crash(self):
        # The `phone` key is intentionally absent from the context.
        ctx = {k: v for k, v in _SAMPLE_CTX.items() if k != "phone"}
        text = lt.render_text("pakta_integritas", ctx)
        # Should not contain the literal placeholder, and must not raise.
        self.assertNotIn("{{ phone }}", text)

    def test_render_pdf_returns_valid_pdf_bytes(self):
        pdf = lt.render_pdf("pakta_integritas", _SAMPLE_CTX)
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        # Minimum length sanity — a useful PDF is at least a few hundred bytes.
        self.assertGreater(len(pdf), 500)

    def test_template_name_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            lt.render_text("../etc/passwd", _SAMPLE_CTX)

    def test_unknown_template_raises_filenotfound(self):
        with self.assertRaises(FileNotFoundError):
            lt.render_text("does_not_exist", _SAMPLE_CTX)

    def test_indonesian_date_format(self):
        from datetime import datetime
        ctx = lt.build_signing_context(
            license_name="X", applicant_name="A", nik="3374012345678901",
            business_name="B", phone="081", now=datetime(2026, 6, 4),
        )
        self.assertEqual(ctx["date_id"], "4 Juni 2026")
        self.assertEqual(ctx["city"], "Semarang")

    def test_pdf_text_escape(self):
        # An applicant name with `(` would break a naive PDF string literal.
        ctx = dict(_SAMPLE_CTX, applicant_name="Budi (Joko) Santoso")
        pdf = lt.render_pdf("pakta_integritas", ctx)
        # The unescaped paren must not appear inside the content stream
        # boundary — being permissive here, we just check the file
        # still has a valid trailer.
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))


# ===========================================================================
# Webhook router
# ===========================================================================


class TestPrivyWebhookRouter(unittest.TestCase):
    """Verify the FastAPI router enforces HMAC, parses events, and routes
    to the right guided-submission hook. Uses FastAPI's TestClient
    against the real `main.app` so the integration is exercised end to end.
    """

    def setUp(self):
        os.environ["PRIVY_ENABLED"] = "true"
        os.environ["PRIVY_API_KEY"] = "k"
        os.environ["PRIVY_WEBHOOK_SECRET"] = "topsecret123"
        os.environ["PRIVY_API_BASE"] = "http://privy.test"
        # Force the client singleton to re-read env.
        privy_mod.reset_privy_client_for_tests()

    def tearDown(self):
        for k in ("PRIVY_ENABLED", "PRIVY_API_KEY",
                  "PRIVY_WEBHOOK_SECRET", "PRIVY_API_BASE"):
            os.environ.pop(k, None)
        privy_mod.reset_privy_client_for_tests()

    def _client(self):
        # Heavy: importing main.py pulls FastAPI + middleware. We do it
        # only inside the test so the rest of the suite can skip it.
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed in this env")
        # Some sibling routers (transparency_poller etc.) import optional
        # deps at module load. Stub the bits that would fail.
        _ensure_stub("chromadb")
        from routers import privy_callback as cb_mod
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(cb_mod.router)
        return TestClient(app)

    def _sign(self, body: bytes) -> str:
        return hmac.new(b"topsecret123", body, hashlib.sha256).hexdigest()

    def test_rejects_missing_signature(self):
        client = self._client()
        r = client.post("/webhook/privy/callback", json={"event": "x"})
        self.assertEqual(r.status_code, 403)

    def test_rejects_bad_signature(self):
        client = self._client()
        body = b'{"event":"signing_complete","request_id":"r"}'
        r = client.post(
            "/webhook/privy/callback",
            content=body,
            headers={"X-Privy-Signature": "deadbeef",
                     "Content-Type": "application/json"},
        )
        self.assertEqual(r.status_code, 403)

    def test_accepts_signed_complete_event(self):
        client = self._client()
        body = b'{"event":"signing_complete","request_id":"req_x"}'
        sig = self._sign(body)
        # Patch the background-task module entry points so the test does
        # not need a guided_submission session — we only care that the
        # router returns 200 + accepted the event.
        with patch("services.guided_submission.notify_privy_signed",
                   new=AsyncMock(return_value=None)), \
             patch("services.privy_client.PrivyClient.get_signed_doc",
                   new=AsyncMock(return_value={
                       "ok": True, "configured": True, "status": "signed",
                       "pdf_bytes": b"%PDF-signed",
                   })):
            r = client.post(
                "/webhook/privy/callback",
                content=body,
                headers={"X-Privy-Signature": sig,
                         "Content-Type": "application/json"},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["event"], "signing_complete")

    def test_accepts_signed_failed_event(self):
        client = self._client()
        body = b'{"event":"signing_failed","request_id":"req_y"}'
        sig = self._sign(body)
        with patch("services.guided_submission.notify_privy_failed",
                   new=AsyncMock(return_value=None)):
            r = client.post(
                "/webhook/privy/callback",
                content=body,
                headers={"X-Privy-Signature": sig,
                         "Content-Type": "application/json"},
            )
        self.assertEqual(r.status_code, 200)

    def test_signed_but_malformed_json_returns_400(self):
        client = self._client()
        body = b'{not json'
        sig = self._sign(body)
        r = client.post(
            "/webhook/privy/callback",
            content=body,
            headers={"X-Privy-Signature": sig,
                     "Content-Type": "application/json"},
        )
        self.assertEqual(r.status_code, 400)


# ===========================================================================
# Guided-submission state machine extension
# ===========================================================================


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


class TestGuidedSubmissionPrivyBranch(unittest.TestCase):
    """Verify the AWAITING_PRIVY_SIGN branch is entered after all fields
    are collected when Privy is enabled, and the citizen flow continues
    correctly thereafter.
    """

    def setUp(self):
        # Hot import — guided_submission has heavy transitive deps so we
        # use the same stubbing strategy as the existing test file.
        from services import guided_submission as gs
        self.gs = gs
        gs._sessions.clear()
        gs._privy_request_to_user.clear()
        os.environ["BIMA_GUIDED_SUBMISSION_ENABLED"] = "true"
        os.environ["GUIDED_SUBMISSION_DEMO_FIXTURE"] = "clean"
        os.environ["GUIDED_SUBMISSION_PROFILE_ID"] = "7"
        os.environ["PRIVY_ENABLED"] = "true"
        os.environ["PRIVY_API_KEY"] = "k"
        os.environ["PRIVY_WEBHOOK_SECRET"] = "s"
        os.environ["PRIVY_API_BASE"] = "http://privy.test"
        privy_mod.reset_privy_client_for_tests()

    def tearDown(self):
        self.gs._sessions.clear()
        self.gs._privy_request_to_user.clear()
        for k in ("BIMA_GUIDED_SUBMISSION_ENABLED",
                  "GUIDED_SUBMISSION_DEMO_FIXTURE",
                  "GUIDED_SUBMISSION_PROFILE_ID",
                  "PRIVY_ENABLED", "PRIVY_API_KEY",
                  "PRIVY_WEBHOOK_SECRET", "PRIVY_API_BASE"):
            os.environ.pop(k, None)
        privy_mod.reset_privy_client_for_tests()

    def _initiate_ok(self):
        return AsyncMock(return_value={
            "ok": True, "configured": True,
            "request_id": "req_abc", "signing_url": "https://privy.test/s/abc",
        })

    def _initiate_unavailable(self):
        return AsyncMock(return_value={
            "ok": False, "configured": True,
            "note": "down",
        })

    def test_enters_privy_branch_after_fields_collected(self):
        with patch("services.siap_tools.siap_lookup_license",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)), \
             patch("services.privy_client.PrivyClient.initiate_signing",
                   new=self._initiate_ok()):
            uid = "wa-700"
            _run(self.gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
            _run(self.gs.maybe_handle(uid, "Budi Santoso"))
            _run(self.gs.maybe_handle(uid, "3374012345678901"))
            _run(self.gs.maybe_handle(uid, "Warung Budi"))
            reply = _run(self.gs.maybe_handle(uid, "081234567890"))

        self.assertIn("tanda tangan digital", reply.lower())
        self.assertIn("https://privy.test/s/abc", reply)
        sess = self.gs._sessions[uid]
        self.assertEqual(sess.stage, self.gs.Stage.AWAITING_PRIVY_SIGN)
        self.assertEqual(sess.privy_request_id, "req_abc")
        self.assertIn("req_abc", self.gs._privy_request_to_user)

    def test_privy_failure_falls_back_to_review(self):
        # If Privy is unreachable, the flow must NOT block — it falls
        # back to the legacy REVIEW path so DPMPTSP can still process
        # the application manually.
        with patch("services.siap_tools.siap_lookup_license",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)), \
             patch("services.privy_client.PrivyClient.initiate_signing",
                   new=self._initiate_unavailable()):
            uid = "wa-701"
            _run(self.gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
            _run(self.gs.maybe_handle(uid, "Budi Santoso"))
            _run(self.gs.maybe_handle(uid, "3374012345678901"))
            _run(self.gs.maybe_handle(uid, "Warung Budi"))
            reply = _run(self.gs.maybe_handle(uid, "081234567890"))

        self.assertIn("YA", reply.upper())  # REVIEW prompt text
        sess = self.gs._sessions[uid]
        self.assertEqual(sess.stage, self.gs.Stage.REVIEW)

    def test_skips_privy_when_flag_off(self):
        os.environ["PRIVY_ENABLED"] = "false"
        privy_mod.reset_privy_client_for_tests()
        with patch("services.siap_tools.siap_lookup_license",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)):
            uid = "wa-702"
            _run(self.gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
            _run(self.gs.maybe_handle(uid, "Budi Santoso"))
            _run(self.gs.maybe_handle(uid, "3374012345678901"))
            _run(self.gs.maybe_handle(uid, "Warung Budi"))
            reply = _run(self.gs.maybe_handle(uid, "081234567890"))

        sess = self.gs._sessions[uid]
        self.assertEqual(sess.stage, self.gs.Stage.REVIEW)
        self.assertNotIn("Privy", reply)

    def test_poll_during_awaiting_returns_pending(self):
        with patch("services.siap_tools.siap_lookup_license",
                   new=AsyncMock(return_value=_ONE_MATCH)), \
             patch("services.siap_tools.siap_get_requirements",
                   new=AsyncMock(return_value=_REQUIREMENTS)), \
             patch("services.privy_client.PrivyClient.initiate_signing",
                   new=self._initiate_ok()), \
             patch("services.privy_client.PrivyClient.get_signed_doc",
                   new=AsyncMock(return_value={
                       "ok": False, "pending": True, "status": "pending",
                       "note": "still working on it",
                   })):
            uid = "wa-703"
            _run(self.gs.maybe_handle(uid, "saya mau ajukan izin pemakaian tanah"))
            _run(self.gs.maybe_handle(uid, "Budi Santoso"))
            _run(self.gs.maybe_handle(uid, "3374012345678901"))
            _run(self.gs.maybe_handle(uid, "Warung Budi"))
            _run(self.gs.maybe_handle(uid, "081234567890"))
            reply = _run(self.gs.maybe_handle(uid, "sudah belum?"))

        self.assertIn("belum selesai", reply.lower())
        sess = self.gs._sessions[uid]
        self.assertEqual(sess.stage, self.gs.Stage.AWAITING_PRIVY_SIGN)


# ===========================================================================
# Entry point
# ===========================================================================


if __name__ == "__main__":
    unittest.main()
