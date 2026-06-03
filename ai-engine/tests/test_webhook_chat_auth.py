"""
Tests for the /webhook/chat auth gate (security/gate-webhook-chat).

Run standalone (no pytest needed — matches tests/test_siap_write.py):

    python -m tests.test_webhook_chat_auth      # from ai-engine/
    python tests/test_webhook_chat_auth.py      # also works

What it covers:
  * No auth headers                         → 401
  * Invalid X-Internal-Key                  → 403
  * Valid X-Internal-Key                    → 200 (body user_id trusted)
  * No internal key configured              → 503 path is not triggered for
                                              X-Internal-Key when JWT is also
                                              valid (we test JWT-only env)
  * Valid citizen JWT, matching user_id     → 200
  * Valid citizen JWT, mismatched user_id   → 403
  * Citizen JWT with wrong `kind`           → 403
  * Expired citizen JWT                     → 401
  * Tampered citizen JWT signature          → 401
  * Body validation still works under auth (empty message → 422)

The downstream ai_handler is mocked so we never reach the real LLM /
ChromaDB stack. The tests assert on:
  - the HTTP response (status + body)
  - whether generate_ai_response was called at all
  - the user_id it received (the session-hijack defense — JWT path
    must pass the JWT-derived id, not the body-supplied one)

No real network, no real LLM, no real DB.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Make `deps`, `routers`, `services` importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Stub the heavy ML / DB deps the chat router transitively pulls. None of
# them are exercised in this file — `generate_ai_response` is patched at the
# module-attribute level — but the import graph must still resolve.
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


_ensure_stub("chromadb")
_ensure_stub("chromadb.config", {"Settings": object})
_ensure_stub("asyncpg")
_ensure_stub("sentence_transformers", {"SentenceTransformer": object})
_ensure_stub("transformers")


# ---------------------------------------------------------------------------
# Test env. Set BEFORE importing `deps` because deps reads env at module
# import time (matches the rest of ai-engine's "os.getenv at import" idiom).
# ---------------------------------------------------------------------------
_TEST_INTERNAL_KEY = "test-internal-key-very-long-and-secret-do-not-use-irl"
_TEST_JWT_SECRET = "test-jwt-secret-also-long-and-not-a-placeholder-value"

os.environ["INTERNAL_API_KEY"] = _TEST_INTERNAL_KEY
os.environ["CITIZEN_JWT_SECRET"] = _TEST_JWT_SECRET
os.environ["JWT_ALGORITHM"] = "HS256"

# Force a clean re-import of deps so it picks up our env. If a prior test
# already imported it, the module-level constants are stale.
if "deps" in sys.modules:
    importlib.reload(sys.modules["deps"])

from jose import jwt  # noqa: E402

import deps  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# ---------------------------------------------------------------------------
# Build a tiny app that mounts only the webhooks router, with the
# downstream ai_handler functions patched. We never want a real LLM call.
# ---------------------------------------------------------------------------
def _make_app() -> tuple[FastAPI, AsyncMock]:
    """
    Returns (app, mock_generate). `mock_generate` is what the router will
    call instead of the real LLM. The test asserts on its call_args to
    confirm the right user_id was passed through.
    """
    # Import lazily so the patched env above is in effect.
    from routers import webhooks  # noqa: WPS433

    mock_generate = AsyncMock(return_value="hello from mocked LLM")
    mock_log = AsyncMock(return_value=None)

    # Patch the names the router actually called bind to. ai_handler may
    # be a heavy module — we patch the symbols already imported into the
    # router's namespace, which is the cheapest surgical mock.
    webhooks.generate_ai_response = mock_generate  # type: ignore[attr-defined]
    webhooks.log_to_backend = mock_log  # type: ignore[attr-defined]

    app = FastAPI()
    app.include_router(webhooks.router)
    return app, mock_generate


def _make_citizen_jwt(
    *,
    sub: str | int,
    kind: str = "pemohon",
    expires_in_seconds: int = 3600,
    secret: str = _TEST_JWT_SECRET,
) -> str:
    """Mint a JWT that mimics admin-api `create_token_with_claims` output."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(sub),
        "kind": kind,
        "name": "Test Citizen",
        "identity_number": "1234567890123456",
        "identity_type": "KTP",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in_seconds)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


class WebhookChatAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.mock_generate = _make_app()
        self.client = TestClient(self.app)
        self.payload = {"user_id": "web-42", "message": "halo bima"}

    # ----- no auth ---------------------------------------------------------
    def test_no_headers_401(self) -> None:
        r = self.client.post("/webhook/chat", json=self.payload)
        self.assertEqual(r.status_code, 401)
        self.mock_generate.assert_not_called()

    def test_no_headers_stream_401(self) -> None:
        r = self.client.post("/webhook/chat/stream", json=self.payload)
        self.assertEqual(r.status_code, 401)
        self.mock_generate.assert_not_called()

    # ----- X-Internal-Key path --------------------------------------------
    def test_valid_internal_key_200_trusts_body_user_id(self) -> None:
        r = self.client.post(
            "/webhook/chat",
            json=self.payload,
            headers={"X-Internal-Key": _TEST_INTERNAL_KEY},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("response", r.json())
        # Body user_id ("web-42") was trusted as-is.
        self.mock_generate.assert_awaited_once()
        called_user_id = self.mock_generate.await_args.args[0]
        self.assertEqual(called_user_id, "web-42")

    def test_invalid_internal_key_403(self) -> None:
        r = self.client.post(
            "/webhook/chat",
            json=self.payload,
            headers={"X-Internal-Key": "wrong-key"},
        )
        self.assertEqual(r.status_code, 403)
        self.mock_generate.assert_not_called()

    def test_empty_internal_key_treated_as_missing_401(self) -> None:
        # An empty header string must NOT be a free pass — many proxies
        # forward unset headers as the empty string.
        r = self.client.post(
            "/webhook/chat",
            json=self.payload,
            headers={"X-Internal-Key": ""},
        )
        self.assertEqual(r.status_code, 401)
        self.mock_generate.assert_not_called()

    # ----- citizen JWT path -----------------------------------------------
    def test_valid_jwt_matching_user_id_200(self) -> None:
        token = _make_citizen_jwt(sub=42)
        r = self.client.post(
            "/webhook/chat",
            json={"user_id": "web-42", "message": "halo"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.mock_generate.assert_awaited_once()
        called_user_id = self.mock_generate.await_args.args[0]
        # JWT-derived id, not whatever the body said (even though they match).
        self.assertEqual(called_user_id, "web-42")

    def test_valid_jwt_with_no_body_user_id_field_rejected_by_schema(self) -> None:
        # The schema still requires user_id; we don't silently drop it
        # because then a client can never tell if the server respected it.
        token = _make_citizen_jwt(sub=42)
        r = self.client.post(
            "/webhook/chat",
            json={"message": "halo"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 422)
        self.mock_generate.assert_not_called()

    def test_valid_jwt_mismatched_user_id_403(self) -> None:
        # The session-hijack defense: token says sub=42 (i.e. web-42),
        # body says web-7. Must 403, must NOT call the LLM.
        token = _make_citizen_jwt(sub=42)
        r = self.client.post(
            "/webhook/chat",
            json={"user_id": "web-7", "message": "halo"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 403)
        self.mock_generate.assert_not_called()

    def test_admin_kind_jwt_rejected_403(self) -> None:
        # Admin tokens must not reach the citizen-chat surface.
        token = _make_citizen_jwt(sub=42, kind="admin")
        r = self.client.post(
            "/webhook/chat",
            json={"user_id": "web-42", "message": "halo"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 403)
        self.mock_generate.assert_not_called()

    def test_expired_jwt_401(self) -> None:
        token = _make_citizen_jwt(sub=42, expires_in_seconds=-60)
        r = self.client.post(
            "/webhook/chat",
            json={"user_id": "web-42", "message": "halo"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 401)
        self.mock_generate.assert_not_called()

    def test_tampered_signature_401(self) -> None:
        # Wrong secret = wrong signature.
        token = _make_citizen_jwt(sub=42, secret="some-other-secret")
        r = self.client.post(
            "/webhook/chat",
            json={"user_id": "web-42", "message": "halo"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 401)
        self.mock_generate.assert_not_called()

    def test_garbage_bearer_token_401(self) -> None:
        r = self.client.post(
            "/webhook/chat",
            json={"user_id": "web-42", "message": "halo"},
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        self.assertEqual(r.status_code, 401)
        self.mock_generate.assert_not_called()

    # ----- precedence: internal key wins over bearer ---------------------
    def test_both_headers_internal_key_wins(self) -> None:
        # If a client somehow ships both, the internal key path is taken
        # and the body user_id is trusted. This is intentional — internal
        # callers are server-tier and may want to address any session.
        token = _make_citizen_jwt(sub=42)
        r = self.client.post(
            "/webhook/chat",
            json={"user_id": "web-7", "message": "halo"},
            headers={
                "X-Internal-Key": _TEST_INTERNAL_KEY,
                "Authorization": f"Bearer {token}",
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        called_user_id = self.mock_generate.await_args.args[0]
        self.assertEqual(called_user_id, "web-7")

    # ----- body validation still bites under auth ------------------------
    def test_empty_message_422_even_with_auth(self) -> None:
        r = self.client.post(
            "/webhook/chat",
            json={"user_id": "web-42", "message": "   "},
            headers={"X-Internal-Key": _TEST_INTERNAL_KEY},
        )
        # Pydantic field_validator rejects whitespace-only.
        self.assertEqual(r.status_code, 422)
        self.mock_generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
