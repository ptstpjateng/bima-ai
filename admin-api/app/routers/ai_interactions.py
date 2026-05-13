"""
AI Interactions router — Sprint B.2.

Two read-only endpoints:

    GET /ai-interactions                       — paginated list with filters
    GET /ai-interactions/sessions/{session_id} — full thread for one session

The list view truncates `content` to 300 chars (table cell rendering); the
thread view returns the full text per turn.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_admin_user, get_db
from app.models import AiInteraction
from app.schemas.ai_interactions import (
    AiInteractionItem,
    AiInteractionListResponse,
    AiInteractionThreadResponse,
    AiInteractionTurn,
)

router = APIRouter()

# Truncation cap for list-view content. Long enough to be useful in a table
# row preview, short enough to keep the wire payload tight on a 50-row page.
_LIST_CONTENT_CAP = 300


def _truncate(text: str, cap: int = _LIST_CONTENT_CAP) -> str:
    """Single-byte safe truncation w/ ellipsis if we trim."""
    if text is None:
        return ""
    if len(text) <= cap:
        return text
    # Prefer to leave room for the marker so the displayed length stays ≤ cap.
    return text[: cap - 1] + "…"


@router.get(
    "",
    response_model=AiInteractionListResponse,
    summary="List AI interactions with filters + pagination.",
)
async def list_ai_interactions(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user=Depends(get_current_admin_user),
    channel: str | None = Query(default=None, description="Exact channel match."),
    message_type: str | None = Query(
        default=None,
        description="user_message | ai_response | system_event",
    ),
    since: datetime | None = Query(
        default=None,
        description="ISO 8601 timestamp; filter created_at >= since.",
    ),
    q: str | None = Query(
        default=None,
        description="Case-insensitive ILIKE on `content`.",
        min_length=1,
        max_length=200,
    ),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> AiInteractionListResponse:
    """
    Most-recent-first list of AI interactions.

    Pagination is offset-based (page/limit) — the dataset is bounded enough
    that cursor pagination would be overkill for the admin UI. Switch to
    keyset if the table ever grows past a few million rows.
    """
    # ---- Build the WHERE conjunction once and reuse for count + page ----
    filters = []
    if channel is not None:
        filters.append(AiInteraction.channel == channel)
    if message_type is not None:
        filters.append(AiInteraction.message_type == message_type)
    if since is not None:
        filters.append(AiInteraction.created_at >= since)
    if q is not None:
        # ILIKE — Postgres-native case-insensitive match. Escape `%` and `_`
        # so a user-supplied wildcard doesn't widen the search.
        safe = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append(AiInteraction.content.ilike(f"%{safe}%"))

    # ---- Total count (filters applied, no offset/limit) ------------------
    count_stmt = select(func.count(AiInteraction.id))
    if filters:
        count_stmt = count_stmt.where(*filters)
    total = int((await db.execute(count_stmt)).scalar_one() or 0)

    # ---- Page of rows ----------------------------------------------------
    offset = (page - 1) * limit
    rows_stmt = (
        select(AiInteraction)
        .order_by(AiInteraction.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if filters:
        rows_stmt = rows_stmt.where(*filters)

    rows_result = await db.execute(rows_stmt)
    rows = rows_result.scalars().all()

    items = [
        AiInteractionItem(
            id=r.id,
            session_id=r.session_id,
            turn_index=r.turn_index,
            channel=r.channel,
            message_type=r.message_type,
            content=_truncate(r.content),
            intent=r.intent,
            user_id=r.user_id,
            response_time_ms=r.response_time_ms,
            created_at=r.created_at,
        )
        for r in rows
    ]

    return AiInteractionListResponse(
        items=items,
        total=total,
        page=page,
        limit=limit,
    )


@router.get(
    "/sessions/{session_id}",
    response_model=AiInteractionThreadResponse,
    summary="Full conversation thread for a session_id.",
)
async def get_session_thread(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user=Depends(get_current_admin_user),
) -> AiInteractionThreadResponse:
    """
    Returns every turn in `session_id`, ordered by `turn_index` ascending.

    404 when no rows exist for the session_id — distinguishes "empty session"
    (which can't happen — there's always a first turn) from "wrong id".
    """
    stmt = (
        select(AiInteraction)
        .where(AiInteraction.session_id == session_id)
        .order_by(AiInteraction.turn_index.asc())
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No AI interactions for session_id={session_id}.",
        )

    first = rows[0]
    last = rows[-1]

    turns = [
        AiInteractionTurn(
            id=r.id,
            turn_index=r.turn_index,
            message_type=r.message_type,
            content=r.content,
            intent=r.intent,
            response_time_ms=r.response_time_ms,
            created_at=r.created_at,
        )
        for r in rows
    ]

    return AiInteractionThreadResponse(
        session_id=session_id,
        # `channel` is per-row in the DB but invariant within a session in
        # practice. Take the first turn's channel as the canonical value.
        channel=first.channel,
        user_id=first.user_id,
        turn_count=len(rows),
        started_at=first.created_at,
        last_turn_at=last.created_at,
        turns=turns,
    )
