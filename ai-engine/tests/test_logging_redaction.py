"""Tests for logging_setup.py — verify bot tokens, webhook secrets, and PII
never leak into log output.

Run standalone (no pytest required — matches the other tests in this dir):

    python -m tests.test_logging_redaction    # from ai-engine/
    python tests/test_logging_redaction.py    # also works

Threat model under test:
  - Operator with access to container stdout / log aggregator.
  - Wants to recover the Telegram bot token, Meta access token, or the
    webhook path-secret to impersonate the platform or forge inbound traffic.

What the redaction layer must guarantee:
  1. `mask_url_secrets` rewrites every known credential-bearing URL shape.
  2. `mask_pii` rewrites NIK / msisdn / channel-prefixed user_id.
  3. The logging.Filter applies BOTH passes to format string AND args.
  4. The synthetic Telegram inbound log line contains no full token or
     full webhook secret after the filter is installed.
"""

from __future__ import annotations

import io
import logging
import sys
import unittest
from pathlib import Path

# Make `logging_setup` importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logging_setup import (  # noqa: E402
    SecretRedactingFilter,
    configure_logging,
    mask_pii,
    mask_url_secrets,
)


# Realistic-looking but fake secrets used throughout the suite.
_FAKE_TG_TOKEN = "123456789:AAEhBP0av28pFa3KZ6F-fakeFAKEfake123abc"
_FAKE_TG_WEBHOOK_SECRET = "f3a8e9c1d2b4e5f60718293a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c"
_FAKE_APTANA_SECRET = "deadbeefcafefacebabe0123456789abcdef0123456789abcdef0123456789ab"
_FAKE_META_TOKEN = "EAAGm0PX4ZCpsBOzqK4fakeMetaTokenZ1Q2W3E4R5T6Y7"
_FAKE_GEMINI_KEY = "AIzaSyD_FAKE_gemini_key_1234567890abcdEFGHijk"


class TestMaskUrlSecrets(unittest.TestCase):
    """mask_url_secrets — purely textual scrubbing of credentials in URLs."""

    def test_telegram_bot_token_in_url_is_redacted(self) -> None:
        url = f"https://api.telegram.org/bot{_FAKE_TG_TOKEN}/sendMessage"
        out = mask_url_secrets(url)
        self.assertNotIn(_FAKE_TG_TOKEN, out)
        self.assertIn("bot<redacted>/sendMessage", out)

    def test_telegram_token_in_log_line_is_redacted(self) -> None:
        # Mirrors httpx's INFO line: 'HTTP Request: POST https://...'
        line = (
            f"HTTP Request: POST https://api.telegram.org/bot{_FAKE_TG_TOKEN}/"
            "sendMessage \"HTTP/1.1 200 OK\""
        )
        out = mask_url_secrets(line)
        self.assertNotIn(_FAKE_TG_TOKEN, out)
        # Surrounding context preserved
        self.assertIn("HTTP/1.1 200 OK", out)

    def test_meta_graph_access_token_query_param_is_redacted(self) -> None:
        url = (
            "https://graph.facebook.com/v18.0/123456/messages"
            f"?access_token={_FAKE_META_TOKEN}"
        )
        out = mask_url_secrets(url)
        self.assertNotIn(_FAKE_META_TOKEN, out)
        self.assertIn("access_token=<redacted>", out)

    def test_gemini_generativelanguage_key_query_param_is_redacted(self) -> None:
        # httpx's INFO line for a Gemini/Gemma call leaks the API key as ?key=
        line = (
            "HTTP Request: POST https://generativelanguage.googleapis.com/v1beta/"
            f"models/gemini-2.5-flash:generateContent?key={_FAKE_GEMINI_KEY} "
            '"HTTP/1.1 429 Too Many Requests"'
        )
        out = mask_url_secrets(line)
        self.assertNotIn(_FAKE_GEMINI_KEY, out)
        self.assertIn("key=<redacted>", out)
        self.assertIn("HTTP/1.1 429 Too Many Requests", out)  # context preserved

    def test_telegram_webhook_path_secret_is_redacted(self) -> None:
        path = f"/webhook/telegram/{_FAKE_TG_WEBHOOK_SECRET}"
        out = mask_url_secrets(path)
        self.assertNotIn(_FAKE_TG_WEBHOOK_SECRET, out)
        self.assertEqual(out, "/webhook/telegram/<redacted>")

    def test_aptana_inbound_secret_is_redacted(self) -> None:
        for kind in ("inbound", "status", "greeting"):
            with self.subTest(kind=kind):
                path = f"/webhook/aptana/{kind}/{_FAKE_APTANA_SECRET}"
                out = mask_url_secrets(path)
                self.assertNotIn(_FAKE_APTANA_SECRET, out)
                self.assertEqual(out, f"/webhook/aptana/{kind}/<redacted>")

    def test_unrelated_url_is_untouched(self) -> None:
        url = "https://nolongin.com/admin-api/case/REG-2026-0001"
        self.assertEqual(mask_url_secrets(url), url)

    def test_idempotent(self) -> None:
        line = f"GET https://api.telegram.org/bot{_FAKE_TG_TOKEN}/getMe"
        once = mask_url_secrets(line)
        twice = mask_url_secrets(once)
        self.assertEqual(once, twice)

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(mask_url_secrets(""), "")
        # Note: mask_url_secrets only handles str; callers shouldn't pass None.


class TestMaskPii(unittest.TestCase):
    """mask_pii — phone numbers, NIK, channel-prefixed user_ids."""

    def test_wa_user_id_masks_msisdn_tail_visible(self) -> None:
        out = mask_pii("user_id=wa-628123456789 status=ok")
        self.assertNotIn("628123456789", out)
        self.assertIn("user_id=wa-****6789", out)
        self.assertIn("status=ok", out)

    def test_tg_user_id_masks_chat_id(self) -> None:
        out = mask_pii("Rate limit hit | user_id=tg-987654321 | count=6")
        self.assertNotIn("987654321", out)
        self.assertIn("user_id=tg-****4321", out)

    def test_bare_msisdn_field_is_masked(self) -> None:
        out = mask_pii("APTANA send | msisdn=628111222333 attempt=1")
        self.assertNotIn("628111222333", out)
        self.assertIn("msisdn=****2333", out)

    def test_nik_is_masked(self) -> None:
        out = mask_pii("submit | nik=3301234567890123 status=ok")
        self.assertNotIn("3301234567890123", out)
        self.assertIn("0123", out)
        # Bulk of the NIK is starred out
        self.assertIn("************", out)

    def test_unrelated_text_untouched(self) -> None:
        text = "RAG returned 0 usable chunks (no PII here)"
        self.assertEqual(mask_pii(text), text)


class TestFilterIntegration(unittest.TestCase):
    """End-to-end: install the filter, emit a log line, capture stdout-shape
    output via an in-memory handler, assert no secrets present."""

    def _make_logger(
        self, name: str
    ) -> tuple[logging.Logger, io.StringIO, logging.Handler]:
        log = logging.getLogger(name)
        log.handlers.clear()  # isolate from prior test runs
        log.setLevel(logging.INFO)
        log.propagate = False
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
        handler.addFilter(SecretRedactingFilter())
        log.addHandler(handler)
        return log, buf, handler

    def test_synthetic_telegram_inbound_no_token_in_output(self) -> None:
        """The headline regression test — request-id middleware logs a path
        that contains the Telegram webhook secret, AND httpx logs the bot
        token in its outbound URL. After installing the filter, NEITHER
        secret may appear in the final log buffer."""
        log, buf, _ = self._make_logger("test.tg.integration")

        # Simulate the middleware line ("path=… method=POST …")
        log.info(
            "path=%s method=POST status=200 request_id=abc",
            f"/webhook/telegram/{_FAKE_TG_WEBHOOK_SECRET}",
        )

        # Simulate httpx's outbound INFO line when calling sendMessage
        log.info(
            'HTTP Request: POST https://api.telegram.org/bot%s/sendMessage'
            ' "HTTP/1.1 200 OK"',
            _FAKE_TG_TOKEN,
        )

        contents = buf.getvalue()
        self.assertNotIn(_FAKE_TG_TOKEN, contents)
        self.assertNotIn(_FAKE_TG_WEBHOOK_SECRET, contents)
        self.assertIn("/webhook/telegram/<redacted>", contents)
        self.assertIn("bot<redacted>/sendMessage", contents)
        # The non-secret parts of the line must still be visible for debugging
        self.assertIn("HTTP/1.1 200 OK", contents)
        self.assertIn("method=POST", contents)

    def test_filter_handles_dict_args(self) -> None:
        """httpx variants and some libraries log with %(...)s — dict args."""
        log, buf, _ = self._make_logger("test.dict.args")
        log.info(
            "outbound %(method)s %(url)s",
            {"method": "POST", "url": f"https://api.telegram.org/bot{_FAKE_TG_TOKEN}/x"},
        )
        self.assertNotIn(_FAKE_TG_TOKEN, buf.getvalue())

    def test_filter_handles_pre_interpolated_msg(self) -> None:
        """If a caller did f-string the URL into msg already, we still mask."""
        log, buf, _ = self._make_logger("test.preinterp")
        log.info(f"outbound POST https://api.telegram.org/bot{_FAKE_TG_TOKEN}/x")
        self.assertNotIn(_FAKE_TG_TOKEN, buf.getvalue())

    def test_filter_masks_pii_too(self) -> None:
        log, buf, _ = self._make_logger("test.pii")
        log.info("rate limit hit | user_id=%s | count=%d", "wa-628111222333", 6)
        contents = buf.getvalue()
        self.assertNotIn("628111222333", contents)
        self.assertIn("****2333", contents)


class TestConfigureLogging(unittest.TestCase):
    """configure_logging installs filters in the right places and is idempotent."""

    def test_idempotent(self) -> None:
        # Reset httpx logger to a clean state, then call configure_logging twice.
        httpx_log = logging.getLogger("httpx")
        httpx_log.filters = [
            f for f in httpx_log.filters if not isinstance(f, SecretRedactingFilter)
        ]

        configure_logging()
        first_count = sum(
            1 for f in httpx_log.filters if isinstance(f, SecretRedactingFilter)
        )
        configure_logging()
        second_count = sum(
            1 for f in httpx_log.filters if isinstance(f, SecretRedactingFilter)
        )
        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
