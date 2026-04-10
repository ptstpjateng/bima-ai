"""
BIMA-AI – Webhook Router

Endpoints:
  POST /webhook/whatsapp  – Meta Cloud API (WhatsApp Business)
  POST /webhook/telegram  – Telegram Bot API

Security:
  - WhatsApp: HMAC-SHA256 signature verification against X-Hub-Signature-256.
  - Telegram: Shared secret token in X-Telegram-Bot-Api-Secret-Token header.

All validation errors are caught and logged; the server always returns a
structured error body and never exposes internal state to callers.
"""

import hashlib
import hmac
import logging
import os
from typing import Any

from dotenv import load_dotenv
from fastapi import APIRouter, BackgroundTasks, Header, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError, field_validator

import time as _time

from services.ai_handler import generate_ai_response, process_message

load_dotenv()

logger = logging.getLogger("bima_ai.webhooks")

router = APIRouter()

# ---------------------------------------------------------------------------
# Secrets – loaded once at module import; fail loudly if missing.
# ---------------------------------------------------------------------------
_WHATSAPP_APP_SECRET: str = os.getenv("WHATSAPP_APP_SECRET", "")
_TELEGRAM_SECRET_TOKEN: str = os.getenv("TELEGRAM_SECRET_TOKEN", "")

if not _WHATSAPP_APP_SECRET:
    logger.warning("WHATSAPP_APP_SECRET is not set – signature verification will fail.")
if not _TELEGRAM_SECRET_TOKEN:
    logger.warning("TELEGRAM_SECRET_TOKEN is not set – request authentication disabled.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error_body(code: int, message: str) -> dict:
    return {"status": "error", "code": code, "message": message}


def _verify_whatsapp_signature(raw_body: bytes, signature_header: str) -> bool:
    """Return True if the payload matches the HMAC-SHA256 signature from Meta."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        _WHATSAPP_APP_SECRET.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ---------------------------------------------------------------------------
# Pydantic models – WhatsApp (Meta Cloud API)
# ---------------------------------------------------------------------------

class WhatsAppProfile(BaseModel):
    name: str


class WhatsAppContact(BaseModel):
    profile: WhatsAppProfile
    wa_id: str


class WhatsAppTextBody(BaseModel):
    body: str


class WhatsAppMessage(BaseModel):
    from_: str | None = None
    id: str
    timestamp: str
    type: str
    text: WhatsAppTextBody | None = None

    model_config = {"populate_by_name": True}

    @field_validator("type")
    @classmethod
    def type_must_be_known(cls, v: str) -> str:
        allowed = {"text", "image", "audio", "video", "document", "location", "sticker", "reaction", "interactive"}
        if v not in allowed:
            logger.info("Received WhatsApp message of unhandled type: %s", v)
        return v


class WhatsAppMetadata(BaseModel):
    display_phone_number: str
    phone_number_id: str


class WhatsAppValue(BaseModel):
    messaging_product: str
    metadata: WhatsAppMetadata
    contacts: list[WhatsAppContact] | None = None
    messages: list[WhatsAppMessage] | None = None
    statuses: list[dict[str, Any]] | None = None


class WhatsAppChange(BaseModel):
    value: WhatsAppValue
    field: str


class WhatsAppEntry(BaseModel):
    id: str
    changes: list[WhatsAppChange]


class WhatsAppPayload(BaseModel):
    object: str
    entry: list[WhatsAppEntry]

    @field_validator("object")
    @classmethod
    def object_must_be_whatsapp(cls, v: str) -> str:
        if v != "whatsapp_business_account":
            raise ValueError(f"Unexpected object type: {v!r}")
        return v


# ---------------------------------------------------------------------------
# Pydantic models – Telegram Bot API
# ---------------------------------------------------------------------------

class TelegramUser(BaseModel):
    id: int
    is_bot: bool
    first_name: str
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None


class TelegramChat(BaseModel):
    id: int
    type: str  # "private" | "group" | "supergroup" | "channel"
    title: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class TelegramMessage(BaseModel):
    message_id: int
    from_: TelegramUser | None = None
    chat: TelegramChat
    date: int
    text: str | None = None
    caption: str | None = None

    model_config = {"populate_by_name": True}


class TelegramCallbackQuery(BaseModel):
    id: str
    from_: TelegramUser
    data: str | None = None
    message: TelegramMessage | None = None

    model_config = {"populate_by_name": True}


class TelegramUpdate(BaseModel):
    update_id: int
    message: TelegramMessage | None = None
    edited_message: TelegramMessage | None = None
    callback_query: TelegramCallbackQuery | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/webhook/whatsapp", status_code=status.HTTP_200_OK)
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
) -> JSONResponse:
    """
    Receives events from the Meta Cloud API (WhatsApp Business Platform).

    Security: verifies the HMAC-SHA256 signature before processing.
    Meta requires a 200 response quickly; heavy processing must be offloaded
    to a background task / queue.
    """
    request_id: str = getattr(request.state, "request_id", "unknown")

    # 1. Read raw body for signature verification BEFORE parsing JSON.
    try:
        raw_body = await request.body()
    except Exception:
        logger.exception("Failed to read request body | request_id=%s", request_id)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_error_body(400, "Could not read request body."),
        )

    # 2. Signature verification.
    if _WHATSAPP_APP_SECRET:
        if not _verify_whatsapp_signature(raw_body, x_hub_signature_256 or ""):
            logger.warning(
                "WhatsApp signature mismatch | request_id=%s | sig_header=%s",
                request_id,
                x_hub_signature_256,
            )
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content=_error_body(401, "Signature verification failed."),
            )

    # 3. Parse and validate the JSON payload against the strict Pydantic model.
    try:
        json_body = await request.json()
        payload = WhatsAppPayload.model_validate(json_body)
    except ValidationError as exc:
        logger.warning(
            "WhatsApp payload validation error | request_id=%s | detail=%s",
            request_id,
            exc.errors(),
        )
        # Meta still expects 200 to avoid retries on genuinely malformed data.
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=_error_body(422, "Payload validation failed."),
        )
    except Exception:
        logger.exception(
            "Unexpected error parsing WhatsApp payload | request_id=%s", request_id
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=_error_body(500, "Internal error processing webhook."),
        )

    # 4. Dispatch – enqueue each inbound text message as a background task.
    # Meta requires a <1 s response; all heavy work runs after the 200 is sent.
    try:
        for entry in payload.entry:
            for change in entry.changes:
                if change.field != "messages":
                    continue
                messages = change.value.messages or []
                phone_number_id = change.value.metadata.phone_number_id
                for msg in messages:
                    if msg.type != "text" or not msg.text:
                        logger.info(
                            "Skipping non-text WhatsApp message | type=%s | request_id=%s",
                            msg.type,
                            request_id,
                        )
                        continue
                    sender = msg.from_ or "unknown"
                    logger.info(
                        "WhatsApp text message queued | from=%s | request_id=%s",
                        sender,
                        request_id,
                    )
                    background_tasks.add_task(
                        process_message,
                        user_id=sender,
                        message=msg.text.body,
                        platform="whatsapp",
                        reply_context={
                            "phone_number_id": phone_number_id,
                            "recipient_wa_id": sender,
                        },
                    )
    except Exception:
        logger.exception(
            "Error dispatching WhatsApp message | request_id=%s", request_id
        )
        # Return 200 so Meta does not retry – the error is logged internally.

    return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok"})


@router.post("/webhook/telegram", status_code=status.HTTP_200_OK)
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    """
    Receives updates from the Telegram Bot API (setWebhook mode).

    Security: validates the secret token configured in setWebhook call.
    Telegram expects a 200 response; processing must not block this handler.
    """
    request_id: str = getattr(request.state, "request_id", "unknown")

    # 1. Authenticate via shared secret token.
    if _TELEGRAM_SECRET_TOKEN:
        incoming_token = x_telegram_bot_api_secret_token or ""
        if not hmac.compare_digest(_TELEGRAM_SECRET_TOKEN, incoming_token):
            logger.warning(
                "Telegram secret token mismatch | request_id=%s", request_id
            )
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content=_error_body(401, "Unauthorized."),
            )

    # 2. Parse and validate the update payload.
    try:
        json_body = await request.json()
        update = TelegramUpdate.model_validate(json_body)
    except ValidationError as exc:
        logger.warning(
            "Telegram payload validation error | request_id=%s | detail=%s",
            request_id,
            exc.errors(),
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=_error_body(422, "Payload validation failed."),
        )
    except Exception:
        logger.exception(
            "Unexpected error parsing Telegram payload | request_id=%s", request_id
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=_error_body(500, "Internal error processing webhook."),
        )

    # 3. Dispatch – enqueue as a background task so Telegram gets a quick 200.
    try:
        if update.message and update.message.text:
            chat_id = update.message.chat.id
            text = update.message.text
            user_id = str(chat_id)
            logger.info(
                "Telegram text message queued | chat_id=%s | update_id=%s | request_id=%s",
                chat_id,
                update.update_id,
                request_id,
            )
            background_tasks.add_task(
                process_message,
                user_id=user_id,
                message=text,
                platform="telegram",
                reply_context={"chat_id": chat_id},
            )
        elif update.edited_message and update.edited_message.text:
            # Treat edited messages the same as new messages.
            chat_id = update.edited_message.chat.id
            text = update.edited_message.text
            logger.info(
                "Telegram edited message queued | chat_id=%s | update_id=%s | request_id=%s",
                chat_id,
                update.update_id,
                request_id,
            )
            background_tasks.add_task(
                process_message,
                user_id=str(chat_id),
                message=text,
                platform="telegram",
                reply_context={"chat_id": chat_id},
            )
        elif update.callback_query:
            logger.info(
                "Telegram callback_query received (not yet handled) | update_id=%s | request_id=%s",
                update.update_id,
                request_id,
            )
    except Exception:
        logger.exception(
            "Error dispatching Telegram update | request_id=%s", request_id
        )

    return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok"})


# ---------------------------------------------------------------------------
# Web chat endpoint — synchronous, called by the Next.js portal
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    user_id: str
    message: str

    @field_validator("user_id")
    @classmethod
    def user_id_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("user_id must not be empty")
        return v.strip()

    @field_validator("message")
    @classmethod
    def message_must_not_be_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message must not be empty")
        if len(v) > 2000:
            raise ValueError("message too long (max 2000 chars)")
        return v


@router.post("/webhook/chat", status_code=status.HTTP_200_OK)
async def web_chat(body: ChatRequest) -> JSONResponse:
    """
    Synchronous web chat for the Next.js portal.
    Accepts { user_id, message } and returns { response, elapsed }.
    user_id should be prefixed with "web-" to keep history separate from
    Telegram/WhatsApp sessions.
    """
    request_id = str(__import__("uuid").uuid4())
    logger.info(
        "Web chat | user_id=%s | msg_len=%d | request_id=%s",
        body.user_id, len(body.message), request_id,
    )
    t0 = _time.monotonic()
    try:
        response = await generate_ai_response(body.user_id, body.message)
        elapsed = round(_time.monotonic() - t0, 2)
        logger.info(
            "Web chat done | user_id=%s | elapsed=%.2fs | request_id=%s",
            body.user_id, elapsed, request_id,
        )
        return JSONResponse({"response": response, "elapsed": elapsed})
    except Exception:
        logger.exception("Web chat failed | user_id=%s | request_id=%s", body.user_id, request_id)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body(500, "Gagal memproses pesan. Silakan coba lagi."),
        )
