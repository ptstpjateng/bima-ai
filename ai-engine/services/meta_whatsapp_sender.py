"""Direct Meta WhatsApp Cloud API — STATUS-ONLY helper (read receipt + typing).

This module is deliberately NOT a sender. APTANA (`services.whatsapp_sender`)
remains the sole outbound message transport for the citizen WhatsApp channel.
The single capability here is the native UX that APTANA's BSP wrapper cannot
expose: marking a citizen's inbound message as READ (blue double-ticks) and
showing the native "…" TYPING indicator — both in ONE Graph API call:

    POST https://graph.facebook.com/<version>/<phone_number_id>/messages
    {
      "messaging_product": "whatsapp",
      "status": "read",
      "message_id": "<wamid>",
      "typing_indicator": {"type": "text"}
    }

The typing indicator auto-dismisses after ~25s or when the next message is sent.

PLATFORM CAVEAT (drives the feature-flag design): this is the SAME
/{phone_number_id}/messages endpoint used to SEND, and it only accepts a token
issued by the ONE Meta app the number is registered to. BIMA's number
(6285117557091) is currently registered to APTANA's app (APTANA is the BSP).
A DPMPTSP Business-Manager token may therefore be rejected on this endpoint
with error 190 / "registered to another app". This module never raises and
never sends content — on any rejection the caller falls back to today's
working text-bubble acknowledgment. See BIMA-Vault/Decisions.md §9.

Required env vars (see ../.env.example):
    META_WA_PHONE_NUMBER_ID — the number's ID (a long integer), NOT the phone
                              number itself. Captured from APTANA's status
                              webhook metadata.phone_number_id (967104853150201).
    META_WA_TOKEN           — Meta access token with whatsapp_business_messaging,
                              valid for the APP THE NUMBER IS REGISTERED TO.
                              SECRET — never logged; only ever sent as a Bearer
                              header.
    META_GRAPH_VERSION      — optional, default "v22.0". Bump-able without a code
                              change (e.g. to "v23.0") once Meta floors it.

Reference: https://developers.facebook.com/docs/whatsapp/cloud-api/guides/mark-messages-as-read
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Meta Graph API version. Env-driven so a Meta-side floor bump (v22 → v23) is a
# config change, not a code change. Pinning a default keeps payload shapes
# stable until we bump deliberately and re-test.
_GRAPH_API_VERSION = os.getenv("META_GRAPH_VERSION", "v22.0").strip() or "v22.0"

_TIMEOUT_SECONDS = 15.0

_PHONE_NUMBER_ID = os.getenv("META_WA_PHONE_NUMBER_ID", "").strip()
_ACCESS_TOKEN = os.getenv("META_WA_TOKEN", "").strip()


def _messages_url() -> str:
    return f"https://graph.facebook.com/{_GRAPH_API_VERSION}/{_PHONE_NUMBER_ID}/messages"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def _mask_phone(msisdn: str | None) -> str:
    if not msisdn:
        return "<none>"
    digits = "".join(c for c in msisdn if c.isdigit())
    if len(digits) < 6:
        return "<short>"
    return digits[:4] + "…" + digits[-3:]


async def mark_read_and_typing(message_id: str, *, show_typing: bool = True) -> bool:
    """Mark an inbound message as read and (optionally) show the native typing
    indicator — in a single Meta Graph API call.

    Returns True ONLY on HTTP 200. Returns False (never raises) when:
      * the module is not configured (META_WA_* unset) or message_id is empty,
      * Meta returns a non-200 (e.g. error 190 / "registered to another app"
        because the number belongs to APTANA's app) — logged at WARNING with
        the status and a trimmed error body so the cause is visible,
      * the request times out or the network fails.

    NEVER logs the access token. The caller treats False as "fall back to the
    text-bubble acknowledgment".
    """
    if not _PHONE_NUMBER_ID or not _ACCESS_TOKEN or not message_id:
        return False

    payload: dict[str, object] = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    if show_typing:
        payload["typing_indicator"] = {"type": "text"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            r = await client.post(_messages_url(), json=payload, headers=_headers())
    except (httpx.RequestError, asyncio.TimeoutError) as exc:
        logger.warning(
            "Meta WA status transport error | err=%s typing=%s",
            exc.__class__.__name__, show_typing,
        )
        return False

    if r.status_code == 200:
        logger.info("mark_read_and_typing ok | typing=%s", show_typing)
        return True

    # Non-200. Meta returns {"error": {"message","code","error_subcode",
    # "fbtrace_id"}}. Log status + trimmed body (NO token) so a 190 /
    # "registered to another app" / permission failure is diagnosable.
    logger.warning(
        "Meta WA status failed | status=%d body=%s",
        r.status_code, r.text[:300],
    )
    return False
