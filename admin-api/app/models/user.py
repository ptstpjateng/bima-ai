"""
`users` — Laravel-owned table. Reflect the existing schema; do NOT add
columns here without a coordinated Laravel-side migration first.

Source of truth for column types: `BIMA-Vault/backend-laravel.md` §"Migration
Inventory" table 2.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"
    # Laravel migrations own this table — Alembic must not autogenerate
    # diffs against it. The include_object hook in env.py enforces that;
    # this kw guards local Base.create_all() too.
    __table_args__ = {"keep_existing": True}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Laravel uses `'password' => 'hashed'` cast (bcrypt). passlib's bcrypt
    # context is wire-compatible — see app/routers/auth.py.
    password: Mapped[str] = mapped_column(String(255))
    remember_token: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # 'msme' | 'dpmptsp_staff' | 'admin' — enforced by Postgres enum on the
    # Laravel side. We only allow login for the latter two (see auth router).
    role: Mapped[str] = mapped_column(String(20), default="msme")

    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # PII — Decisions.md §4 mandates these be encrypted at rest before prod.
    # Phase 1 reads them as-is; Phase 2/3 introduces the encryption seam.
    nik: Mapped[str | None] = mapped_column(String(16), unique=True, nullable=True)
    npwp: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)

    business_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    business_legal_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    business_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    province: Mapped[str | None] = mapped_column(String(100), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    district: Mapped[str | None] = mapped_column(String(100), nullable=True)
    village: Mapped[str | None] = mapped_column(String(100), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    profile_photo_path: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Telegram is being deprecated in favour of WhatsApp/APTANA per
    # Decisions.md §2 — column kept for migration window. WhatsApp column
    # gets added in a later Alembic revision once Sprint C lands.
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(String(100), nullable=True)

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
