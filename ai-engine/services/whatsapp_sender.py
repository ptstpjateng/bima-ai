"""APTANA WhatsApp BSP outbound message sender.

Wraps `POST https://api-multichannel.aptana.co.id/api/v2/messages`.

This module is the APTANA-based replacement for the legacy `_send_whatsapp_reply`
in ai_handler.py (which talks directly to Meta Cloud API). It is NOT yet wired into
process_message — the new pipeline lives in routers/aptana.py and bypasses
process_message until the migration is locked in.

Required env vars (see .env.example):
    APTANA_API_TOKEN     — bearer token from APTANA dashboard → Settings → Account
    APTANA_SENDER_NUMBER — provisioned WABA phone, E.164 without +, e.g. "6285XXXXXXXXX"
    APTANA_API_BASE      — defaults to https://api-multichannel.aptana.co.id
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

_API_BASE = os.getenv("APTANA_API_BASE", "https://api-multichannel.aptana.co.id").rstrip("/")
_API_TOKEN = os.getenv("APTANA_API_TOKEN", "")
_SENDER_NUMBER = os.getenv("APTANA_SENDER_NUMBER", "")

_TIMEOUT_SECONDS = 30.0
_MAX_ATTEMPTS = 3
_RETRY_STATUS = {429, 500, 502, 503, 504}

if not _API_TOKEN:
    logger.warning(
        "APTANA_API_TOKEN is unset — outbound WhatsApp sends will fail. "
        "Set it in ai-engine/.env on the VPS."
    )
if not _SENDER_NUMBER:
    logger.warning(
        "APTANA_SENDER_NUMBER is unset — outbound WhatsApp sends will fail. "
        "Set to your provisioned WABA phone in E.164 without +, e.g. 6285XXXXXXXXX."
    )


def normalize_phone(phone: str) -> str:
    """Coerce a phone string into APTANA's expected format (E.164 without +).

    Accepts: +6285…, 6285…, 085…, 85… → 6285…
    Indonesian-only by design — all BIMA users are Indonesian UMKM.
    """
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    elif not digits.startswith("62"):
        digits = "62" + digits
    return digits


async def send_text(recipient_phone: str, body: str, *, preview_url: bool = False) -> bool:
    """Send a freeform WhatsApp text message via APTANA.

    Returns True on success, False on permanent failure (after retries exhausted).
    Never raises — callers can fire-and-forget.

    WhatsApp 24-hour session-message rule applies: this only succeeds if the user has
    contacted the bot in the last 24h. For outbound notifications outside that window,
    use a pre-approved template via APTANA's Send Template Message endpoint instead.
    """
    if not _API_TOKEN or not _SENDER_NUMBER:
        logger.error("Cannot send WhatsApp: APTANA_API_TOKEN or APTANA_SENDER_NUMBER unset.")
        return False

    # NOTE on path: APTANA's Postman cURL snippet (2026-05-13) shows /api/v1/messages.
    # An earlier endpoint card in the Postman docs displayed /api/v2/messages — likely a
    # different (newer?) endpoint or a Postman display quirk. v1 is what the working
    # cURL uses, so go with v1. If a 404 comes back, try /api/v2/messages.
    url = f"{_API_BASE}/api/v1/messages"
    headers = {
        # Confirmed from APTANA's Postman cURL snippet: header name is `Api-Token`,
        # NOT the standard `Authorization: Bearer ...`.
        "Api-Token": _API_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "channel": "wa",
        "sender": _SENDER_NUMBER,
        "recipient": normalize_phone(recipient_phone),
        "type": "text",
        "text": {
            "body": body[:4096],  # WhatsApp text-message limit
            "preview_url": preview_url,
        },
    }

    masked_to = payload["recipient"][:4] + "…" + payload["recipient"][-4:]

    for attempt in range(_MAX_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=payload, headers=headers)

            if resp.status_code < 300:
                logger.info(
                    "APTANA send ok | to=%s status=%d attempt=%d",
                    masked_to, resp.status_code, attempt + 1,
                )
                return True

            if resp.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s
                logger.warning(
                    "APTANA send transient | to=%s status=%d attempt=%d retry_in=%ds",
                    masked_to, resp.status_code, attempt + 1, wait,
                )
                await asyncio.sleep(wait)
                continue

            logger.error(
                "APTANA send failed | to=%s status=%d body=%s",
                masked_to, resp.status_code, resp.text[:500],
            )
            return False

        except (httpx.RequestError, asyncio.TimeoutError) as exc:
            if attempt < _MAX_ATTEMPTS - 1:
                wait = 2 ** attempt
                logger.warning(
                    "APTANA send error | to=%s err=%s attempt=%d retry_in=%ds",
                    masked_to, exc.__class__.__name__, attempt + 1, wait,
                )
                await asyncio.sleep(wait)
            else:
                logger.error("APTANA send exhausted retries | to=%s err=%s", masked_to, exc)

    return False


# ---------------------------------------------------------------------------
# Typing acknowledgment (Option C — try native, fall back to text).
# ---------------------------------------------------------------------------
#
# WhatsApp Cloud API supports a "read + typing" status update that turns the
# user's checkmarks blue and shows "BIMA is typing…" for ~25s. Whether APTANA
# passes that field through to Meta is undocumented; we try it once, and on
# any non-2xx response fall back to a short interim text message.
#
# Either path is fire-and-forget — the caller doesn't await the result and
# never sees an exception. Goal is a faster perceived response, not a
# guaranteed delivery.

# Meta's typing indicator times out at 25s; our RAG+Gemma path is 5–10s, so
# we have plenty of headroom. Short timeout on the request itself so a slow
# APTANA endpoint can't delay the real reply.
_ACK_TIMEOUT_SECONDS = 4.0

# Indonesian-language interim text — used only when the native typing call
# is rejected by APTANA. Kept short so it doesn't dominate the chat.
_ACK_FALLBACK_BODY = "💭 Sebentar ya, BIMA sedang mencarikan info untukmu…"


async def acknowledge_received(message_id: str | None, recipient_phone: str) -> None:
    """Best-effort acknowledgment so the user sees activity within ~1s.

    Tries the Meta-style typing indicator payload via APTANA first — if APTANA
    passes it through, the user gets a native "BIMA is typing…" with no extra
    bubble. On any rejection (or if we don't have a message_id to ack), falls
    back to sending a short interim text. Never raises.

    Call as fire-and-forget:
        asyncio.create_task(acknowledge_received(msg_id, msisdn))
    """
    if not _API_TOKEN or not _SENDER_NUMBER:
        return  # send_text would log the same warning; stay silent here.

    masked = (
        recipient_phone[:4] + "…" + recipient_phone[-4:]
        if len(recipient_phone) >= 8
        else "<short>"
    )

    if message_id:
        if await _try_native_typing(message_id, recipient_phone, masked):
            return  # native indicator accepted; we're done.

    # Either no message_id, or APTANA rejected the native call. Send a short
    # interim text so the user knows BIMA heard them.
    try:
        await send_text(recipient_phone=recipient_phone, body=_ACK_FALLBACK_BODY)
    except Exception:
        # send_text already swallows network errors; this guard catches anything
        # else so a broken acknowledge can't kill the inbound handler.
        logger.exception("Acknowledgment fallback raised | to=%s", masked)


async def _try_native_typing(
    message_id: str, recipient_phone: str, masked: str
) -> bool:
    """POST a Meta-style read+typing status to APTANA. Return True on 2xx.

    APTANA's API for status updates isn't documented in their Postman
    collection. We try the payload shape Meta uses, wrapped in APTANA's
    standard envelope. First successful response observed in production
    becomes the reference; first 4xx body teaches us the right shape.
    """
    url = f"{_API_BASE}/api/v1/messages"
    headers = {
        "Api-Token": _API_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "channel": "wa",
        "sender": _SENDER_NUMBER,
        "recipient": normalize_phone(recipient_phone),
        # Speculative APTANA passthrough of Meta's status-update fields.
        # If APTANA rejects, the rejection body tells us which field is wrong.
        "type": "status",
        "status": "read",
        "message_id": message_id,
        "typing_indicator": {"type": "text"},
    }

    try:
        async with httpx.AsyncClient(timeout=_ACK_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except (httpx.RequestError, asyncio.TimeoutError) as exc:
        logger.info(
            "ack native typing network error → falling back | to=%s err=%s",
            masked, exc.__class__.__name__,
        )
        return False

    if resp.status_code < 300:
        logger.info(
            "ack native typing accepted | to=%s status=%d", masked, resp.status_code
        )
        return True

    # Non-2xx — log enough body to teach us the right shape, then fall back.
    logger.info(
        "ack native typing rejected → falling back to text | to=%s status=%d body=%s",
        masked, resp.status_code, resp.text[:300],
    )
    return False
