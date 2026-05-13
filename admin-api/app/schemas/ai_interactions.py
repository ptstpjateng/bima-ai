"""
Pydantic schemas for the AI Interactions admin views.

Two surfaces:

* List view (`AiInteractionItem`) — content truncated to 300 chars so the table
  renders cheaply and the wire payload stays small.
* Thread view (`AiInteractionTurn`) — full content, ordered by turn_index
  ascending, plus session-level metadata (channel, started_at, ...).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AiInteractionItem(BaseModel):
    """List-row shape. `content` is truncated upstream to 300 chars."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: str
    turn_index: int
    channel: str
    message_type: str
    content: str
    intent: str | None
    user_id: int | None
    response_time_ms: int | None
    created_at: datetime


class AiInteractionListResponse(BaseModel):
    items: list[AiInteractionItem]
    total: int
    page: int
    limit: int


class AiInteractionTurn(BaseModel):
    """Single turn inside a thread. Full content (no truncation)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    turn_index: int
    message_type: str
    content: str
    intent: str | None
    response_time_ms: int | None
    created_at: datetime


class AiInteractionThreadResponse(BaseModel):
    session_id: str
    channel: str
    user_id: int | None
    turn_count: int
    started_at: datetime
    last_turn_at: datetime
    turns: list[AiInteractionTurn]
