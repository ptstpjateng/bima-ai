"""APTANA WhatsApp BSP webhook handlers.

Two endpoints, both authenticated by a path-based shared secret because APTANA does
NOT HMAC-sign its webhook bodies (confirmed in their docs — only TLS encrypts
the payload in transit).

    POST /webhook/aptana/inbound/{path_secret}
        Receives Autopilot Worker callbacks (trigger: "Receive a Message from Meta").
        This is the user → BIMA channel. Hands off to the AI pipeline + replies via
        whatsapp_sender.send_text. Runs as a BackgroundTask so APTANA gets 200 fast.

    POST /webhook/aptana/status/{path_secret}
        Receives outbound delivery receipts (URL Callback Webhook OR a "Receive
        Message Status Update" Worker — pick ONE in APTANA dashboard, both deliver
        the same data). For now we just log; future: write to a whatsapp_sends
        table for "did my permit notification land?" admin queries.

This router is NOT registered in main.py yet — add `from routers import aptana;
app.include_router(aptana.router)` once APTANA setup is verified end-to-end and
you're ready to switch over from the legacy Telegram/Meta-direct pipeline.

Required env vars (see .env.example):
    APTANA_PATH_SECRET — long random string; same value in the APTANA webhook URL
                         path and in this env var. Generate with `openssl rand -hex 32`.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from services.ai_handler import generate_ai_response, log_to_backend
from services.input_sanitizer import sanitize_user_input
from services.notifications import record_engagement, record_opt_out
from services.prompt_injection_detector import audit_user_input
from services.whatsapp_sender import acknowledge_received, send_text

# Indonesian apology shown when Gemma fails (rate-limited / 5xx / timeout) AFTER
# the user has already seen the "💭 Sebentar ya…" acknowledgment. Without this
# fallback they're stranded with no reply at all — worse UX than a polite error.
_GEMMA_FAILURE_APOLOGY = "Maaf, sistem BIMA sedang sibuk sekali. Silakan coba lagi sebentar lagi 🙏"

logger = logging.getLogger(__name__)
router = APIRouter()

_PATH_SECRET = os.getenv("APTANA_PATH_SECRET", "")
if not _PATH_SECRET:
    logger.warning(
        "APTANA_PATH_SECRET is unset — APTANA webhooks will reject all requests. "
        "Generate one with `openssl rand -hex 32`, set both in ai-engine/.env and "
        "in the APTANA webhook URL path before going live."
    )


def _check_path_secret(provided: str) -> None:
    """Constant-time check against the configured secret. Raises 403 on mismatch."""
    if not _PATH_SECRET or not hmac.compare_digest(provided, _PATH_SECRET):
        raise HTTPException(status_code=403, detail="forbidden")


# ---------------------------------------------------------------------------
# Inbound: user → BIMA
# ---------------------------------------------------------------------------

# Confirmed APTANA Worker body shape for trigger `Receive a Message from Meta`
# (verified from default template in Autopilot UI, 2026-05-13):
#   {
#     "phoneNumberId": "{{phoneNumberId}}",   # APTANA's WABA identifier
#     "phoneNumber":   "{{phoneNumber}}",     # SENDER's phone (the user)
#     "name":          "{{name}}",            # WhatsApp display name
#     "payload":       "{{payload}}",         # Meta-shaped message JSON (text lives in here)
#     "receivedAt":    "{{receivedAt}}"
#   }
# `payload` is the meaty bit — likely a JSON-encoded string of the Meta webhook
# envelope. We probe a few shapes in _extract_text_from_payload below.

@router.post("/webhook/aptana/inbound/{path_secret}")
async def aptana_inbound(path_secret: str, request: Request, background: BackgroundTasks):
    """Receive an inbound WhatsApp user message from APTANA Autopilot."""
    _check_path_secret(path_secret)

    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_json")

    msisdn = data.get("phoneNumber")
    name = data.get("name")
    phone_number_id = data.get("phoneNumberId")
    raw_payload = data.get("payload")

    # Log envelope at INFO and full payload at DEBUG for the first weeks. Once
    # _extract_text_from_payload proves stable on real traffic, drop the DEBUG line.
    logger.info(
        "APTANA inbound | from=%s name=%s phone_id=%s payload_type=%s",
        _mask(msisdn or ""), name, phone_number_id, type(raw_payload).__name__,
    )
    logger.debug("APTANA inbound full | %s", data)

    text = _extract_text_from_payload(raw_payload)
    message_id = _extract_message_id_from_payload(raw_payload)

    if not msisdn or not text:
        logger.warning(
            "APTANA inbound dropped | missing=%s payload_preview=%s",
            "phone" if not msisdn else "text",
            (raw_payload[:300] if isinstance(raw_payload, str) else str(raw_payload)[:300]),
        )
        # 200 so APTANA doesn't retry — it's a parser bug or non-text message,
        # not a transient failure. Investigate from logs.
        return {"ok": True, "skipped": "missing_fields"}

    # Sanitise BEFORE handing off to the background task. Done synchronously
    # here (cheap, O(n)) so the audit log line happens in the same logical
    # request that received the message — easier to correlate with APTANA's
    # request id during incident review.
    sanitized = sanitize_user_input(text)
    if not sanitized:
        # Message collapsed to empty after stripping control chars / bidi /
        # zero-width markers. Almost always a hostile payload (or a sticker
        # / image misrouted as text). 200 + skipped so APTANA doesn't retry.
        logger.warning(
            "APTANA inbound dropped | reason=sanitized_empty user=%s",
            _mask(msisdn),
        )
        return {"ok": True, "skipped": "sanitized_empty"}
    audit_user_input(f"wa-{msisdn}", sanitized)

    # Meta compliance: record engagement (opt-in) + check for STOP/opt-out.
    # Done synchronously so the records are committed before the background task
    # reads them — record_engagement is O(1) with in-memory set + async file write.
    _OPT_OUT_TOKENS = {"stop", "berhenti", "unsubscribe", "opt out", "optout", "hentikan notif"}
    if sanitized.lower().strip() in _OPT_OUT_TOKENS:
        record_opt_out(msisdn)
        background.add_task(
            send_text,
            recipient_phone=msisdn,
            body=(
                "Oke, notifikasi otomatis dari BIMA untuk nomor ini sudah kami nonaktifkan. "
                "Anda masih bisa tanya apa saja kapan saja — BIMA tetap siap membantu 🙏"
            ),
        )
        return {"ok": True, "opted_out": True}

    record_engagement(msisdn)

    # Greeting dedup — WhatsApp fires BOTH ReceiveGreetingRequest AND
    # ReceiveMessageMeta for a user's very first message. The APTANA Greeting
    # worker forwards the greeting event here AND the Inbound worker forwards
    # the message event here, often within milliseconds of each other.
    # If _send_greeting already fired for this number in the last few seconds,
    # skip AI processing — the greeting IS the welcome response. Without this
    # guard the user sees the greeting message + a second AI reply to "haloo".
    if _was_recently_greeted(msisdn):
        logger.info(
            "APTANA inbound deduped (greeting just sent) | user=%s text=%r",
            _mask(msisdn), sanitized[:40],
        )
        return {"ok": True, "skipped": "greeted"}

    background.add_task(
        _process_inbound, msisdn=msisdn, text=sanitized, name=name, message_id=message_id
    )
    return {"ok": True}


async def _process_inbound(
    msisdn: str, text: str, name: str | None = None, message_id: str | None = None
) -> None:
    """End-to-end pipeline for one inbound message: AI reply + send back + log."""
    # Channel-namespaced user_id avoids the cross-tenant collision noted in
    # ai_handler.py:81 (flat in-memory dict keyed by stringified user_id).
    user_id = f"wa-{msisdn}"
    logger.info(
        "APTANA processing | user=%s name=%s msg_id=%s text=%r",
        _mask(msisdn), name, message_id or "<none>", text[:80],
    )

    # Fire-and-forget acknowledgment so the user sees activity within ~1s.
    # Tries native WhatsApp typing indicator via APTANA; falls back to a short
    # interim text. Never blocks the slow generation path below.
    asyncio.create_task(acknowledge_received(message_id, msisdn))

    # Officer chat-bridge FAST-PATH: if this number is the configured demo
    # officer with an active case session, route the reply into the Officer
    # Copilot instead of the citizen AI. Feature-flagged; returns None when
    # the bridge is off or the sender isn't an officer-in-session.
    try:
        from services import officer_bridge

        officer_reply = await officer_bridge.maybe_handle_officer_reply(
            channel=officer_bridge.CHANNEL_WHATSAPP,
            channel_id=msisdn,
            message=text,
        )
    except Exception:
        logger.exception("Officer bridge crashed | user=%s", _mask(msisdn))
        officer_reply = None

    if officer_reply is not None:
        delivered = await send_text(recipient_phone=msisdn, body=officer_reply)
        if not delivered:
            logger.error("APTANA officer delivery failed | user=%s", _mask(msisdn))
        return

    try:
        reply = await generate_ai_response(user_id, text)
    except Exception:
        logger.exception("AI generation failed | user=%s", _mask(msisdn))
        # Send a polite Indonesian apology so the user isn't stranded after the
        # "Sebentar ya…" acknowledgment. Don't log_to_backend — that path
        # expects a real reply, not a fallback string.
        try:
            await send_text(recipient_phone=msisdn, body=_GEMMA_FAILURE_APOLOGY)
        except Exception:
            logger.exception("Apology send failed | user=%s", _mask(msisdn))
        return

    delivered = await send_text(recipient_phone=msisdn, body=reply)
    if not delivered:
        logger.error("APTANA delivery failed | user=%s", _mask(msisdn))

    try:
        await log_to_backend(user_id, text, reply, channel="whatsapp")
    except Exception:
        logger.exception("Backend log failed | user=%s", _mask(msisdn))


# ---------------------------------------------------------------------------
# Greeting: first-contact welcome (APTANA Worker `Receive a Greeting Request from Meta`)
# ---------------------------------------------------------------------------
#
# WhatsApp fires a "greeting request" the first time a brand-new user opens
# a chat with the bot, BEFORE they type anything. APTANA exposes this as its
# own Worker trigger; configure a Worker pointing at this endpoint to get a
# proactive welcome bubble.
#
# The payload shape isn't documented in APTANA's Postman docs. We probe the
# same shapes _extract_text_from_payload uses; in practice the trigger only
# carries `phoneNumber` (the user) — `payload` may be empty or omitted.

# Static welcome — no AI generation, no per-user RAG. Indonesian, Midnight
# Government tone (calm, helpful, government-appropriate). One emoji max.
_GREETING_BODY = (
    "Halo! 👋 Saya BIMA, asisten AI perizinan UMKM dari DPMPTSP Jawa Tengah.\n\n"
    "Tanyakan saja apapun tentang KBLI, syarat NIB, atau alur OSS RBA — saya "
    "siap bantu 24 jam, dalam Bahasa Indonesia.\n\n"
    "Coba mulai dengan:\n"
    "• \"Saya mau buka warung makan, KBLI berapa?\"\n"
    "• \"Apa syarat NIB untuk usaha mikro?\"\n"
    "• \"Berapa lama proses izin OSS RBA?\""
)

# ---------------------------------------------------------------------------
# Greeting dedup — prevents double messages when WhatsApp fires BOTH a
# ReceiveGreetingRequest AND a ReceiveMessageMeta for the same first-contact
# message (e.g. user opens chat and immediately types "halo").
#
# When the greeting endpoint fires, we stamp the msisdn with the current
# time. The inbound handler checks this stamp: if a greeting went out for
# this number within the last N seconds, the inbound is silently skipped
# (the greeting IS the response). The window is short — just long enough
# for both APTANA workers to fire and arrive at BIMA.
# ---------------------------------------------------------------------------
import time as _time
_GREETING_DEDUP_WINDOW_SECONDS = 8.0
_recently_greeted: dict[str, float] = {}   # msisdn → unix timestamp of last greeting send


def _mark_greeted(msisdn: str) -> None:
    _recently_greeted[msisdn] = _time.time()
    # Prune entries older than the window to keep memory bounded.
    cutoff = _time.time() - _GREETING_DEDUP_WINDOW_SECONDS
    for k in [k for k, v in list(_recently_greeted.items()) if v < cutoff]:
        _recently_greeted.pop(k, None)


def _was_recently_greeted(msisdn: str) -> bool:
    ts = _recently_greeted.get(msisdn)
    return ts is not None and (_time.time() - ts) < _GREETING_DEDUP_WINDOW_SECONDS


@router.post("/webhook/aptana/greeting/{path_secret}")
async def aptana_greeting(path_secret: str, request: Request, background: BackgroundTasks):
    """Receive WhatsApp first-contact greeting and send a proactive welcome.

    Configure the matching APTANA Worker:
        Trigger: WhatsApp → Receive a Greeting Request from Meta
        Action:  HTTP POST → https://nolongin.com/webhook/aptana/greeting/<APTANA_PATH_SECRET>
        Body:    {"phoneNumber": "{{phoneNumber}}", "name": "{{name}}"}
    """
    _check_path_secret(path_secret)

    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_json")

    msisdn = data.get("phoneNumber")
    name = data.get("name")

    logger.info("APTANA greeting | from=%s name=%s", _mask(msisdn or ""), name)

    if not msisdn:
        # 200 so APTANA doesn't retry — payload bug, not transient.
        return {"ok": True, "skipped": "missing_phone"}

    # Fire-and-forget so APTANA gets a fast 200. The welcome itself is the
    # only side-effect — no DB log, no AI call. Cheap.
    background.add_task(_send_greeting, msisdn=msisdn, name=name)
    return {"ok": True}


async def _send_greeting(msisdn: str, name: str | None = None) -> None:
    """Send the static welcome message; never raises."""
    body = _GREETING_BODY

    # Personalise if APTANA provided a real display name.
    # Guard: APTANA's ReceiveGreetingRequest trigger may send the literal
    # string "{{name}}" when the variable is unavailable — detect this and
    # fall back to the generic greeting so citizens never see "Halo {{name}}!".
    if name and not name.startswith("{{"):
        first_name = name.strip().split(" ")[0][:30]
        body = body.replace("Halo! 👋", f"Halo {first_name}! 👋", 1)

    # Stamp this number as recently greeted BEFORE the send so the inbound
    # handler sees it even if the send is slow.
    _mark_greeted(msisdn)
    record_engagement(msisdn)   # greeting counts as opt-in

    delivered = await send_text(recipient_phone=msisdn, body=body)
    if not delivered:
        logger.error("APTANA greeting send failed | user=%s", _mask(msisdn))


# ---------------------------------------------------------------------------
# Status receipts: outbound delivery callbacks
# ---------------------------------------------------------------------------

@router.post("/webhook/aptana/status/{path_secret}")
async def aptana_status(path_secret: str, request: Request):
    """Receive WhatsApp Business webhook events from APTANA (the
    `ReceiveWhatsAppBusinessWebhookMeta` worker → POST here).

    The body shape APTANA forwards is:
        {"wabaId": "...", "payload": <Meta webhook envelope>, "receivedAt": "..."}

    `payload` is Meta's full WhatsApp Cloud API webhook envelope:
        {object, entry:[{id, changes:[{field:"messages", value:{...}}]}]}
    where `value` carries `statuses[]` (sent/delivered/read/failed receipts for
    messages BIMA sent), `messages[]` (inbound — handled by the inbound worker),
    and/or `errors[]`.

    We DECODE and log a clean one-line summary per event instead of dumping the
    whole blob (the raw envelope is huge — APTANA inlines a full price-list — and
    contains PII). Full raw dump stays at DEBUG for deep debugging only.

    This endpoint does NOT act on the events yet — it's the visibility layer.
    Read receipts captured here are the foundation for "citizen read the
    notification" tracking; see BIMA-Vault for the roadmap.
    """
    _check_path_secret(path_secret)

    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_json")

    # Full raw envelope only at DEBUG (off by default) — never INFO (PII + noise).
    logger.debug("APTANA status raw | %s", body)

    envelope = body.get("payload") if isinstance(body, dict) else None
    # APTANA may forward `payload` as a dict (observed) or as a JSON string.
    if isinstance(envelope, str):
        try:
            envelope = json.loads(envelope)
        except (json.JSONDecodeError, ValueError):
            envelope = None

    if not isinstance(envelope, dict):
        # Either a legacy flat APTANA callback or an unparseable shape — log a
        # minimal line so we still see that *something* arrived.
        logger.info(
            "APTANA status | unrecognized shape | top_keys=%s",
            sorted(body.keys()) if isinstance(body, dict) else type(body).__name__,
        )
        return {"ok": True}

    _log_meta_webhook_summary(envelope)
    return {"ok": True}


def _log_meta_webhook_summary(envelope: dict[str, Any]) -> None:
    """Walk a Meta webhook envelope and log a clean summary per sub-event.

    Surfaces exactly what event TYPES arrive so we can confirm what the
    `ReceiveWhatsAppBusinessWebhookMeta` passthrough actually delivers
    (status receipts incl. `read`, inbound messages, errors, or anything
    unexpected like a presence/typing event).
    """
    for entry in envelope.get("entry") or []:
        for change in entry.get("changes") or []:
            field = change.get("field")
            value = change.get("value")
            if not isinstance(value, dict):
                logger.info("APTANA status | field=%s (no value object)", field)
                continue

            statuses = value.get("statuses") or []
            messages = value.get("messages") or []
            errors = value.get("errors") or []
            # Any keys beyond the known ones reveal new event types (e.g. typing).
            known = {"messaging_product", "metadata", "contacts", "statuses",
                     "messages", "errors"}
            extra = sorted(set(value.keys()) - known)

            for st in statuses:
                err = st.get("errors") or []
                err_codes = [e.get("code") for e in err] if err else None
                st_value = st.get("status")               # sent|delivered|read|failed
                recipient = st.get("recipient_id", "")
                logger.info(
                    "APTANA status | field=%s status=%s recipient=%s wamid=…%s "
                    "category=%s errors=%s",
                    field,
                    st_value,
                    _mask(recipient),
                    str(st.get("id", ""))[-8:],
                    (st.get("conversation") or {}).get("origin", {}).get("type"),
                    err_codes,
                )
                # Attach this status to the ticket whose notification we sent
                # to this recipient (read-receipt tracking). Never fatal.
                try:
                    from services import delivery_tracker
                    if recipient and st_value:
                        delivery_tracker.update_status(
                            recipient, st_value, st.get("timestamp")
                        )
                except Exception:
                    logger.exception("delivery tracker update_status failed (non-fatal)")

            for msg in messages:
                # Inbound messages also arrive via the raw passthrough; the
                # inbound worker handles them. Log only that they appeared.
                logger.info(
                    "APTANA status | field=%s INBOUND message type=%s from=%s "
                    "(handled by inbound worker, ignored here)",
                    field, msg.get("type"), _mask(msg.get("from", "")),
                )

            for er in errors:
                logger.info(
                    "APTANA status | field=%s ERROR code=%s title=%s",
                    field, er.get("code"), er.get("title"),
                )

            if extra:
                # The interesting case for the typing/read investigation:
                # an event type we haven't seen before.
                logger.info(
                    "APTANA status | field=%s UNEXPECTED value keys=%s "
                    "(investigate — possible new event type)",
                    field, extra,
                )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _extract_text_from_payload(payload: Any) -> str | None:
    """Pull the message text out of APTANA's `payload` field.

    The exact internal shape isn't documented. We expect APTANA forwards Meta's
    WhatsApp Cloud API webhook structure inside `payload`, possibly as a
    JSON-encoded string. Probe several known shapes before giving up:

    1. Plain string — `payload` IS the text (simplest case if APTANA pre-extracts).
    2. JSON-string of a flat dict — {"text": "..."} or {"body": "..."} etc.
    3. JSON-string of a Meta envelope — {"messages":[{"text":{"body":"..."}}]}
    4. Already-parsed dict — same as (2)/(3) but no json.loads needed.

    Returns the trimmed text, or None if no text-like content is found (likely
    means the inbound was an image / sticker / location etc.).
    """
    if payload is None or payload == "":
        return None

    # If it's a string, decide: is it JSON or already plain text?
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith(("{", "[")):
            try:
                payload = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                # Looked like JSON but didn't parse — treat the whole string as text
                return stripped or None
        else:
            # Pure plain text path (best case — APTANA already extracted)
            return stripped or None

    if not isinstance(payload, dict):
        return None

    # Shape: flat dict with text under one of several common keys
    for key in ("text", "body", "message", "content"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            inner = v.get("body") or v.get("text")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()

    # Shape: Meta WhatsApp Cloud API envelope
    # {"messages": [{"type": "text", "text": {"body": "..."}}], ...}
    msgs = payload.get("messages")
    if isinstance(msgs, list) and msgs:
        first = msgs[0] if isinstance(msgs[0], dict) else None
        if first:
            text_obj = first.get("text")
            if isinstance(text_obj, dict):
                body = text_obj.get("body")
                if isinstance(body, str) and body.strip():
                    return body.strip()
            if isinstance(text_obj, str) and text_obj.strip():
                return text_obj.strip()

    # Shape: full Meta envelope with nested entry/changes/value (Meta Cloud API webhook)
    # {"entry":[{"changes":[{"value":{"messages":[...]}}]}]}
    entries = payload.get("entry")
    if isinstance(entries, list) and entries:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes", []):
                value = change.get("value", {}) if isinstance(change, dict) else {}
                msgs = value.get("messages")
                if isinstance(msgs, list) and msgs:
                    first = msgs[0]
                    if isinstance(first, dict):
                        text_obj = first.get("text")
                        if isinstance(text_obj, dict):
                            body = text_obj.get("body")
                            if isinstance(body, str) and body.strip():
                                return body.strip()

    return None


def _extract_message_id_from_payload(payload: Any) -> str | None:
    """Pull the Meta `message_id` (wamid.xxx) out of APTANA's `payload` field.

    Mirrors the probing logic of `_extract_text_from_payload` — APTANA wraps
    Meta's webhook envelope and we don't know the exact shape, so try the
    common ones. Returns None if no id is found; the acknowledgment falls
    back to plain text in that case.
    """
    if payload is None or payload == "":
        return None

    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith(("{", "[")):
            try:
                payload = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                return None
        else:
            return None  # plain text inbound has no message id

    if not isinstance(payload, dict):
        return None

    # Shape 1: flat dict with id directly under known keys.
    for key in ("id", "message_id", "messageId", "wamid"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # Shape 2: Meta-style {"messages": [{"id": "wamid.xxx", ...}]}
    msgs = payload.get("messages")
    if isinstance(msgs, list) and msgs:
        first = msgs[0] if isinstance(msgs[0], dict) else None
        if first:
            mid = first.get("id") or first.get("message_id")
            if isinstance(mid, str) and mid.strip():
                return mid.strip()

    # Shape 3: full Meta envelope {"entry":[{"changes":[{"value":{"messages":[...]}}]}]}
    entries = payload.get("entry")
    if isinstance(entries, list) and entries:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes", []):
                value = change.get("value", {}) if isinstance(change, dict) else {}
                msgs = value.get("messages")
                if isinstance(msgs, list) and msgs:
                    first = msgs[0]
                    if isinstance(first, dict):
                        mid = first.get("id") or first.get("message_id")
                        if isinstance(mid, str) and mid.strip():
                            return mid.strip()

    return None


def _mask(phone: str) -> str:
    if not phone or len(phone) < 8:
        return "<short>"
    return f"{phone[:4]}…{phone[-4:]}"
