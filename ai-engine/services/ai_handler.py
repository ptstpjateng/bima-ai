"""
BIMA-AI – AI Handler Service

Pipeline for every inbound message:
  1. generate_ai_response  – placeholder LLM call (swap for LangChain RAG in Step 3)
  2. send_platform_reply   – dispatches reply back to WhatsApp or Telegram
  3. log_to_backend        – async POST to Laravel /api/ai-logs (fire-and-forget)
  4. process_message       – single entry point wiring the steps above

Error contract:
  - generate_ai_response always returns a string (fallback on failure).
  - send_platform_reply and log_to_backend never raise; failures are logged only.
  - A downed Laravel backend or unreachable messaging API must never block the
    pipeline or surface as an unhandled exception.
"""

import logging
import os
from typing import Any, Literal

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("bima_ai.ai_handler")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_LARAVEL_BACKEND_URL: str = os.getenv("LARAVEL_BACKEND_URL", "http://localhost:8000")
_LARAVEL_API_KEY: str = os.getenv("LARAVEL_API_KEY", "")
_LOG_ENDPOINT: str = f"{_LARAVEL_BACKEND_URL}/api/ai-logs"
_LOG_TIMEOUT_SECONDS: float = 5.0

_WHATSAPP_API_TOKEN: str = os.getenv("WHATSAPP_API_TOKEN", "")
_WHATSAPP_API_VERSION: str = os.getenv("WHATSAPP_API_VERSION", "v19.0")

_TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

Platform = Literal["whatsapp", "telegram"]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT: str = """
You are BIMA-AI, an intelligent business assistant helping Indonesian MSMEs
(Usaha Mikro, Kecil, dan Menengah) navigate the OSS RBA (Online Single
Submission – Risk-Based Approach) licensing system administered by DPMPTSP.

Core rules:
- Be helpful, concise, and empathetic. Translate bureaucratic language into
  plain, practical guidance a small business owner can act on immediately.
- Specialise in Central Java (Jawa Tengah) licensing nuances: DPMPTSP
  requirements, local KBLI (Klasifikasi Baku Lapangan Usaha Indonesia) codes,
  regional regulation specifics, and Pemerintah Provinsi Jawa Tengah policies.
- When a user describes a business activity, identify the correct KBLI code(s)
  and explain which OSS RBA licences, NIB (Nomor Induk Berusaha), or
  certificates are required.
- Ask one focused clarifying question if the request is ambiguous rather than
  guessing.
- Never fabricate regulation article numbers, ministerial decree references, or
  legal citations. Acknowledge uncertainty and offer to verify.
- Match the user's language: respond in Bahasa Indonesia if they write in
  Indonesian, in English if they write in English.
- Keep replies short enough to read comfortably on a mobile screen (≤ 5 short
  paragraphs or a brief bulleted list).
""".strip()


# ---------------------------------------------------------------------------
# 1. LLM placeholder
# ---------------------------------------------------------------------------

async def generate_ai_response(user_id: str, message: str) -> str:
    """
    Generate an AI reply for the given user message.

    This is a structured placeholder. Replace the marked block below with your
    LangChain / LlamaIndex RAG chain in Step 3. The function signature, error
    contract, and return type must remain unchanged.

    Returns:
        The AI reply string. Never raises; returns a safe fallback on any error.
    """
    try:
        # ------------------------------------------------------------------ #
        # REPLACE THIS BLOCK IN STEP 3 with your LangChain RAG chain:        #
        #                                                                      #
        #   from services.rag_chain import build_chain                        #
        #   chain = build_chain()                                              #
        #   result = await chain.ainvoke({                                    #
        #       "system_prompt": _SYSTEM_PROMPT,                              #
        #       "question": message,                                           #
        #       "user_id": user_id,                                            #
        #   })                                                                 #
        #   return result["answer"]                                            #
        # ------------------------------------------------------------------ #

        logger.info("LLM placeholder invoked | user_id=%s", user_id)

        return (
            f"[BIMA-AI] Halo! Pesan Anda telah diterima:\n\n"
            f"\"{message}\"\n\n"
            f"Integrasi RAG/LLM sedang disiapkan. Sebentar lagi saya akan "
            f"membantu Anda menavigasi perizinan OSS RBA secara penuh. "
            f"Ada pertanyaan lain yang bisa saya bantu?"
        )

    except Exception:
        logger.exception("LLM generation failed | user_id=%s", user_id)
        return (
            "Maaf, terjadi gangguan teknis sementara. "
            "Silakan coba lagi dalam beberapa saat. Terima kasih atas kesabaran Anda."
        )


# ---------------------------------------------------------------------------
# 2. Platform reply senders
# ---------------------------------------------------------------------------

async def _send_whatsapp_reply(
    phone_number_id: str,
    recipient_wa_id: str,
    message: str,
) -> None:
    """
    Send a text reply via the Meta Cloud API (WhatsApp Business Platform).

    Docs: https://developers.facebook.com/docs/whatsapp/cloud-api/messages/text
    Requires WHATSAPP_API_TOKEN in the environment.
    """
    if not _WHATSAPP_API_TOKEN:
        logger.warning(
            "WHATSAPP_API_TOKEN not set – cannot send reply | recipient=%s",
            recipient_wa_id,
        )
        return

    url = (
        f"https://graph.facebook.com/{_WHATSAPP_API_VERSION}"
        f"/{phone_number_id}/messages"
    )
    headers = {
        "Authorization": f"Bearer {_WHATSAPP_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_wa_id,
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            logger.info(
                "WhatsApp reply sent | recipient=%s | status=%s",
                recipient_wa_id,
                response.status_code,
            )
    except httpx.TimeoutException:
        logger.warning(
            "WhatsApp API timed out | recipient=%s", recipient_wa_id
        )
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "WhatsApp API error | recipient=%s | status=%s | body=%s",
            recipient_wa_id,
            exc.response.status_code,
            exc.response.text[:300],
        )
    except httpx.RequestError as exc:
        logger.warning(
            "WhatsApp API unreachable | recipient=%s | error=%s",
            recipient_wa_id,
            str(exc),
        )
    except Exception:
        logger.exception(
            "Unexpected error sending WhatsApp reply | recipient=%s", recipient_wa_id
        )


async def _send_telegram_reply(chat_id: int, message: str) -> None:
    """
    Send a text reply via the Telegram Bot API (sendMessage).

    Docs: https://core.telegram.org/bots/api#sendmessage
    Requires TELEGRAM_BOT_TOKEN in the environment.
    """
    if not _TELEGRAM_BOT_TOKEN:
        logger.warning(
            "TELEGRAM_BOT_TOKEN not set – cannot send reply | chat_id=%s", chat_id
        )
        return

    url = f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            logger.info(
                "Telegram reply sent | chat_id=%s | status=%s",
                chat_id,
                response.status_code,
            )
    except httpx.TimeoutException:
        logger.warning("Telegram API timed out | chat_id=%s", chat_id)
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Telegram API error | chat_id=%s | status=%s | body=%s",
            chat_id,
            exc.response.status_code,
            exc.response.text[:300],
        )
    except httpx.RequestError as exc:
        logger.warning(
            "Telegram API unreachable | chat_id=%s | error=%s", chat_id, str(exc)
        )
    except Exception:
        logger.exception(
            "Unexpected error sending Telegram reply | chat_id=%s", chat_id
        )


# ---------------------------------------------------------------------------
# 3. Backend logger
# ---------------------------------------------------------------------------

async def log_to_backend(
    user_id: str,
    prompt: str,
    ai_response: str,
) -> None:
    """
    Persist the conversation turn to the Laravel backend (POST /api/ai-logs).

    This is fire-and-forget: any failure is caught, logged, and swallowed so
    that a downed TALL backend never interrupts the reply path to the user.
    """
    if not _LARAVEL_API_KEY:
        logger.warning(
            "LARAVEL_API_KEY not set – AI log will be sent without auth | user_id=%s",
            user_id,
        )

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if _LARAVEL_API_KEY:
        headers["Authorization"] = f"Bearer {_LARAVEL_API_KEY}"

    payload: dict[str, str] = {
        "user_id": user_id,
        "prompt": prompt,
        "ai_response": ai_response,
    }

    try:
        async with httpx.AsyncClient(timeout=_LOG_TIMEOUT_SECONDS) as client:
            response = await client.post(_LOG_ENDPOINT, json=payload, headers=headers)
            response.raise_for_status()
            logger.info(
                "AI log persisted | user_id=%s | status=%s",
                user_id,
                response.status_code,
            )
    except httpx.TimeoutException:
        logger.warning(
            "Laravel backend timed out after %.1fs | user_id=%s",
            _LOG_TIMEOUT_SECONDS,
            user_id,
        )
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Laravel backend returned error | user_id=%s | status=%s | body=%s",
            user_id,
            exc.response.status_code,
            exc.response.text[:300],
        )
    except httpx.RequestError as exc:
        logger.warning(
            "Laravel backend unreachable | user_id=%s | error=%s",
            user_id,
            str(exc),
        )
    except Exception:
        logger.exception(
            "Unexpected error logging to backend | user_id=%s", user_id
        )


# ---------------------------------------------------------------------------
# 4. Orchestrator – single entry point called by webhook background tasks
# ---------------------------------------------------------------------------

async def process_message(
    user_id: str,
    message: str,
    platform: Platform,
    reply_context: dict[str, Any],
) -> None:
    """
    Full pipeline: generate reply → send to platform → log to backend.

    Designed to run as a FastAPI BackgroundTask so the webhook can return 200
    to Meta/Telegram immediately without waiting for LLM inference.

    Args:
        user_id:       Unique identifier for the sender (WA phone number or
                       Telegram chat ID as string).
        message:       The plain-text content of the inbound message.
        platform:      "whatsapp" or "telegram".
        reply_context: Platform-specific data required to send the reply.
                       WhatsApp: {"phone_number_id": str, "recipient_wa_id": str}
                       Telegram: {"chat_id": int}
    """
    logger.info(
        "Message pipeline start | platform=%s | user_id=%s", platform, user_id
    )

    # Step 1 – Generate AI response (always succeeds; fallback string on error).
    ai_response = await generate_ai_response(user_id, message)

    # Step 2 – Send reply back to the user on the originating platform.
    try:
        if platform == "whatsapp":
            await _send_whatsapp_reply(
                phone_number_id=reply_context["phone_number_id"],
                recipient_wa_id=reply_context["recipient_wa_id"],
                message=ai_response,
            )
        elif platform == "telegram":
            await _send_telegram_reply(
                chat_id=reply_context["chat_id"],
                message=ai_response,
            )
    except Exception:
        # Defensive catch: individual senders already handle their own errors,
        # but guard against any unforeseen failure in the dispatch logic.
        logger.exception(
            "Reply dispatch failed | platform=%s | user_id=%s", platform, user_id
        )

    # Step 3 – Persist to Laravel (best-effort; never blocks).
    await log_to_backend(user_id, message, ai_response)

    logger.info(
        "Message pipeline complete | platform=%s | user_id=%s", platform, user_id
    )
