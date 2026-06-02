"""
BIMA-AI – Web chat router.

Endpoints:
  POST /webhook/chat         – synchronous web chat from the Next.js portal.
  POST /webhook/chat/stream  – Server-Sent Events (SSE) streaming variant of
                               the same chat, for progressive token rendering.

Note on history: this file used to also house Telegram + Meta direct WhatsApp
webhook handlers. Both were removed when the WhatsApp channel migrated to
APTANA (see routers/aptana.py for the new inbound + status endpoints).
"""

import json
import logging
import time as _time
import uuid

from fastapi import APIRouter, BackgroundTasks, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator

from services.ai_handler import (
    generate_ai_response,
    generate_ai_response_stream,
    log_to_backend,
)
from services.input_sanitizer import sanitize_user_input
from services.prompt_injection_detector import audit_user_input

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
    # Sanitise + audit before any LLM work. ChatRequest already enforces a
    # 2000-char ceiling at the Pydantic layer; the sanitiser layers on
    # control-char / bidi / encoded-blob defences that Pydantic doesn't do.
    sanitized = sanitize_user_input(body.message)
    if not sanitized:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "error", "code": 400, "message": "Pesan kosong setelah pembersihan."},
        )
    audit_user_input(body.user_id, sanitized)

    t0 = _time.monotonic()
    try:
        response = await generate_ai_response(body.user_id, sanitized)
        elapsed = round(_time.monotonic() - t0, 2)
        logger.info(
            "Web chat done | user_id=%s | elapsed=%.2fs | request_id=%s",
            body.user_id, elapsed, request_id,
        )
        background_tasks.add_task(log_to_backend, body.user_id, sanitized, response, "web")
        return JSONResponse({"response": response, "elapsed": elapsed})
    except Exception:
        logger.exception("Web chat failed | user_id=%s | request_id=%s", body.user_id, request_id)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "code": 500, "message": "Gagal memproses pesan. Silakan coba lagi."},
        )


def _sse(payload: dict) -> str:
    """Encode a dict as one SSE message frame.

    Format: `event: <name>\\ndata: <json>\\n\\n`. The `event:` line lets an
    EventSource-style client (or the browser-native EventSource) dispatch on
    event type; the JSON `data:` line carries the payload. Newlines inside the
    JSON are safe because json.dumps escapes them.
    """
    name = payload.get("event", "message")
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/webhook/chat/stream")
async def web_chat_stream(body: ChatRequest, background_tasks: BackgroundTasks) -> StreamingResponse:
    """
    Streaming web chat for the Next.js portal (Server-Sent Events).

    Accepts the SAME `{ user_id, message }` body as `POST /webhook/chat`.
    Responds with `text/event-stream`. Each frame is:

        event: delta
        data: {"event":"delta","text":"<token chunk>"}

        event: done
        data: {"event":"done","text":"<full reply>","elapsed":3.1}

        event: error
        data: {"event":"error","message":"<user-facing message>"}

    The client should append every `delta.text` to render text progressively,
    treat `done` as end-of-stream (its `text` is the authoritative full reply,
    useful for de-dup / final render), and surface `error.message` to the user.

    The fast paths (SIAP ticket lookup, SIAP agent, rate-limit, demo mode) are
    not token-streamed — they arrive as a single `delta` followed by `done`,
    which the client handles transparently.

    The non-streaming `POST /webhook/chat` is unchanged; WhatsApp (aptana.py)
    keeps using `generate_ai_response` and never touches this endpoint.
    """
    request_id = str(uuid.uuid4())
    logger.info(
        "Web chat (stream) | user_id=%s | msg_len=%d | request_id=%s",
        body.user_id, len(body.message), request_id,
    )

    # Sanitise + audit BEFORE entering the SSE generator. If we did it inside
    # event_source(), we'd already have committed to the 200/text-event-stream
    # response headers and would have to ship an error frame instead — fine,
    # but harder to monitor and easier to miss in dashboards. Bouncing here
    # with a 400 surfaces obviously malformed input loud and clear.
    sanitized = sanitize_user_input(body.message)
    if not sanitized:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "error", "code": 400, "message": "Pesan kosong setelah pembersihan."},
        )
    audit_user_input(body.user_id, sanitized)
    body_message = sanitized  # use the cleaned text from here on

    async def event_source():
        t0 = _time.monotonic()
        final_reply = ""
        try:
            async for event in generate_ai_response_stream(body.user_id, body_message):
                if event.get("event") == "done":
                    final_reply = event.get("text", "")
                yield _sse(event)
        except Exception:
            logger.exception(
                "Web chat stream failed | user_id=%s | request_id=%s",
                body.user_id, request_id,
            )
            yield _sse({
                "event": "error",
                "message": "Gagal memproses pesan. Silakan coba lagi.",
            })
        finally:
            elapsed = round(_time.monotonic() - t0, 2)
            logger.info(
                "Web chat stream done | user_id=%s | elapsed=%.2fs | request_id=%s",
                body.user_id, elapsed, request_id,
            )
            # Fire-and-forget backend log, only when we produced a real reply.
            if final_reply:
                background_tasks.add_task(
                    log_to_backend, body.user_id, body_message, final_reply, "web"
                )

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disable proxy buffering (Caddy/nginx) so tokens flush immediately.
            "X-Accel-Buffering": "no",
        },
    )
