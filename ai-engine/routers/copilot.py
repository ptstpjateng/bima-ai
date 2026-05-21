"""
Officer Copilot HTTP surface.

POST /v1/copilot/chat
  Internal endpoint called by admin-api (`/case/{ticket}/copilot/chat`)
  on behalf of the bima-admin case page. Runs one user-message turn
  through `OfficerCopilot.chat`, executing Gemini function-calling rounds
  as needed, and returns the final Indonesian reply plus a transcript of
  tool calls (for transparency in the admin UI side panel).

Stateless by design:
  This endpoint keeps NO conversation memory. admin-api owns the
  `copilot_session` table (one row per officer+ticket): it loads the
  prior `history`, sends it here, and persists the `history` we return.
  See [[Decisions]] §22 — admin-api owns durability, ai-engine owns the
  agent logic.

`officer_id` is for masked logging + audit only. admin-api derives it
from the authenticated officer's JWT (`get_current_admin_user`) — it is
NEVER a value an end user can set. The copilot does not use it for any
access decision (admin-api already scoped the session before calling us).

`validation` is the BIMA validator result for the ticket, computed by
admin-api and injected so the `get_validation_summary` tool can surface
the score + issues without a second Gemini Vision pass.

Auth: gated by X-Internal-Key — mirrors the validator and tracking
endpoints. The endpoint is *not* meant to be exposed to citizens.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import httpx
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
    officer_id: int | None = Field(
        default=None,
        ge=1,
        description=(
            "ID of the officer this copilot session belongs to. Supplied by "
            "admin-api from the authenticated officer's JWT — used here for "
            "masked logging/audit only, never for an access decision."
        ),
    )
    message: str = Field(..., min_length=1, max_length=_MAX_MESSAGE_CHARS)
    history: list[HistoryTurn] = Field(
        default_factory=list,
        description=(
            "Prior turns in the conversation. The endpoint is stateless, "
            "so the caller (admin-api) is responsible for echoing it back "
            "each turn from the `copilot_session` table."
        ),
    )
    validation: dict[str, Any] | None = Field(
        default=None,
        description=(
            "BIMA validator result for `ticket` (score, score_percent, "
            "status, summary, issues), computed by admin-api. Injected so "
            "the `get_validation_summary` tool can surface it without "
            "re-running Gemini Vision. Omit when no validation exists yet."
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
        "Copilot chat | ticket=%s | officer_id=%s | message_len=%d | "
        "history_turns=%d | has_validation=%s",
        body.ticket,
        body.officer_id if body.officer_id is not None else "<none>",
        len(body.message),
        len(history_payload),
        body.validation is not None,
    )

    # Narrow exception handling per QA finding H4 (2026-05-19) — the prior
    # blanket `except Exception → 502` was hiding real programming bugs as
    # if they were upstream failures, making stage-rehearsal debugging
    # blind. Now we map by failure kind:
    #   503 — config missing (no API key, no model name)
    #   504 — Gemini timed out
    #   502 — Gemini returned a non-2xx or network error
    #   500 — agent threw an unexpected Python exception (real bug)
    try:
        result = await get_copilot().chat(
            message=body.message,
            ticket=body.ticket,
            history=history_payload,
            officer_id=body.officer_id,
            validation=body.validation,
        )
    except httpx.TimeoutException as exc:
        logger.warning("Copilot timeout | ticket=%s | err=%s", body.ticket, exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Copilot upstream (Gemini) timed out.",
        )
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        logger.warning("Copilot upstream error | ticket=%s | err=%s", body.ticket, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Copilot upstream (Gemini) returned an error.",
        )
    except (RuntimeError, ValueError) as exc:
        # OfficerCopilot raises these for predictable config / contract issues
        # (e.g. missing GEMINI_API_KEY surfaced via gemini_vision.is_configured()).
        logger.error("Copilot configuration error | ticket=%s | err=%s", body.ticket, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Copilot agent not ready: {exc}",
        )
    except Exception:
        # Genuine programming bug — surface as 500 so it's not confused with
        # an upstream failure, and emit full traceback for debugging.
        logger.exception("Copilot unexpected error | ticket=%s", body.ticket)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Copilot agent crashed — see server logs.",
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
