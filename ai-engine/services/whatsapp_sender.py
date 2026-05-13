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
# Typing acknowledgment — text bubble (locked, May 2026).
# ---------------------------------------------------------------------------
#
# We probed APTANA's /api/v1/messages with Meta's native read+typing status
# fields (status, message_id, typing_indicator) and got HTTP 422 with explicit
# rejection of all three field names — APTANA's wrapper enforces a payload
# whitelist of message TYPES (text, template, document, image, …) and does
# NOT expose Meta's status-update channel. So we send a short Indonesian text
# bubble as the acknowledgment.
#
# See BIMA-Vault/Decisions.md for the full reasoning. Re-test if APTANA
# upgrades their wrapper.
#
# Fire-and-forget — caller doesn't await; never raises.

# Indonesian-language interim text. Kept short so it doesn't dominate the chat.
_ACK_BODY = "💭 Sebentar ya, BIMA sedang mencarikan info untukmu…"


async def acknowledge_received(message_id: str | None, recipient_phone: str) -> None:
    """Best-effort acknowledgment so the user sees activity within ~1s.

    Sends a short interim text via APTANA. The `message_id` argument is kept
    for forward compatibility — if APTANA ever exposes native typing, the
    handler can branch on it without changing the call site. Today it's
    unused.

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
    try:
        await send_text(recipient_phone=recipient_phone, body=_ACK_BODY)
    except Exception:
        # send_text already swallows network errors; this guard catches
        # anything else so a broken acknowledge can't kill the inbound handler.
        logger.exception("Acknowledgment send raised | to=%s", masked)
