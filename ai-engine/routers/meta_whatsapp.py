"""Direct Meta WhatsApp Cloud API webhook handler.

Two endpoints at one path:

    GET /webhook/meta/whatsapp
        The one-shot verification handshake Meta performs when you
        register the webhook in App settings. Meta sends:
            ?hub.mode=subscribe&hub.verify_token=<TOKEN>&hub.challenge=<NONCE>
        If hub.verify_token matches our META_WA_VERIFY_TOKEN, we return
        the hub.challenge value as a plain-text 200. Otherwise 403.

    POST /webhook/meta/whatsapp
        Every inbound WhatsApp message + status update lands here. Meta
        HMAC-signs the request body — we verify
        X-Hub-Signature-256 against META_WA_APP_SECRET (constant-time)
        before parsing anything. Hands off text messages to a
        BackgroundTask so Meta gets a fast 200; status-update payloads
        are logged and acknowledged.

Why a separate router (not extending routers/aptana.py)
    Different auth model (Meta HMACs the body; APTANA uses a path
    secret). Different inbound payload shape (Meta's deeply nested
    entry/changes/value/messages vs APTANA's flat envelope). Trying to
    branch inside one handler obscures both. Keep them as parallel
    routes; the AI pipeline downstream is shared.

Required env vars (see Meta WhatsApp Direct Setup.md in the vault)
    META_WA_VERIFY_TOKEN  — chosen by us; entered in Meta App Config.
                            Used only in the GET handshake.
    META_WA_APP_SECRET    — Meta App Secret (Settings → Basic). Signs
                            every POST. NOT the access token.

Reference: https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from services.ai_handler import generate_ai_response, log_to_backend
from services.meta_whatsapp_sender import mark_as_read, send_text

logger = logging.getLogger(__name__)
router = APIRouter()

_VERIFY_TOKEN = os.getenv("META_WA_VERIFY_TOKEN", "").strip()
_APP_SECRET = os.getenv("META_WA_APP_SECRET", "").strip()

if not _VERIFY_TOKEN:
    logger.warning(
        "META_WA_VERIFY_TOKEN unset — the Meta webhook handshake will fail. "
        "Set it before registering the callback URL in Meta App Config."
    )
if not _APP_SECRET:
    logger.warning(
        "META_WA_APP_SECRET unset — every Meta webhook POST will be rejected "
        "(403). Set it from Meta App Settings → Basic → App Secret."
    )

_GENERATION_FAILURE_APOLOGY = (
    "Maaf, sistem BIMA sedang sibuk sekali. Silakan coba lagi sebentar lagi 🙏"
)


# ---------------------------------------------------------------------------
# Verification (GET) — Meta App config handshake
# ---------------------------------------------------------------------------


@router.get("/webhook/meta/whatsapp", response_class=PlainTextResponse)
async def meta_verify(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
) -> str:
    """One-shot verification handshake.

    Meta calls this exactly once when you save the webhook URL in App
    config. We echo `hub.challenge` if the verify-token matches; Meta
    then activates the webhook for the chosen fields.
    """
    if hub_mode != "subscribe":
        raise HTTPException(status_code=400, detail="unexpected_mode")
    if not _VERIFY_TOKEN or not hmac.compare_digest(hub_verify_token, _VERIFY_TOKEN):
        logger.warning("Meta verify rejected | mode=%s", hub_mode)
        raise HTTPException(status_code=403, detail="invalid_verify_token")
    logger.info("Meta webhook verification handshake OK")
    return hub_challenge


# ---------------------------------------------------------------------------
# Inbound (POST) — every message + status update from Meta
# ---------------------------------------------------------------------------


def _verify_signature(body: bytes, header_value: str | None) -> bool:
    """Constant-time HMAC-SHA256 check against the App Secret.

    Header format: `X-Hub-Signature-256: sha256=<hex>`.
    Returns True iff the header's hex matches HMAC-SHA256(body, APP_SECRET).
    """
    if not _APP_SECRET or not header_value:
        return False
    if not header_value.startswith("sha256="):
        return False
    provided_hex = header_value[len("sha256="):]
    expected_hex = hmac.new(
        _APP_SECRET.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(provided_hex, expected_hex)


def _mask_phone(msisdn: str | None) -> str:
    if not msisdn:
        return "<none>"
    digits = "".join(c for c in msisdn if c.isdigit())
    if len(digits) < 6:
        return "<short>"
    return digits[:4] + "…" + digits[-3:]


@router.post("/webhook/meta/whatsapp")
async def meta_inbound(
    request: Request,
    background: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
):
    """Receive a Meta webhook event. Returns {ok:true} fast — every real
    work (AI generation, send, log) runs in a BackgroundTask.

    Meta retries non-2xx aggressively, so we ack 200 even for events we
    don't act on (status updates, non-text messages). Genuine auth
    failures still return 403.
    """
    raw = await request.body()
    if not _verify_signature(raw, x_hub_signature_256):
        logger.warning(
            "Meta inbound rejected | bad signature | header_present=%s body_bytes=%d",
            bool(x_hub_signature_256), len(raw),
        )
        raise HTTPException(status_code=403, detail="bad_signature")

    try:
        envelope = await request.json()
    except ValueError:
        logger.error("Meta inbound dropped | invalid JSON | body_bytes=%d", len(raw))
        # 200 so Meta stops retrying a permanently-broken payload.
        return {"ok": True, "skipped": "invalid_json"}

    # Meta webhook envelope:
    #   {"object": "whatsapp_business_account",
    #    "entry": [{"id": "<WABA>", "changes": [{"value": {...}, "field": "messages"}]}]}
    # We loop because a single POST CAN carry multiple entries / changes
    # in burst scenarios (rare for inbound text, common for status updates).
    for entry in envelope.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            _handle_value(value, background)

    return {"ok": True}


def _handle_value(value: dict[str, Any], background: BackgroundTasks) -> None:
    """Process one `entry.changes[i].value` object — may carry inbound
    messages (`messages[]`) AND delivery status updates (`statuses[]`)."""

    # ---------- inbound messages ----------
    contacts = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in (value.get("contacts") or [])}

    for msg in value.get("messages") or []:
        msg_type = msg.get("type")
        msisdn = msg.get("from")
        msg_id = msg.get("id")

        if msg_type != "text":
            # Non-text (image, document, audio, sticker, location, reaction).
            # Acknowledge with a polite "we only handle text for now" reply so
            # the user isn't ghosted. Read receipt fires regardless.
            logger.info(
                "Meta inbound | type=%s from=%s msg_id=%s — non-text, replying with stub",
                msg_type, _mask_phone(msisdn), msg_id,
            )
            background.add_task(mark_as_read, msg_id)
            background.add_task(
                send_text,
                recipient_phone=msisdn,
                body=(
                    "Maaf, untuk sekarang BIMA hanya bisa baca pesan teks. "
                    "Boleh ketik pertanyaannya? Misalnya: 'syarat NIB UMKM apa saja?'"
                ),
            )
            continue

        text = (msg.get("text") or {}).get("body") or ""
        name = contacts.get(msisdn)

        if not msisdn or not text.strip():
            logger.info(
                "Meta inbound dropped | missing=%s",
                "phone" if not msisdn else "text",
            )
            continue

        logger.info(
            "Meta inbound | from=%s name=%s msg_id=%s text=%r",
            _mask_phone(msisdn), name, msg_id, text[:80],
        )
        background.add_task(
            _process_inbound,
            msisdn=msisdn,
            text=text,
            name=name,
            message_id=msg_id,
        )

    # ---------- status updates (delivery receipts) ----------
    for status in value.get("statuses") or []:
        # Useful for an outbound-delivery analytics table later; for now
        # just log so demo failures are diagnosable.
        logger.info(
            "Meta status | id=%s status=%s recipient=%s",
            status.get("id"),
            status.get("status"),
            _mask_phone(status.get("recipient_id")),
        )


async def _process_inbound(
    msisdn: str,
    text: str,
    name: str | None,
    message_id: str | None,
) -> None:
    """End-to-end inbound pipeline. Mirror of routers/aptana._process_inbound."""
    # Channel-namespaced user_id distinguishes direct-Meta WhatsApp from
    # APTANA-routed WhatsApp; same citizen using both numbers would otherwise
    # collide in ai_handler's per-user state dict.
    user_id = f"wam-{msisdn}"

    # Read receipt — cosmetic UX win. Fire-and-forget.
    asyncio.create_task(mark_as_read(message_id or ""))

    try:
        reply = await generate_ai_response(user_id, text)
    except Exception:
        logger.exception("Meta AI generation failed | user=%s", _mask_phone(msisdn))
        try:
            await send_text(recipient_phone=msisdn, body=_GENERATION_FAILURE_APOLOGY)
        except Exception:
            logger.exception("Meta apology send failed | user=%s", _mask_phone(msisdn))
        return

    delivered = await send_text(recipient_phone=msisdn, body=reply)
    if not delivered:
        logger.error("Meta delivery failed | user=%s", _mask_phone(msisdn))

    try:
        await log_to_backend(user_id, text, reply, channel="whatsapp_meta")
    except Exception:
        logger.exception("Meta backend log failed | user=%s", _mask_phone(msisdn))
