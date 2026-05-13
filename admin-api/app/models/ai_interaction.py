"""
`ai_interactions` — Laravel-owned (Phase 1). Schema mirrors
backend-laravel.md table 2.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AiInteraction(Base):
    __tablename__ = "ai_interactions"
    __table_args__ = {"keep_existing": True}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Both FKs are nullable + SET NULL on delete (Laravel migration semantics).
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    permit_application_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("permit_applications.id", ondelete="SET NULL"),
        nullable=True,
    )

    session_id: Mapped[str] = mapped_column(String(64), index=True)
    turn_index: Mapped[int] = mapped_column(BigInteger)

    # Enums on the Laravel side — kept as VARCHAR here to avoid coupling to a
    # specific Postgres enum type that Alembic might try to manage.
    channel: Mapped[str] = mapped_column(String(20))  # whatsapp|telegram|web|mobile|internal
    message_type: Mapped[str] = mapped_column(String(20))  # user_message|ai_response|system_event

    content: Mapped[str] = mapped_column(Text)
    content_rendered: Mapped[str | None] = mapped_column(Text, nullable=True)
    intent: Mapped[str | None] = mapped_column(String(100), nullable=True)
    entities: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    context_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(80), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    external_message_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    is_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
