"""
Tests for the native WhatsApp typing+read enhancement.

Covers:
  * services/meta_whatsapp_sender.mark_read_and_typing — the read+typing
    payload shape and every outcome with httpx.AsyncClient mocked
    (not-configured, 200 success, non-200 e.g. error-190, transport error).
  * services/whatsapp_sender.acknowledge_received — the four fallback
    branches required by the plan:
      (a) flag OFF                         → only send_text (text bubble) fires
      (b) flag ON + native True + !KEEP    → send_text NOT called (no duplicate)
      (c) flag ON + native raises          → send_text called (fallback)
      (d) flag ON but META_WA_* missing    → send_text called + hint logged once

Run standalone (no pytest needed — matches tests/test_siap_write.py):

    python -m tests.test_whatsapp_typing     # from ai-engine/
    python tests/test_whatsapp_typing.py     # also works

httpx is mocked at the AsyncClient boundary — NO real network, NO real
WhatsApp/Meta/APTANA call is ever made. The access token never appears in
any assertion or payload.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Make `services` importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _run(coro):
    return asyncio.run(coro)


def _fake_async_client(response: MagicMock | None = None, raise_exc: Exception | None = None):
    """Build a MagicMock usable as `httpx.AsyncClient(...)` context manager.

    If raise_exc is set, the .post call raises it. Otherwise .post returns
    `response`. Returns (client_factory, captured) where captured["call"] holds
    the kwargs the code passed to client.post so we can assert the payload.
    """
    captured: dict = {}

    async def _post(url, json=None, headers=None):  # noqa: A002 - mirrors httpx
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        if raise_exc is not None:
            raise raise_exc
        return response

    client = MagicMock()
    client.post = AsyncMock(side_effect=_post)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    return factory, captured


class MetaStatusSenderTests(unittest.TestCase):
    """services/meta_whatsapp_sender.mark_read_and_typing."""

    def _load_module_with_env(self, **env):
        """Import a FRESH copy of meta_whatsapp_sender with the given env so the
        module-level _PHONE_NUMBER_ID / _ACCESS_TOKEN reflect the test config."""
        with patch.dict(os.environ, env, clear=False):
            if "services.meta_whatsapp_sender" in sys.modules:
                del sys.modules["services.meta_whatsapp_sender"]
            return importlib.import_module("services.meta_whatsapp_sender")

    def test_payload_shape_and_success(self):
        mod = self._load_module_with_env(
            META_WA_PHONE_NUMBER_ID="967104853150201",
            META_WA_TOKEN="SECRET_TOKEN_VALUE",
            META_GRAPH_VERSION="v22.0",
        )
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '{"success":true}'
        factory, captured = _fake_async_client(response=resp)
        with patch.object(mod.httpx, "AsyncClient", factory):
            ok = _run(mod.mark_read_and_typing("wamid.HBgABC"))
        self.assertTrue(ok)
        # Exact read+typing payload shape.
        self.assertEqual(
            captured["json"],
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": "wamid.HBgABC",
                "typing_indicator": {"type": "text"},
            },
        )
        # Version-pinned messages URL with the phone-number-id (not the number).
        self.assertEqual(
            captured["url"],
            "https://graph.facebook.com/v22.0/967104853150201/messages",
        )
        # Token travels ONLY as a Bearer header, never elsewhere in the payload.
        self.assertEqual(captured["headers"]["Authorization"], "Bearer SECRET_TOKEN_VALUE")
        self.assertNotIn("SECRET_TOKEN_VALUE", str(captured["json"]))

    def test_show_typing_false_omits_indicator(self):
        mod = self._load_module_with_env(
            META_WA_PHONE_NUMBER_ID="111", META_WA_TOKEN="tok",
        )
        resp = MagicMock(); resp.status_code = 200; resp.text = "{}"
        factory, captured = _fake_async_client(response=resp)
        with patch.object(mod.httpx, "AsyncClient", factory):
            ok = _run(mod.mark_read_and_typing("wamid.X", show_typing=False))
        self.assertTrue(ok)
        self.assertNotIn("typing_indicator", captured["json"])
        self.assertEqual(captured["json"]["status"], "read")

    def test_not_configured_returns_false_no_call(self):
        mod = self._load_module_with_env(
            META_WA_PHONE_NUMBER_ID="", META_WA_TOKEN="",
        )
        factory, _ = _fake_async_client(response=MagicMock(status_code=200))
        with patch.object(mod.httpx, "AsyncClient", factory):
            ok = _run(mod.mark_read_and_typing("wamid.X"))
        self.assertFalse(ok)
        factory.assert_not_called()  # never even constructs a client

    def test_empty_message_id_returns_false(self):
        mod = self._load_module_with_env(
            META_WA_PHONE_NUMBER_ID="111", META_WA_TOKEN="tok",
        )
        factory, _ = _fake_async_client(response=MagicMock(status_code=200))
        with patch.object(mod.httpx, "AsyncClient", factory):
            ok = _run(mod.mark_read_and_typing(""))
        self.assertFalse(ok)
        factory.assert_not_called()

    def test_non_200_error_190_returns_false(self):
        # Simulates Meta rejecting the call because the number is registered to
        # APTANA's app — error 190 / "registered to another app".
        mod = self._load_module_with_env(
            META_WA_PHONE_NUMBER_ID="111", META_WA_TOKEN="tok",
        )
        resp = MagicMock()
        resp.status_code = 400
        resp.text = '{"error":{"message":"registered to another app","code":190}}'
        factory, _ = _fake_async_client(response=resp)
        with patch.object(mod.httpx, "AsyncClient", factory):
            ok = _run(mod.mark_read_and_typing("wamid.X"))
        self.assertFalse(ok)

    def test_transport_error_returns_false(self):
        mod = self._load_module_with_env(
            META_WA_PHONE_NUMBER_ID="111", META_WA_TOKEN="tok",
        )
        factory, _ = _fake_async_client(raise_exc=mod.httpx.ConnectError("boom"))
        with patch.object(mod.httpx, "AsyncClient", factory):
            ok = _run(mod.mark_read_and_typing("wamid.X"))
        self.assertFalse(ok)


class AcknowledgeReceivedBranchTests(unittest.TestCase):
    """services/whatsapp_sender.acknowledge_received — the 4 fallback branches.

    APTANA creds are forced present so the early `_API_TOKEN/_SENDER_NUMBER`
    guard never short-circuits; send_text itself is mocked so nothing is sent.
    """

    APTANA_ENV = {
        "APTANA_API_TOKEN": "aptana_tok",
        "APTANA_SENDER_NUMBER": "6285117557091",
    }

    def _load_ws(self, **extra_env):
        env = {**self.APTANA_ENV, **extra_env}
        with patch.dict(os.environ, env, clear=False):
            # Reload BOTH modules INSIDE the env context so meta_whatsapp_sender's
            # module-level _PHONE_NUMBER_ID/_ACCESS_TOKEN capture this test's env.
            # acknowledge_received lazily imports those module globals at call
            # time, so the meta module must already be in sys.modules with the
            # right config — otherwise a later fresh import (after patch.dict
            # reverts) would re-read empty config. Importing it here pins it.
            for name in ("services.meta_whatsapp_sender", "services.whatsapp_sender"):
                if name in sys.modules:
                    del sys.modules[name]
            importlib.import_module("services.meta_whatsapp_sender")
            return importlib.import_module("services.whatsapp_sender")

    def test_a_flag_off_only_text_bubble(self):
        ws = self._load_ws(BIMA_NATIVE_TYPING_ENABLED="false")
        with patch.object(ws, "send_text", new=AsyncMock(return_value=True)) as st:
            # Even if the meta module were importable, the flag-off path must
            # never reach it — patch it to blow up if called.
            with patch(
                "services.meta_whatsapp_sender.mark_read_and_typing",
                new=AsyncMock(side_effect=AssertionError("native must not run")),
            ):
                _run(ws.acknowledge_received("wamid.X", "6285117557091"))
        st.assert_awaited_once()
        self.assertEqual(st.await_args.kwargs["body"], ws._ACK_BODY)

    def test_b_native_success_skips_text_bubble(self):
        ws = self._load_ws(
            BIMA_NATIVE_TYPING_ENABLED="true",
            BIMA_NATIVE_TYPING_KEEP_TEXT_BUBBLE="false",
            META_WA_PHONE_NUMBER_ID="967104853150201",
            META_WA_TOKEN="tok",
        )
        with patch.object(ws, "send_text", new=AsyncMock(return_value=True)) as st, patch(
            "services.meta_whatsapp_sender.mark_read_and_typing",
            new=AsyncMock(return_value=True),
        ) as native:
            _run(ws.acknowledge_received("wamid.X", "6285117557091"))
        native.assert_awaited_once_with("wamid.X")
        st.assert_not_awaited()  # no duplicate text bubble

    def test_b2_native_success_keep_bubble_sends_both(self):
        ws = self._load_ws(
            BIMA_NATIVE_TYPING_ENABLED="true",
            BIMA_NATIVE_TYPING_KEEP_TEXT_BUBBLE="true",
            META_WA_PHONE_NUMBER_ID="967104853150201",
            META_WA_TOKEN="tok",
        )
        with patch.object(ws, "send_text", new=AsyncMock(return_value=True)) as st, patch(
            "services.meta_whatsapp_sender.mark_read_and_typing",
            new=AsyncMock(return_value=True),
        ) as native:
            _run(ws.acknowledge_received("wamid.X", "6285117557091"))
        native.assert_awaited_once()
        st.assert_awaited_once()  # belt-and-suspenders: both fire

    def test_c_native_raises_falls_back_to_text(self):
        ws = self._load_ws(
            BIMA_NATIVE_TYPING_ENABLED="true",
            META_WA_PHONE_NUMBER_ID="967104853150201",
            META_WA_TOKEN="tok",
        )
        with patch.object(ws, "send_text", new=AsyncMock(return_value=True)) as st, patch(
            "services.meta_whatsapp_sender.mark_read_and_typing",
            new=AsyncMock(side_effect=RuntimeError("kaboom")),
        ):
            # Must NOT raise — the handler swallows and falls back.
            _run(ws.acknowledge_received("wamid.X", "6285117557091"))
        st.assert_awaited_once()
        self.assertEqual(st.await_args.kwargs["body"], ws._ACK_BODY)

    def test_c2_native_returns_false_falls_back_to_text(self):
        # Meta rejected (e.g. error 190) → mark_read_and_typing returns False.
        ws = self._load_ws(
            BIMA_NATIVE_TYPING_ENABLED="true",
            META_WA_PHONE_NUMBER_ID="967104853150201",
            META_WA_TOKEN="tok",
        )
        with patch.object(ws, "send_text", new=AsyncMock(return_value=True)) as st, patch(
            "services.meta_whatsapp_sender.mark_read_and_typing",
            new=AsyncMock(return_value=False),
        ):
            _run(ws.acknowledge_received("wamid.X", "6285117557091"))
        st.assert_awaited_once()

    def test_d_flag_on_config_missing_falls_back_and_hints_once(self):
        # Flag on but META_WA_* unset → text bubble + a single INFO hint.
        ws = self._load_ws(
            BIMA_NATIVE_TYPING_ENABLED="true",
            META_WA_PHONE_NUMBER_ID="",
            META_WA_TOKEN="",
        )
        with patch.object(ws, "send_text", new=AsyncMock(return_value=True)) as st, patch(
            "services.meta_whatsapp_sender.mark_read_and_typing",
            new=AsyncMock(side_effect=AssertionError("must not call when unconfigured")),
        ), patch.object(ws.logger, "info") as log_info:
            _run(ws.acknowledge_received("wamid.X", "6285117557091"))
            _run(ws.acknowledge_received("wamid.Y", "6285117557091"))
        # Text bubble both times.
        self.assertEqual(st.await_count, 2)
        # Hint logged exactly once (module-level _native_hint_logged guard).
        hint_calls = [c for c in log_info.call_args_list if "META_WA_* unset" in str(c)]
        self.assertEqual(len(hint_calls), 1)

    def test_e_flag_on_no_message_id_falls_back(self):
        # Configured + enabled but message_id is None → no native call, bubble.
        ws = self._load_ws(
            BIMA_NATIVE_TYPING_ENABLED="true",
            META_WA_PHONE_NUMBER_ID="967104853150201",
            META_WA_TOKEN="tok",
        )
        with patch.object(ws, "send_text", new=AsyncMock(return_value=True)) as st, patch(
            "services.meta_whatsapp_sender.mark_read_and_typing",
            new=AsyncMock(side_effect=AssertionError("no msg id → no native call")),
        ):
            _run(ws.acknowledge_received(None, "6285117557091"))
        st.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
