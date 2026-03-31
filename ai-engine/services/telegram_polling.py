"""
BIMA-AI – Telegram Long-Polling Service

Runs as an asyncio background task inside FastAPI's lifespan.
Uses Telegram's getUpdates API with long-polling (timeout=30s) so no
HTTPS / public webhook URL is required.

Message flow for every inbound text:
  1. Call backend POST /api/auth/telegram/identify → get user_id + magic link
  2. Call AI handler generate_ai_response → get reply text
  3. Send reply (+ portal button / magic link) back via Telegram sendMessage
  4. Log the interaction to backend via ai_handler.log_to_backend
"""

import asyncio
import logging
import os
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("bima_ai.telegram_polling")

_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
_BACKEND_URL: str = os.getenv("LARAVEL_BACKEND_URL", "http://backend:80")
_INTERNAL_KEY: str = os.getenv("LARAVEL_API_KEY", "")

_TELEGRAM_API = f"https://api.telegram.org/bot{_BOT_TOKEN}"
_IDENTIFY_URL = f"{_BACKEND_URL}/api/auth/telegram/identify"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _tg_post(client: httpx.AsyncClient, method: str, payload: dict) -> dict:
    """POST to a Telegram Bot API method; return the JSON response dict."""
    resp = await client.post(f"{_TELEGRAM_API}/{method}", json=payload, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


async def _send_message(
    client: httpx.AsyncClient,
    chat_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        await _tg_post(client, "sendMessage", payload)
    except httpx.HTTPStatusError as exc:
        # Telegram rejects Markdown with some special chars — retry as plain text.
        if exc.response.status_code == 400:
            payload.pop("parse_mode", None)
            payload.pop("reply_markup", None)
            try:
                await _tg_post(client, "sendMessage", payload)
            except Exception:
                logger.exception("Failed to send plain-text fallback | chat_id=%s", chat_id)
        else:
            logger.warning(
                "Telegram sendMessage error | chat_id=%s | status=%s | body=%s",
                chat_id, exc.response.status_code, exc.response.text[:200],
            )
    except Exception:
        logger.exception("Unexpected error sending Telegram message | chat_id=%s", chat_id)


async def _identify_user(
    client: httpx.AsyncClient,
    chat_id: int,
    first_name: str | None,
    last_name: str | None,
    username: str | None,
) -> tuple[int | None, str | None, bool]:
    """
    Call backend to find/create user.
    Returns (user_id, magic_link, is_new_user) or (None, None, False) on error.
    """
    if not _INTERNAL_KEY:
        logger.warning("LARAVEL_API_KEY not set — cannot identify Telegram user.")
        return None, None, False

    try:
        resp = await client.post(
            _IDENTIFY_URL,
            json={
                "telegram_chat_id": chat_id,
                "first_name": first_name,
                "last_name": last_name,
                "username": username,
            },
            headers={"X-Internal-Key": _INTERNAL_KEY, "Accept": "application/json"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return data.get("user_id"), data.get("magic_link"), data.get("is_new_user", False)
    except Exception:
        logger.exception("Failed to identify Telegram user | chat_id=%s", chat_id)
        return None, None, False


# ---------------------------------------------------------------------------
# Update handler
# ---------------------------------------------------------------------------

async def _handle_update(client: httpx.AsyncClient, update: dict) -> None:
    """Process a single Telegram update dict."""
    # Import here to avoid circular import at module load.
    from services.ai_handler import generate_ai_response, log_to_backend

    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    text: str | None = message.get("text")
    if not text:
        return  # Ignore non-text updates (photos, stickers, etc.)

    chat = message["chat"]
    chat_id: int = chat["id"]
    from_user: dict = message.get("from") or {}
    first_name: str | None = from_user.get("first_name")
    last_name: str | None = from_user.get("last_name")
    username: str | None = from_user.get("username")

    logger.info("Telegram update | chat_id=%s | text_len=%s", chat_id, len(text))

    # --- 1. Identify / provision user in backend ---
    user_id, magic_link, is_new = await _identify_user(
        client, chat_id, first_name, last_name, username
    )

    # --- 2. Handle /start command ---
    if text.strip().lower().startswith("/start"):
        name = first_name or "Anda"
        if magic_link:
            welcome = (
                f"👋 Halo, *{name}*! Selamat datang di *BIMA-AI*.\n\n"
                f"Saya asisten perizinan OSS RBA untuk UMKM di Jawa Tengah.\n\n"
                f"Klik tombol di bawah untuk masuk ke *Portal BIMA-AI* dan mulai "
                f"mengurus perizinan usaha Anda secara online:"
            )
            markup = {
                "inline_keyboard": [[
                    {"text": "🚀 Buka Portal BIMA-AI", "url": magic_link}
                ]]
            }
        else:
            welcome = (
                f"👋 Halo, *{name}*! Selamat datang di *BIMA-AI*.\n\n"
                f"Saya asisten perizinan OSS RBA untuk UMKM di Jawa Tengah.\n\n"
                f"Ceritakan jenis usaha Anda, dan saya akan bantu menentukan "
                f"perizinan yang dibutuhkan. Ketik pertanyaan Anda sekarang!"
            )
            markup = None

        await _send_message(client, chat_id, welcome, markup)
        return

    # --- 3. Generate AI response ---
    user_ref = str(user_id) if user_id else str(chat_id)
    ai_reply = await generate_ai_response(user_ref, text)

    # --- 4. Append portal button if we have a magic link ---
    if magic_link:
        markup = {
            "inline_keyboard": [[
                {"text": "📋 Lihat Status Perizinan", "url": magic_link}
            ]]
        }
    else:
        markup = None

    await _send_message(client, chat_id, ai_reply, markup)

    # --- 5. Log to backend (best-effort) ---
    if user_id:
        await log_to_backend(str(user_id), text, ai_reply)


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

async def run_polling() -> None:
    """
    Long-polling loop. Meant to run as an asyncio background task.
    Calls getUpdates with timeout=30; on error waits 5 s before retrying.
    Stops cleanly when the task is cancelled (e.g. app shutdown).
    """
    if not _BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set — polling disabled.")
        return

    logger.info("Telegram long-polling started.")
    offset: int = 0

    async with httpx.AsyncClient() as client:
        # Clear any pending updates left from a previous run.
        try:
            resp = await _tg_post(client, "getUpdates", {"offset": -1, "limit": 1})
            updates = resp.get("result", [])
            if updates:
                offset = updates[-1]["update_id"] + 1
        except Exception:
            pass  # Non-fatal; just start from 0.

        while True:
            try:
                resp = await _tg_post(
                    client,
                    "getUpdates",
                    {"offset": offset, "timeout": 30, "allowed_updates": ["message", "edited_message"]},
                )
                updates: list[dict] = resp.get("result", [])

                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        await _handle_update(client, update)
                    except Exception:
                        logger.exception(
                            "Error handling update | update_id=%s", update.get("update_id")
                        )

            except asyncio.CancelledError:
                logger.info("Telegram polling cancelled — shutting down.")
                break
            except httpx.TimeoutException:
                # Long-poll timeout is normal; just re-poll immediately.
                continue
            except Exception:
                logger.exception("Telegram polling error — retrying in 5 s.")
                await asyncio.sleep(5)

    logger.info("Telegram long-polling stopped.")
