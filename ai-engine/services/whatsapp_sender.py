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

# Native WhatsApp typing+read via Meta Graph (services.meta_whatsapp_sender).
# OFF by default — see acknowledge_received() and BIMA-Vault/Decisions.md §9.
#   _NATIVE_TYPING_ENABLED : master flag. When false/unset, acknowledge_received
#                            behaves EXACTLY as before (text bubble).
#   _KEEP_TEXT_BUBBLE      : when native is on, ALSO fire the text bubble
#                            (belt-and-suspenders). Default false → once native
#                            is confirmed working, the duplicate bubble is dropped.
_NATIVE_TYPING_ENABLED = os.getenv("BIMA_NATIVE_TYPING_ENABLED", "false").lower() in ("1", "true", "yes")
_KEEP_TEXT_BUBBLE = os.getenv("BIMA_NATIVE_TYPING_KEEP_TEXT_BUBBLE", "false").lower() in ("1", "true", "yes")

# Interim "💭 Sebentar ya…" text bubble. Default OFF as of 2026-06-02 (user
# decision): the bubble cluttered every exchange with an extra message, and the
# native typing indicator isn't available via APTANA, so we accept a short
# silent gap during generation in exchange for a clean chat. Set
# BIMA_ACK_TEXT_BUBBLE_ENABLED=true to bring it back without a redeploy.
_ACK_TEXT_BUBBLE_ENABLED = os.getenv("BIMA_ACK_TEXT_BUBBLE_ENABLED", "false").lower() in ("1", "true", "yes")

# One-time INFO hint so "flag on but META_WA_* unset" doesn't spam the logs.
_native_hint_logged = False

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


# WhatsApp text does NOT render Markdown links ([label](url)) or **double-star**
# bold — they ship as literal characters. The LLM sometimes emits Markdown, so
# normalise the common shapes to WhatsApp's own formatting (or plain text)
# before sending, so replies look clean on the phone.
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_MD_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")


def format_for_whatsapp(text: str) -> str:
    """Convert the Markdown the LLM commonly emits into WhatsApp-safe text:
    [label](url) -> 'url' (or 'label: url'), and **bold** -> *bold*."""
    if not text:
        return text

    def _link(m: "re.Match[str]") -> str:
        label, url = m.group(1).strip(), m.group(2).strip()
        bare = url.split("//", 1)[-1]
        # Label is just the URL (or its bare domain) → drop it; WhatsApp
        # auto-links a plain URL.
        if label == url or label.rstrip("/") == bare.rstrip("/"):
            return url
        return f"{label}: {url}"

    out = _MD_LINK.sub(_link, text)
    out = _MD_BOLD.sub(r"*\1*", out)
    return out


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

    body = format_for_whatsapp(body)

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


async def send_document(
    recipient_phone: str,
    link: str,
    filename: str,
    *,
    caption: str | None = None,
) -> bool:
    """Send a WhatsApp *document* message — a PDF link APTANA fetches + delivers.

    `link` must be a publicly reachable URL; BIMA hosts generated PDFs at
    `/dl/{token}` (services.generated_docs + routers.downloads). Mirrors
    send_text's transport (same /api/v1/messages, Api-Token header, retry
    ladder); kept standalone so the notify path in send_text stays untouched.
    The WhatsApp 24-hour session-window rule applies. Never raises.
    """
    if not _API_TOKEN or not _SENDER_NUMBER:
        logger.error("Cannot send WhatsApp document: APTANA_API_TOKEN or APTANA_SENDER_NUMBER unset.")
        return False

    url = f"{_API_BASE}/api/v1/messages"
    headers = {
        "Api-Token": _API_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    document: dict[str, str] = {"link": link, "filename": filename}
    if caption:
        document["caption"] = format_for_whatsapp(caption)[:1024]
    payload = {
        "channel": "wa",
        "sender": _SENDER_NUMBER,
        "recipient": normalize_phone(recipient_phone),
        "type": "document",
        "document": document,
    }
    masked_to = payload["recipient"][:4] + "…" + payload["recipient"][-4:]

    for attempt in range(_MAX_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=payload, headers=headers)

            if resp.status_code < 300:
                logger.info(
                    "APTANA send-doc ok | to=%s status=%d attempt=%d",
                    masked_to, resp.status_code, attempt + 1,
                )
                return True

            if resp.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS - 1:
                wait = 2 ** attempt
                logger.warning(
                    "APTANA send-doc transient | to=%s status=%d attempt=%d retry_in=%ds",
                    masked_to, resp.status_code, attempt + 1, wait,
                )
                await asyncio.sleep(wait)
                continue

            logger.error(
                "APTANA send-doc failed | to=%s status=%d body=%s",
                masked_to, resp.status_code, resp.text[:500],
            )
            return False

        except (httpx.RequestError, asyncio.TimeoutError) as exc:
            if attempt < _MAX_ATTEMPTS - 1:
                wait = 2 ** attempt
                logger.warning(
                    "APTANA send-doc error | to=%s err=%s attempt=%d retry_in=%ds",
                    masked_to, exc.__class__.__name__, attempt + 1, wait,
                )
                await asyncio.sleep(wait)
            else:
                logger.error("APTANA send-doc exhausted retries | to=%s err=%s", masked_to, exc)

    return False


# ---------------------------------------------------------------------------
# Typing acknowledgment.
# ---------------------------------------------------------------------------
#
# DEFAULT (locked, May 2026): a short Indonesian text bubble.
#   We probed APTANA's /api/v1/messages with Meta's native read+typing status
#   fields (status, message_id, typing_indicator) and got HTTP 422 with explicit
#   rejection of all three field names — APTANA's wrapper enforces a payload
#   whitelist of message TYPES (text, template, document, image, …) and does
#   NOT expose Meta's status-update channel. So APTANA can never do native UX.
#
# OPTIONAL NATIVE PATH (behind BIMA_NATIVE_TYPING_ENABLED): when enabled AND a
#   Meta token + phone-number-id are configured, we call Meta's Graph API
#   directly (services.meta_whatsapp_sender.mark_read_and_typing) to mark the
#   inbound READ (blue ticks) and show the native "…" typing bubble in one call.
#   This is the ONLY path that yields the native UX. It is gated OFF by default
#   because the number is registered to APTANA's app, so a DPMPTSP token may be
#   rejected (error 190 / "registered to another app") — in which case we fall
#   back to the text bubble below. See BIMA-Vault/Decisions.md §9.
#
# Fire-and-forget — caller doesn't await; never raises.

# Indonesian-language interim text. Kept short so it doesn't dominate the chat.
# Brand signal — locked per Decisions §9; do NOT change the wording/emoji.
_ACK_BODY = "💭 Sebentar ya, BIMA sedang mencarikan info untukmu…"


async def acknowledge_received(message_id: str | None, recipient_phone: str) -> None:
    """Best-effort acknowledgment so the user sees activity within ~1s.

    Decision tree (never raises — safe to fire-and-forget):
      1. If native typing is disabled, or the Meta config / message_id is
         missing → send the "💭 Sebentar ya…" text bubble via APTANA, exactly
         as before. (A one-time INFO hint fires if the flag is on but the
         META_WA_* config is absent, so it never spams.)
      2. If native typing is enabled AND configured AND message_id present →
         call Meta's mark_read_and_typing(message_id). On success, skip the
         text bubble (unless BIMA_NATIVE_TYPING_KEEP_TEXT_BUBBLE is set) so the
         chat stays clean. On any failure / non-200 → fall back to the text
         bubble so the user still sees activity.

    Call as fire-and-forget:
        asyncio.create_task(acknowledge_received(msg_id, msisdn))
    """
    global _native_hint_logged

    if not _API_TOKEN or not _SENDER_NUMBER:
        return  # send_text would log the same warning; stay silent here.

    masked = (
        recipient_phone[:4] + "…" + recipient_phone[-4:]
        if len(recipient_phone) >= 8
        else "<short>"
    )

    # --- Native-first branch (Meta Graph read + typing), behind the flag. ---
    if _NATIVE_TYPING_ENABLED:
        try:
            # Lazy import keeps the APTANA-only deploy free of a hard dependency
            # on the Meta module and reads its env-driven config at call time.
            from services.meta_whatsapp_sender import (
                _ACCESS_TOKEN as _meta_token,
                _PHONE_NUMBER_ID as _meta_phone_id,
                mark_read_and_typing,
            )

            if not _meta_token or not _meta_phone_id:
                if not _native_hint_logged:
                    logger.info(
                        "BIMA_NATIVE_TYPING_ENABLED is true but META_WA_* unset "
                        "— falling back to text bubble."
                    )
                    _native_hint_logged = True
            elif message_id:
                native_ok = await mark_read_and_typing(message_id)
                if native_ok and not _KEEP_TEXT_BUBBLE:
                    return  # native UX shown; skip the duplicate text bubble.
                # native failed (e.g. token rejected) OR KEEP_TEXT_BUBBLE on →
                # fall through to the text bubble below.
            # message_id missing → fall through to the text bubble below.
        except Exception:
            # Native path must NEVER kill the handler. Mask + fall back.
            logger.warning("Native typing attempt raised | to=%s — falling back", masked)

    # --- Text-bubble acknowledgment (OFF by default since 2026-06-02). ---
    # When native typing is unavailable AND the bubble is disabled, this is a
    # clean no-op: the citizen sees no interim message, just BIMA's real reply
    # when it's ready. Re-enable with BIMA_ACK_TEXT_BUBBLE_ENABLED=true.
    if not _ACK_TEXT_BUBBLE_ENABLED:
        return
    try:
        await send_text(recipient_phone=recipient_phone, body=_ACK_BODY)
    except Exception:
        # send_text already swallows network errors; this guard catches
        # anything else so a broken acknowledge can't kill the inbound handler.
        logger.exception("Acknowledgment send raised | to=%s", masked)
