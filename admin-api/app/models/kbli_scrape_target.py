"""
`kbli_scrape_targets` — Laravel-owned. Note: `scraped_content TEXT` was added
manually in production without a Laravel migration (backend-laravel.md
🔴 schema-drift issue). We include it here so SQLAlchemy queries don't blow
up — the formal migration to capture it lives outside admin-api's scope.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class KbliScrapeTarget(Base):
    __tablename__ = "kbli_scrape_targets"
    __table_args__ = {"keep_existing": True}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kbli_code: Mapped[str] = mapped_column(String(10), unique=True)
    kbli_description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    priority: Mapped[int] = mapped_column(SmallInteger, default=5)
    last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scrape_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chroma_chunks: Mapped[int] = mapped_column(Integer, default=0)
    scrape_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Manually-added column. See file docstring.
    scraped_content: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
