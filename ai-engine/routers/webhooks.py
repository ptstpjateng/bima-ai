"""
BIMA-AI – Web chat router.

Single endpoint:
  POST /webhook/chat – synchronous web chat from the Next.js portal.

Note on history: this file used to also house Telegram + Meta direct WhatsApp
webhook handlers. Both were removed when the WhatsApp channel migrated to
APTANA (see routers/aptana.py for the new inbound + status endpoints).
"""

import logging
import time as _time
import uuid

from fastapi import APIRouter, BackgroundTasks, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from services.ai_handler import generate_ai_response, log_to_backend

logger = logging.getLogger("bima_ai.webhooks")

router = APIRouter()


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
async def web_chat(body: ChatRequest, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Synchronous web chat for the Next.js portal.
    Accepts { user_id, message } and returns { response, elapsed }.
    user_id should be prefixed with "web-" to keep history separate from
    APTANA WhatsApp sessions.
    """
    request_id = str(uuid.uuid4())
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
        background_tasks.add_task(log_to_backend, body.user_id, body.message, response, "web")
        return JSONResponse({"response": response, "elapsed": elapsed})
    except Exception:
        logger.exception("Web chat failed | user_id=%s | request_id=%s", body.user_id, request_id)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "code": 500, "message": "Gagal memproses pesan. Silakan coba lagi."},
        )
