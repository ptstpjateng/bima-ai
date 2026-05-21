"""
`copilot_session` — NEW table owned by admin-api.

Per Decisions.md §22: the Officer Copilot is each officer's right-hand man,
scoped to their own cases, and "each officer's copilot session persists."

One row per (officer, ticket) pair. admin-api owns durability — ai-engine's
`/v1/copilot/chat` is stateless. The flow each turn:

  1. admin-api looks up (officer_id, ticket) and loads `history_json`.
  2. It POSTs message + history to ai-engine, which runs the agent and
     returns the updated history.
  3. admin-api writes the new history back to this row and bumps
     `last_active_at`.

SECURITY: `officer_id` is always the authenticated officer's `users.id`
(from the JWT via `get_current_admin_user`). A session is scoped to the
officer who owns it — the `/case/{ticket}/copilot/chat` endpoint filters
on `officer_id == current_user.id`, so officer A can never load officer
B's conversation.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CopilotSession(Base):
    __tablename__ = "copilot_session"
    # One conversation per officer per ticket. The unique constraint is the
    # upsert key — `get_or_create` relies on it.
    __table_args__ = (
        UniqueConstraint(
            "officer_id", "ticket", name="uq_copilot_session_officer_ticket"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # The officer who owns this session. FK to the Laravel-managed `users`
    # table; CASCADE so a deleted officer's sessions are cleaned up.
    officer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # SIAP ticket the conversation is anchored to (zero-padded 9-digit, but
    # stored as VARCHAR so leading zeros survive).
    ticket: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # The full conversation transcript: a JSON array of {role, text} turns,
    # exactly the shape ai-engine's copilot expects and returns. JSONB at the
    # DB level (see the migration); JSON here so sqlite-backed tests still run.
    history_json: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=func.now()
    )
    # Bumped on every turn so a future "recent conversations" view / cleanup
    # job can sort by activity.
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=func.now(),
        onupdate=func.now(),
    )
