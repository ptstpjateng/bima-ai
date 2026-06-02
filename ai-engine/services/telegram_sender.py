"""Telegram Bot API outbound sender.

Mirrors the surface of `services.whatsapp_sender` — `send_text` and
`acknowledge_typing` — so the inbound router in `routers/telegram.py` can
reuse the same processing pipeline shape as `routers/aptana.py`.

Why Telegram lives alongside APTANA-WhatsApp:
    Meta disabled the DPMPTSP WhatsApp Business Account on 2026-05-21
    (review pending). Telegram is BIMA's demo-day insurance: same AI,
    same RAG, same conversation — different transport.

Required env vars
    TELEGRAM_BOT_TOKEN     — from BotFather (/newbot). Format: "12345:AAA…"
                             Treat as a credential; never log it.

The Telegram Bot API:
    Base URL:  https://api.telegram.org/bot<TOKEN>/<method>
    Auth:      the token IS the URL prefix (no separate header)
    Encoding:  application/json bodies; JSON responses

Reference: https://core.telegram.org/bots/api
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 10s covers Telegram's normal RTT (typically <500ms) with plenty of slack
# without holding the request handler. Per-call retries are short to keep
# the inbound BackgroundTask quick — we don't want a slow send to delay
# the AI reply of a later inbound message.
_TIMEOUT_SECONDS = 10.0
_MAX_TEXT_LENGTH = 4096  # Telegram hard cap on a single sendMessage text

_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not _BOT_TOKEN:
    logger.warning(
        "TELEGRAM_BOT_TOKEN is unset — Telegram outbound is disabled. "
        "Generate one via BotFather (/newbot) and set it in ai-engine/.env."
    )


def _api_url(method: str) -> str:
    """Compose the Telegram Bot API URL for a method (e.g. 'sendMessage')."""
    return f"https://api.telegram.org/bot{_BOT_TOKEN}/{method}"


def _mask_chat(chat_id: int | str) -> str:
    """Don't print full chat IDs to logs — they map to real user identities.
    Shows the first 2 and last 3 digits."""
    s = str(chat_id)
    if len(s) <= 5:
        return "…" + s[-2:]
    return s[:2] + "…" + s[-3:]


async def send_text(
    chat_id: int | str,
    body: str,
    *,
    parse_mode: str | None = None,
    disable_web_page_preview: bool = True,
) -> bool:
    """Send a text message to a Telegram chat.

    Returns True if Telegram accepted the message (HTTP 200 + ok=True),
    False otherwise. Never raises — failures are logged.

    `parse_mode` defaults to plain text (None) for safety. BIMA's AI
    occasionally emits Markdown-ish text; passing parse_mode="Markdown" or
    "MarkdownV2" would require careful escaping, which we skip.

    `disable_web_page_preview=True` because BIMA links to nolongin.com
    tracking URLs and the link preview card clutters the chat with stale
    OG metadata.
    """
    if not _BOT_TOKEN:
        logger.error("Telegram send refused | TELEGRAM_BOT_TOKEN unset | chat=%s", _mask_chat(chat_id))
        return False

    # Telegram rejects messages longer than 4096 chars with 400 Bad Request.
    # Truncate with an ellipsis rather than dropping — the user still gets
    # the start of the reply.
    if len(body) > _MAX_TEXT_LENGTH:
        logger.warning(
            "Telegram message truncated | chat=%s original_len=%d",
            _mask_chat(chat_id), len(body),
        )
        body = body[: _MAX_TEXT_LENGTH - 1] + "…"

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": body,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            r = await client.post(_api_url("sendMessage"), json=payload)
        if r.status_code == 200:
            data = r.json()
            if data.get("ok"):
                logger.info(
                    "Telegram send ok | chat=%s body_len=%d",
                    _mask_chat(chat_id), len(body),
                )
                return True
            # ok=False with a description — auth/permission/format issue, not transient
            logger.error(
                "Telegram send rejected | chat=%s description=%s",
                _mask_chat(chat_id), data.get("description"),
            )
            return False
        logger.error(
            "Telegram send failed | chat=%s status=%d body=%s",
            _mask_chat(chat_id), r.status_code, r.text[:200],
        )
        return False
    except (httpx.RequestError, asyncio.TimeoutError) as exc:
        logger.error(
            "Telegram send transport error | chat=%s err=%s",
            _mask_chat(chat_id), exc,
        )
        return False


async def acknowledge_typing(chat_id: int | str) -> None:
    """Show the native Telegram "typing…" indicator for ~5 seconds.

    Fire-and-forget. Telegram resets the indicator after ~5s unless we
    re-send; for BIMA's typical 8-12s reply latency one ack is enough —
    users see typing, then the bubble arrives. No interim text needed
    (unlike APTANA-WhatsApp which doesn't expose the native indicator).
    """
    if not _BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            await client.post(
                _api_url("sendChatAction"),
                json={"chat_id": chat_id, "action": "typing"},
            )
    except (httpx.RequestError, asyncio.TimeoutError):
        # Typing indicator is purely cosmetic — never escalate failures.
        # The user just won't see "typing…" but the real reply will still
        # land via send_text.
        pass


async def set_webhook(
    url: str,
    secret_token: str,
    *,
    drop_pending_updates: bool = True,
) -> dict[str, Any]:
    """Register `url` as this bot's webhook with Telegram. Returns the
    parsed Telegram response.

    Idempotent — calling it again replaces the previous webhook. Use this
    once during deploy (see `docs/telegram-setup.md`) or from a one-shot
    admin endpoint, not from the request hot path.

    `secret_token` is echoed by Telegram in every webhook request as the
    `X-Telegram-Bot-Api-Secret-Token` header — `routers/telegram.py`
    verifies it constant-time. 1-256 chars, A-Z a-z 0-9 _ - only.

    `drop_pending_updates=True` on first setup; you don't want a backlog
    of pre-deploy messages firing the moment the bot comes online.
    """
    payload = {
        "url": url,
        "secret_token": secret_token,
        "drop_pending_updates": drop_pending_updates,
        # Tell Telegram which update types to deliver. We only need
        # 'message' for now; receiving callback_query / edited_message
        # would just be ignored by the inbound handler.
        "allowed_updates": ["message"],
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        r = await client.post(_api_url("setWebhook"), json=payload)
    return r.json()


async def get_me() -> dict[str, Any]:
    """Smoke-test: return the bot's identity per Telegram. Use to verify
    the BOT_TOKEN is valid without sending a message."""
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        r = await client.get(_api_url("getMe"))
    return r.json()
