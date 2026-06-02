"""Direct Meta WhatsApp Cloud API outbound sender.

Mirror of `services.whatsapp_sender` (APTANA BSP) and
`services.telegram_sender` (Telegram). Same shape — `send_text`,
`send_template`, `mark_as_read` — so the inbound router in
`routers/meta_whatsapp.py` can reuse the established `_process_inbound`
pattern from `routers/aptana.py`.

Why this exists
    Meta disabled the DPMPTSP WhatsApp Business Account on 2026-05-21
    (review pending). Direct Meta integration removes APTANA as a
    middleman: one fewer hop, one fewer outage surface, one fewer
    monthly bill. Meta's official Cloud API supports the same templates
    APTANA does (it's literally the same Meta backend).

Required env vars
    META_WA_PHONE_NUMBER_ID    — the number's ID (NOT the phone number).
                                 A long integer. From WhatsApp Manager.
    META_WA_TOKEN              — permanent system-user access token with
                                 `whatsapp_business_messaging` permission.

Reference: https://developers.facebook.com/docs/whatsapp/cloud-api
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Meta's recommended Graph API version. Pinning to a specific version so a
# Meta-side bump doesn't silently change payload shapes. Bump deliberately
# and re-test.
_GRAPH_API_VERSION = "v22.0"

_TIMEOUT_SECONDS = 15.0
_MAX_TEXT_LENGTH = 4096  # WhatsApp soft cap on body text

_PHONE_NUMBER_ID = os.getenv("META_WA_PHONE_NUMBER_ID", "").strip()
_ACCESS_TOKEN = os.getenv("META_WA_TOKEN", "").strip()

if not _PHONE_NUMBER_ID or not _ACCESS_TOKEN:
    logger.warning(
        "META_WA_PHONE_NUMBER_ID or META_WA_TOKEN unset — direct Meta "
        "outbound is disabled. Set both before flipping citizen traffic "
        "off APTANA. See `Meta WhatsApp Direct Setup.md` in the vault."
    )


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


async def send_text(
    recipient_phone: str,
    body: str,
    *,
    preview_url: bool = False,
) -> bool:
    """Send a free-form text message via Meta WhatsApp Cloud API.

    Returns True if Meta accepted the message (HTTP 200 + a message id),
    False on any failure (logged). Never raises.

    Caller responsibility — Meta's 24-hour service window: this only
    works if the recipient texted the BIMA number within the last 24h.
    Outside the window, use `send_template` instead. (The inbound router
    only sends text replies in the same conversation, so the window is
    always open there.)
    """
    if not _PHONE_NUMBER_ID or not _ACCESS_TOKEN:
        logger.error(
            "Meta WA send refused | not configured | to=%s",
            _mask_phone(recipient_phone),
        )
        return False

    if len(body) > _MAX_TEXT_LENGTH:
        logger.warning(
            "Meta WA message truncated | to=%s original_len=%d",
            _mask_phone(recipient_phone), len(body),
        )
        body = body[: _MAX_TEXT_LENGTH - 1] + "…"

    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_phone,
        "type": "text",
        "text": {
            "body": body,
            "preview_url": preview_url,
        },
    }
    return await _post(payload, recipient_phone, kind="text")


async def send_template(
    recipient_phone: str,
    template_name: str,
    language_code: str,
    body_params: list[str],
    *,
    url_button_param: str | None = None,
) -> bool:
    """Send a pre-approved template message.

    `body_params`: ordered list of strings to fill `{{1}}`, `{{2}}`, ….
    `url_button_param`: if the approved template has a URL button with a
    `{{1}}` placeholder, pass the value here. Most BIMA templates embed
    the URL in the body and don't use a separate button — leave None
    in that case.

    Meta rejects template sends if a body param is the empty string —
    pass a placeholder like "1-3" or "-" before calling (see the May 27
    eta_days incident — `routers/aptana.py` learned that the hard way).
    """
    if not _PHONE_NUMBER_ID or not _ACCESS_TOKEN:
        logger.error(
            "Meta WA template refused | not configured | to=%s tpl=%s",
            _mask_phone(recipient_phone), template_name,
        )
        return False

    components: list[dict[str, Any]] = []
    if body_params:
        components.append({
            "type": "body",
            "parameters": [
                {"type": "text", "text": str(p)} for p in body_params
            ],
        })
    if url_button_param is not None:
        components.append({
            "type": "button",
            "sub_type": "url",
            "index": "0",
            "parameters": [
                {"type": "text", "text": str(url_button_param)},
            ],
        })

    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": recipient_phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": components,
        },
    }
    return await _post(payload, recipient_phone, kind=f"template:{template_name}")


async def mark_as_read(message_id: str) -> None:
    """Send a read receipt for an inbound message. Fire-and-forget — the
    UX win (citizens see the double blue tick) is purely cosmetic, so
    failures are silent."""
    if not _PHONE_NUMBER_ID or not _ACCESS_TOKEN or not message_id:
        return
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            await client.post(_messages_url(), json=payload, headers=_headers())
    except (httpx.RequestError, asyncio.TimeoutError):
        pass


async def _post(payload: dict[str, Any], recipient: str, *, kind: str) -> bool:
    """Shared POST wrapper. Logs Meta's response codes uniformly."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            r = await client.post(_messages_url(), json=payload, headers=_headers())
    except (httpx.RequestError, asyncio.TimeoutError) as exc:
        logger.error(
            "Meta WA transport error | to=%s kind=%s err=%s",
            _mask_phone(recipient), kind, exc,
        )
        return False

    if r.status_code == 200:
        try:
            data = r.json()
            msg_id = (data.get("messages") or [{}])[0].get("id", "?")
        except Exception:
            msg_id = "?"
        logger.info(
            "Meta WA send ok | to=%s kind=%s msg_id=%s",
            _mask_phone(recipient), kind, msg_id,
        )
        return True

    # Meta returns structured errors: {"error": {"message": "...", "code": N,
    # "error_subcode": M, "fbtrace_id": "..."}}. Log the diagnostic so we can
    # tell rate-limit (130429) from invalid-number (131026) from auth (190).
    logger.error(
        "Meta WA send failed | to=%s kind=%s status=%d body=%s",
        _mask_phone(recipient), kind, r.status_code, r.text[:400],
    )
    return False
