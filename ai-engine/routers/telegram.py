"""Telegram Bot API webhook handler.

Mirror of `routers.aptana` for Telegram. One endpoint:

    POST /webhook/telegram/{path_secret}
        Receives Telegram Bot API "Update" objects. Authenticates by:
        (1) path_secret in the URL — matches APTANA's pattern, defence in depth
        (2) X-Telegram-Bot-Api-Secret-Token header — Telegram echoes the
            secret_token we registered with setWebhook on every callback

Both must match; the path_secret guard catches misconfigured proxies that
swallow custom headers (Caddy preserves them by default but explicit is
safer), and the header guard is Telegram's documented mechanism.

The handler hands off to a BackgroundTask so Telegram gets a fast 200 —
Telegram retries with backoff on non-2xx responses, and a slow reply path
could rack up duplicates.

Required env vars
    TELEGRAM_BOT_TOKEN          — sender token (see services/telegram_sender.py)
    TELEGRAM_WEBHOOK_SECRET     — long random string; same value in the URL
                                  path AND registered with setWebhook as
                                  secret_token. Generate: openssl rand -hex 32.

Telegram "Update" payload shape (only the fields we use):
    {
      "update_id": int,
      "message": {
        "message_id": int,
        "from": { "id": int, "first_name": str, "is_bot": bool, "language_code": str },
        "chat": { "id": int, "type": "private" | "group" | "supergroup" | "channel" },
        "date": int,            # unix seconds
        "text": str             # only present for text messages
      }
    }

Reference: https://core.telegram.org/bots/api#update
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from services.ai_handler import generate_ai_response, log_to_backend
from services.telegram_sender import acknowledge_typing, send_text

logger = logging.getLogger(__name__)
router = APIRouter()

_PATH_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
if not _PATH_SECRET:
    logger.warning(
        "TELEGRAM_WEBHOOK_SECRET is unset — Telegram webhook will reject all "
        "requests. Generate one with `openssl rand -hex 32`, set it in "
        "ai-engine/.env, and register it as secret_token via setWebhook."
    )

# Static welcome for /start — no AI generation, no per-user RAG. Mirrors the
# APTANA-WhatsApp _GREETING_BODY tone (Indonesian, Midnight Government).
_START_BODY = (
    "Halo! 👋 Saya BIMA, asisten AI perizinan UMKM dari DPMPTSP Jawa Tengah.\n\n"
    "Tanyakan saja apapun tentang KBLI, syarat NIB, atau alur OSS RBA — saya "
    "siap bantu 24 jam, dalam Bahasa Indonesia.\n\n"
    "Coba mulai dengan:\n"
    "• \"Saya mau buka warung makan, KBLI berapa?\"\n"
    "• \"Apa syarat NIB untuk usaha mikro?\"\n"
    "• \"Berapa lama proses izin OSS RBA?\""
)

# Apology shown when the AI generation fails AFTER we've already triggered
# the typing indicator. Without it the user is stranded with "typing…" then
# nothing arrives — worse UX than a polite error.
_GENERATION_FAILURE_APOLOGY = (
    "Maaf, sistem BIMA sedang sibuk sekali. Silakan coba lagi sebentar lagi 🙏"
)


def _check_path_secret(provided: str) -> None:
    """Constant-time check on the URL path segment. Raises 403 on mismatch."""
    if not _PATH_SECRET or not hmac.compare_digest(provided, _PATH_SECRET):
        raise HTTPException(status_code=403, detail="forbidden")


def _check_header_secret(provided: str | None) -> None:
    """Constant-time check on Telegram's X-Telegram-Bot-Api-Secret-Token
    header. Raises 403 if absent or mismatched.

    Telegram only sends this header when we registered a `secret_token`
    with setWebhook — we MUST register one (the deploy script does), so a
    missing header is suspicious."""
    if not _PATH_SECRET:
        raise HTTPException(status_code=403, detail="webhook_secret_unset")
    if not provided or not hmac.compare_digest(provided, _PATH_SECRET):
        raise HTTPException(status_code=403, detail="forbidden")


def _mask_chat(chat_id: int | str | None) -> str:
    if chat_id is None:
        return "<none>"
    s = str(chat_id)
    if len(s) <= 5:
        return "…" + s[-2:]
    return s[:2] + "…" + s[-3:]


@router.post("/webhook/telegram/{path_secret}")
async def telegram_webhook(
    path_secret: str,
    request: Request,
    background: BackgroundTasks,
    # Header alias must match exactly; FastAPI lowercases by default but
    # uses the alias if provided. Telegram's casing is documented as
    # `X-Telegram-Bot-Api-Secret-Token`.
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Receive a Telegram Update. Returns {ok:true} fast; the AI pipeline
    runs in a BackgroundTask."""
    _check_path_secret(path_secret)
    _check_header_secret(x_telegram_bot_api_secret_token)

    try:
        update = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_json")

    # Telegram delivers many update types; we only handle text messages
    # for now. callback_query, edited_message, etc. are dropped with a
    # 200 to prevent Telegram retrying.
    message = update.get("message") or {}
    text = message.get("text")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    sender = message.get("from") or {}
    first_name = sender.get("first_name")
    message_id = message.get("message_id")
    is_bot_sender = bool(sender.get("is_bot"))

    if not chat_id or not text or is_bot_sender:
        logger.info(
            "Telegram drop | update_id=%s reason=%s",
            update.get("update_id"),
            "non_text" if not text else "no_chat" if not chat_id else "bot_echo",
        )
        # 200 — payload is well-formed, just not actionable. Don't make
        # Telegram retry.
        return {"ok": True, "skipped": True}

    logger.info(
        "Telegram inbound | chat=%s chat_type=%s name=%s msg_id=%s text=%r",
        _mask_chat(chat_id), chat_type, first_name, message_id, text[:80],
    )

    background.add_task(
        _process_inbound,
        chat_id=chat_id,
        text=text,
        first_name=first_name,
        message_id=message_id,
    )
    return {"ok": True}


async def _process_inbound(
    chat_id: int,
    text: str,
    first_name: str | None,
    message_id: int | None,
) -> None:
    """End-to-end inbound pipeline: typing ack → AI reply → send → log."""
    # Channel-namespaced user_id avoids cross-channel collisions in
    # ai_handler's in-memory per-user state (the dict is keyed by stringified
    # user_id; we have WhatsApp users at "wa-<msisdn>", now Telegram at
    # "tg-<chat_id>").
    user_id = f"tg-{chat_id}"

    # /start is Telegram's bot-bootstrap command — send the static welcome
    # instead of routing through the AI. Cheaper, instant, no LLM call.
    if text.strip() == "/start":
        await send_text(chat_id=chat_id, body=_START_BODY)
        return

    # Fire-and-forget typing indicator. Telegram's native one is ~5s; for our
    # ~9-13s reply latency one ack is enough — the actual reply usually arrives
    # before users notice the indicator timing out. acknowledge_typing never
    # raises (cosmetic only).
    asyncio.create_task(acknowledge_typing(chat_id))

    try:
        reply = await generate_ai_response(user_id, text)
    except Exception:
        logger.exception("Telegram AI generation failed | chat=%s", _mask_chat(chat_id))
        try:
            await send_text(chat_id=chat_id, body=_GENERATION_FAILURE_APOLOGY)
        except Exception:
            logger.exception("Telegram apology send failed | chat=%s", _mask_chat(chat_id))
        return

    delivered = await send_text(chat_id=chat_id, body=reply)
    if not delivered:
        logger.error("Telegram delivery failed | chat=%s", _mask_chat(chat_id))

    try:
        await log_to_backend(user_id, text, reply, channel="telegram")
    except Exception:
        logger.exception("Telegram backend log failed | chat=%s", _mask_chat(chat_id))
