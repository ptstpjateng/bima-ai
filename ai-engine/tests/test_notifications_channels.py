"""
Tests for multi-channel notification dispatch — `services/notifications.py`
and the channel-routing seam in `services/transparency_poller.py`.

Run standalone (no pytest needed — matches tests/test_siap_write.py):

    python -m tests.test_notifications_channels    # from ai-engine/
    python tests/test_notifications_channels.py    # also works

What it covers:
  * Channel inference — when only a phone is given → APTANA_WA, when only
    a chat_id → TELEGRAM, when both with no explicit channel → error.
  * Master feature flag (BIMA_NOTIFICATIONS_ENABLED) gates BOTH channels.
  * Telegram path actually calls services.telegram_sender.send_text and
    uses the telegram_body when present (otherwise freeform_body).
  * APTANA path is unchanged — template send by default.
  * Rate-limit keys are channel-prefixed so the same identifier on two
    transports doesn't share a quota.
  * URL allowlist still fires on Telegram (a non-BIMA URL is a phishing
    relay regardless of transport).
  * transparency_poller._resolve_channel_for_citizen routes only the
    env-pinned NIK to Telegram and falls back to APTANA otherwise.
  * Idempotency dedupe is channel-agnostic (the same logical event
    cannot fire twice via different transports within the window).

No real network, no APTANA, no Telegram bot.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Make `services` importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Stub external deps so importing notifications + transparency_poller works
# without httpx/asyncpg installed locally. We only test the channel-routing
# logic — every call that would hit the wire is mocked in the tests.
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

# Stub telegram_sender BEFORE importing notifications so the lazy import
# inside notifications._send_telegram resolves to our mock-friendly module.
# Tests that need to assert sends override `send_text` per-test.
telegram_stub = types.ModuleType("services.telegram_sender")
telegram_stub.send_text = AsyncMock(return_value=True)  # type: ignore[attr-defined]
sys.modules["services.telegram_sender"] = telegram_stub


from services import notifications  # noqa: E402
from services.notifications import Channel  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _reset_dispatcher_state() -> None:
    """Clear the in-memory rate-limit + idempotency tables between tests.

    The dispatcher's protections (rate limit, dedupe) are correctly
    persistent within the process — that's their whole point in
    production. For unit tests we deliberately reach into the module to
    reset state between assertions so each test is independent.
    """
    notifications._send_ts_by_recipient.clear()
    notifications._send_ts_global.clear()
    notifications._recent_sends.clear()


def _enable_notifications(enabled: bool = True) -> dict[str, str]:
    """Return an env patch that arms (or disarms) the master flag."""
    return {"BIMA_NOTIFICATIONS_ENABLED": "true" if enabled else "false"}


def _fresh_poller_import() -> None:
    """Force a fresh import of services.transparency_poller so its module-
    level env captures (_NOTIFICATION_TEST_CITIZEN_NIK, ..._CHAT_ID) reflect
    the CURRENT os.environ. Deleting from sys.modules alone is not enough:
    the parent `services` package keeps the submodule as an attribute, and
    a subsequent `from services import transparency_poller` resolves to the
    cached attribute instead of re-running the module body. Strip both."""
    sys.modules.pop("services.transparency_poller", None)
    import services  # the parent package
    if hasattr(services, "transparency_poller"):
        delattr(services, "transparency_poller")


# ===========================================================================
# Channel inference
# ===========================================================================


class TestChannelInference(unittest.TestCase):

    def setUp(self):
        _reset_dispatcher_state()

    def test_phone_only_infers_aptana(self):
        with patch.dict(os.environ, _enable_notifications()):
            with patch("services.notifications.send_template",
                       new=AsyncMock(return_value=True)) as wa_template, \
                 patch.object(telegram_stub, "send_text",
                              new=AsyncMock(return_value=True)) as tg_send:
                ok = _run(notifications.notify(
                    event="citizen_completed",
                    recipient_phone="6285111122223",
                    params={
                        "name": "Budi",
                        "license_type": "izin pemakaian tanah",
                        "ticket": "000099001",
                    },
                ))
        self.assertTrue(ok)
        self.assertEqual(wa_template.await_count, 1)
        self.assertEqual(tg_send.await_count, 0)

    def test_chat_id_only_infers_telegram(self):
        with patch.dict(os.environ, _enable_notifications()):
            with patch("services.notifications.send_template",
                       new=AsyncMock(return_value=True)) as wa_template, \
                 patch.object(telegram_stub, "send_text",
                              new=AsyncMock(return_value=True)) as tg_send:
                ok = _run(notifications.notify(
                    event="citizen_completed",
                    recipient_telegram_chat_id=987654321,
                    params={
                        "name": "Budi",
                        "license_type": "izin pemakaian tanah",
                        "ticket": "000099001",
                    },
                ))
        self.assertTrue(ok)
        self.assertEqual(tg_send.await_count, 1)
        self.assertEqual(wa_template.await_count, 0)
        # Telegram body is what gets sent — and it uses the telegram_body
        # template when defined (which it is for citizen_completed).
        sent_kwargs = tg_send.await_args.kwargs
        self.assertEqual(sent_kwargs["chat_id"], 987654321)
        self.assertIn("Selamat Budi", sent_kwargs["body"])
        self.assertIn("TERBIT", sent_kwargs["body"])

    def test_both_with_no_channel_is_error(self):
        """Ambiguous input — caller MUST pass channel explicitly."""
        with patch.dict(os.environ, _enable_notifications()):
            with patch("services.notifications.send_template",
                       new=AsyncMock(return_value=True)) as wa, \
                 patch.object(telegram_stub, "send_text",
                              new=AsyncMock(return_value=True)) as tg:
                ok = _run(notifications.notify(
                    event="citizen_completed",
                    recipient_phone="6285111122223",
                    recipient_telegram_chat_id=987654321,
                    params={
                        "name": "Budi",
                        "license_type": "izin pemakaian tanah",
                        "ticket": "000099001",
                    },
                ))
        self.assertFalse(ok)
        self.assertEqual(wa.await_count, 0)
        self.assertEqual(tg.await_count, 0)

    def test_no_recipient_is_error(self):
        with patch.dict(os.environ, _enable_notifications()):
            ok = _run(notifications.notify(
                event="citizen_completed",
                params={
                    "name": "Budi",
                    "license_type": "izin",
                    "ticket": "X",
                },
            ))
        self.assertFalse(ok)

    def test_explicit_channel_overrides_inference(self):
        """Passing channel=TELEGRAM with both identifiers should still
        route Telegram and ignore the phone."""
        with patch.dict(os.environ, _enable_notifications()):
            with patch("services.notifications.send_template",
                       new=AsyncMock(return_value=True)) as wa, \
                 patch.object(telegram_stub, "send_text",
                              new=AsyncMock(return_value=True)) as tg:
                ok = _run(notifications.notify(
                    event="citizen_completed",
                    channel=Channel.TELEGRAM,
                    recipient_phone="6285111122223",
                    recipient_telegram_chat_id=12345,
                    params={
                        "name": "Budi",
                        "license_type": "izin",
                        "ticket": "TKT",
                    },
                ))
        self.assertTrue(ok)
        self.assertEqual(tg.await_count, 1)
        self.assertEqual(wa.await_count, 0)


# ===========================================================================
# Feature flag is channel-agnostic
# ===========================================================================


class TestMasterFlag(unittest.TestCase):

    def setUp(self):
        _reset_dispatcher_state()

    def test_flag_off_blocks_aptana(self):
        with patch.dict(os.environ, _enable_notifications(False)):
            with patch("services.notifications.send_template",
                       new=AsyncMock(return_value=True)) as wa:
                ok = _run(notifications.notify(
                    event="citizen_completed",
                    recipient_phone="6285111122223",
                    params={"name": "X", "license_type": "Y", "ticket": "T"},
                ))
        self.assertFalse(ok)
        self.assertEqual(wa.await_count, 0)

    def test_flag_off_blocks_telegram(self):
        """The Meta-review window kill-switch MUST also silence Telegram."""
        with patch.dict(os.environ, _enable_notifications(False)):
            with patch.object(telegram_stub, "send_text",
                              new=AsyncMock(return_value=True)) as tg:
                ok = _run(notifications.notify(
                    event="citizen_completed",
                    recipient_telegram_chat_id=12345,
                    params={"name": "X", "license_type": "Y", "ticket": "T"},
                ))
        self.assertFalse(ok)
        self.assertEqual(tg.await_count, 0)


# ===========================================================================
# Body selection (telegram_body vs freeform_body)
# ===========================================================================


class TestTelegramBody(unittest.TestCase):

    def setUp(self):
        _reset_dispatcher_state()

    def test_uses_telegram_body_when_set(self):
        with patch.dict(os.environ, _enable_notifications()):
            with patch.object(telegram_stub, "send_text",
                              new=AsyncMock(return_value=True)) as tg:
                _run(notifications.notify(
                    event="citizen_progress",
                    recipient_telegram_chat_id=12345,
                    params={
                        "name": "Siti",
                        "license_type": "izin lingkungan",
                        "ticket": "000099002",
                        "next_stage": "Verifikasi Berkas",
                        "eta_days": "5",
                    },
                ))
        body = tg.await_args.kwargs["body"]
        # citizen_progress.telegram_body uses '\n' line breaks for Telegram
        # presentation; freeform body is a single paragraph. Easiest distinction.
        self.assertIn("\n", body)
        self.assertIn("Verifikasi Berkas", body)
        self.assertIn("000099002", body)


# ===========================================================================
# Rate-limit isolation per channel
# ===========================================================================


class TestRateLimitIsolation(unittest.TestCase):

    def setUp(self):
        _reset_dispatcher_state()

    def test_same_identifier_different_channels_have_independent_quota(self):
        """A leaked credential on one channel must not eat the quota of the
        OTHER channel for an unrelated identifier shape — the rate key
        carries a channel prefix to keep these independent."""
        with patch.dict(os.environ, _enable_notifications()):
            with patch("services.notifications.send_template",
                       new=AsyncMock(return_value=True)), \
                 patch.object(telegram_stub, "send_text",
                              new=AsyncMock(return_value=True)):
                # Saturate the WhatsApp side first.
                for i in range(notifications._MAX_PER_RECIPIENT_PER_WINDOW):
                    _run(notifications.notify(
                        event="citizen_progress",
                        recipient_phone="6285111122223",
                        params={
                            "name": "X", "license_type": "Y",
                            "ticket": f"TKT{i}",
                            "next_stage": "Z", "eta_days": "1",
                        },
                        dedupe_key=f"unique-wa-{i}",  # bypass dedupe
                    ))
                # WhatsApp now at cap → next WA send should fail.
                ok_wa = _run(notifications.notify(
                    event="citizen_progress",
                    recipient_phone="6285111122223",
                    params={
                        "name": "X", "license_type": "Y",
                        "ticket": "TKT-OVER", "next_stage": "Z",
                        "eta_days": "1",
                    },
                    dedupe_key="unique-wa-over",
                ))
                self.assertFalse(ok_wa, "WA send should be rate-limited")

                # But Telegram with a different identifier shape is still OK.
                ok_tg = _run(notifications.notify(
                    event="citizen_progress",
                    recipient_telegram_chat_id=12345,
                    params={
                        "name": "X", "license_type": "Y",
                        "ticket": "TKT-TG", "next_stage": "Z",
                        "eta_days": "1",
                    },
                    dedupe_key="unique-tg-1",
                ))
                self.assertTrue(ok_tg, "TG quota should be independent")


# ===========================================================================
# URL allowlist still applies on Telegram
# ===========================================================================


class TestUrlAllowlistOnTelegram(unittest.TestCase):

    def setUp(self):
        _reset_dispatcher_state()

    def test_non_bima_url_blocked_on_telegram(self):
        """A phishing URL inside a Telegram message is just as effective a
        relay as inside a WhatsApp message — the allowlist must apply."""
        with patch.dict(os.environ, _enable_notifications()):
            with patch.object(telegram_stub, "send_text",
                              new=AsyncMock(return_value=True)) as tg:
                ok = _run(notifications.notify(
                    event="citizen_needs_fix",
                    recipient_telegram_chat_id=12345,
                    params={
                        "name": "Budi",
                        "ticket": "TKT",
                        "issue_summary": "perlu koreksi",
                        # Not on the allowlist — must be rejected.
                        "fix_url": "https://evil.example.com/phish",
                    },
                ))
        self.assertFalse(ok)
        self.assertEqual(tg.await_count, 0)


# ===========================================================================
# Idempotency is channel-agnostic
# ===========================================================================


class TestIdempotencyCrossChannel(unittest.TestCase):

    def setUp(self):
        _reset_dispatcher_state()

    def test_same_dedupe_key_blocks_second_send_even_on_different_channel(self):
        """The same logical event sent again via a different transport is
        still a duplicate from the citizen's POV — the dedupe key carries
        no channel prefix on purpose."""
        with patch.dict(os.environ, _enable_notifications()):
            with patch("services.notifications.send_template",
                       new=AsyncMock(return_value=True)) as wa, \
                 patch.object(telegram_stub, "send_text",
                              new=AsyncMock(return_value=True)) as tg:
                first = _run(notifications.notify(
                    event="citizen_completed",
                    recipient_phone="6285111122223",
                    params={"name": "X", "license_type": "Y", "ticket": "T"},
                    dedupe_key="TKT:777",
                ))
                self.assertTrue(first)
                second = _run(notifications.notify(
                    event="citizen_completed",
                    recipient_telegram_chat_id=12345,
                    params={"name": "X", "license_type": "Y", "ticket": "T"},
                    dedupe_key="TKT:777",
                ))
        self.assertFalse(second)
        self.assertEqual(wa.await_count, 1)
        self.assertEqual(tg.await_count, 0)


# ===========================================================================
# transparency_poller._resolve_channel_for_citizen
# ===========================================================================


class TestPollerChannelResolution(unittest.TestCase):

    def test_no_env_pinned_mapping_routes_to_aptana(self):
        # Strip both env vars (a previous test may have set them) so the
        # module captures blank values at import.
        env_no_pin = {k: v for k, v in os.environ.items() if k not in (
            "NOTIFICATION_TEST_CITIZEN_NIK",
            "NOTIFICATION_TEST_TELEGRAM_CHAT_ID",
        )}
        with patch.dict(os.environ, env_no_pin, clear=True):
            _fresh_poller_import()
            from services import transparency_poller as poller

            ch, chat_id = poller._resolve_channel_for_citizen("3301010101010001")
            self.assertEqual(ch, Channel.APTANA_WA)
            self.assertIsNone(chat_id)

    def test_matching_nik_routes_to_telegram(self):
        with patch.dict(os.environ, {
            "NOTIFICATION_TEST_CITIZEN_NIK": "3301010101010001",
            "NOTIFICATION_TEST_TELEGRAM_CHAT_ID": "987654321",
        }, clear=False):
            _fresh_poller_import()
            from services import transparency_poller as poller

            ch, chat_id = poller._resolve_channel_for_citizen("3301010101010001")
            self.assertEqual(ch, Channel.TELEGRAM)
            self.assertEqual(chat_id, 987654321)

    def test_non_matching_nik_routes_to_aptana(self):
        with patch.dict(os.environ, {
            "NOTIFICATION_TEST_CITIZEN_NIK": "3301010101010001",
            "NOTIFICATION_TEST_TELEGRAM_CHAT_ID": "987654321",
        }, clear=False):
            _fresh_poller_import()
            from services import transparency_poller as poller

            ch, chat_id = poller._resolve_channel_for_citizen("9999999999999999")
            self.assertEqual(ch, Channel.APTANA_WA)
            self.assertIsNone(chat_id)

    def test_blank_nik_routes_to_aptana_safely(self):
        """A SIAP person with no NIK column → never accidentally Telegram."""
        with patch.dict(os.environ, {
            "NOTIFICATION_TEST_CITIZEN_NIK": "3301010101010001",
            "NOTIFICATION_TEST_TELEGRAM_CHAT_ID": "987654321",
        }, clear=False):
            _fresh_poller_import()
            from services import transparency_poller as poller

            ch, chat_id = poller._resolve_channel_for_citizen(None)
            self.assertEqual(ch, Channel.APTANA_WA)
            self.assertIsNone(chat_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
