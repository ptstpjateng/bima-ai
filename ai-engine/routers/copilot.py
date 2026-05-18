"""
Officer Copilot HTTP surface.

POST /v1/copilot/chat
  Internal endpoint called by admin-api (`/case/{ticket}/copilot/chat`,
  to be added in a follow-up) on behalf of the bima-admin case page.
  Runs one user-message turn through `OfficerCopilot.chat`, executing
  Gemini function-calling rounds as needed, and returns the final
  Indonesian reply plus a transcript of tool calls (for transparency
  in the admin UI side panel).

Auth: gated by X-Internal-Key — mirrors the validator and tracking
endpoints. The endpoint is *not* meant to be exposed to citizens.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from deps import require_internal_key
from services.agents.officer_copilot import get_copilot

logger = logging.getLogger("bima_ai.copilot")

router = APIRouter(prefix="/v1/copilot", tags=["Copilot"])


# Conservative guards. The officer UI is internal so abuse risk is low,
# but a runaway message or history would still cost tokens. Numbers picked
# to be comfortable for typical case-analysis sessions (~20 turns, paragraph
# questions).
_MAX_MESSAGE_CHARS = 4000
_MAX_HISTORY_TURNS = 40
_MAX_TURN_CHARS = 4000


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class HistoryTurn(BaseModel):
    role: str = Field(..., description="Either 'user' or 'model'.")
    text: str = Field(..., min_length=1, max_length=_MAX_TURN_CHARS)

    @field_validator("role")
    @classmethod
    def _role_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"user", "model"}:
            raise ValueError("role must be 'user' or 'model'")
        return v


class CopilotChatRequest(BaseModel):
    ticket: str = Field(
        ...,
        min_length=1,
        max_length=32,
        description=(
            "SIAP ticket number the conversation is anchored to "
            "(typically 9-digit zero-padded)."
        ),
    )
    message: str = Field(..., min_length=1, max_length=_MAX_MESSAGE_CHARS)
    history: list[HistoryTurn] = Field(
        default_factory=list,
        description=(
            "Prior turns in the conversation. The endpoint is stateless, "
            "so the caller is responsible for echoing it back each turn."
        ),
    )

    @field_validator("history")
    @classmethod
    def _history_within_cap(cls, v: list[HistoryTurn]) -> list[HistoryTurn]:
        if len(v) > _MAX_HISTORY_TURNS:
            raise ValueError(
                f"history exceeds {_MAX_HISTORY_TURNS} turns — "
                "trim older turns client-side before retrying."
            )
        return v


class ToolCallOut(BaseModel):
    name: str
    args: dict[str, Any]
    result_preview: str


class HistoryTurnOut(BaseModel):
    role: str
    text: str


class CopilotChatResponse(BaseModel):
    reply: str
    tool_calls: list[ToolCallOut]
    history: list[HistoryTurnOut]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/chat",
    response_model=CopilotChatResponse,
    summary="Run one Officer Copilot turn (Gemini function-calling agent)",
)
async def copilot_chat_endpoint(
    body: CopilotChatRequest,
    _internal: Annotated[bool, Depends(require_internal_key)],
) -> CopilotChatResponse:
    history_payload = [{"role": t.role, "text": t.text} for t in body.history]

    logger.info(
        "Copilot chat | ticket=%s | message_len=%d | history_turns=%d",
        body.ticket, len(body.message), len(history_payload),
    )

    try:
        result = await get_copilot().chat(
            message=body.message,
            ticket=body.ticket,
            history=history_payload,
        )
    except Exception:
        logger.exception("Copilot chat raised | ticket=%s", body.ticket)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Copilot agent failed — see server logs.",
        )

    return CopilotChatResponse(
        reply=result["reply"],
        tool_calls=[
            ToolCallOut(
                name=tc["name"],
                args=tc.get("args") or {},
                result_preview=tc.get("result_preview", ""),
            )
            for tc in result.get("tool_calls", [])
        ],
        history=[
            HistoryTurnOut(role=h["role"], text=h["text"])
            for h in result.get("history", [])
        ],
    )
