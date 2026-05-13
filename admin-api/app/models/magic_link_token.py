"""
`magic_link_tokens` — Laravel-owned. Phase 1 admin-api does not issue or
redeem these (admin login is password+JWT for now); the model exists so
ingestion / future audit endpoints can read history.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MagicLinkToken(Base):
    __tablename__ = "magic_link_tokens"
    __table_args__ = {"keep_existing": True}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    # Note: Laravel currently stores the raw token (backend-laravel.md 🟠).
    # Phase 2 migration moves this to sha256-at-rest.
    token: Mapped[str] = mapped_column(String(64), unique=True)
    channel: Mapped[str] = mapped_column(String(20))  # whatsapp|telegram|web|mobile|telegram_link
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
