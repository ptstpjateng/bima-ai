"""copilot_session: persistent per-officer-per-case copilot conversations

Revision ID: 002_copilot_session
Revises: 001_baseline
Create Date: 2026-05-21 00:00:00

Notes:
    - New table OWNED by admin-api (Decisions.md §22 — the Officer Copilot
      is per-officer and its sessions persist).
    - One row per (officer_id, ticket) pair, enforced by a unique constraint
      that the `/case/{ticket}/copilot/chat` upsert relies on.
    - `history_json` is JSONB on Postgres (falls back to JSON on sqlite so
      tests still run), holding the full {role, text} transcript.
    - FK on `officer_id` → `users.id` with ON DELETE CASCADE: when an officer
      account is removed, their copilot sessions go with it. `users` is
      Laravel-owned and already exists (see 001_baseline).
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "002_copilot_session"
down_revision: str | None = "001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the copilot_session table admin-api owns."""
    op.create_table(
        "copilot_session",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "officer_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning officer — always users.id from the verified JWT.",
        ),
        sa.Column(
            "ticket",
            sa.String(length=32),
            nullable=False,
            comment="SIAP ticket the conversation is anchored to (zero-padded).",
        ),
        sa.Column(
            "history_json",
            # JSONB on Postgres; JSON on sqlite so unit tests still pass.
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Full conversation transcript: array of {role, text} turns.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # One conversation per officer per ticket — the upsert key.
        sa.UniqueConstraint(
            "officer_id", "ticket", name="uq_copilot_session_officer_ticket"
        ),
    )

    # An officer's "my recent copilot sessions" view will filter by officer_id;
    # ticket lookups also benefit from their own index.
    op.create_index(
        "ix_copilot_session_officer_id",
        "copilot_session",
        ["officer_id"],
    )
    op.create_index(
        "ix_copilot_session_ticket",
        "copilot_session",
        ["ticket"],
    )


def downgrade() -> None:
    """Roll back — drop the new table. Laravel tables are not touched."""
    op.drop_index("ix_copilot_session_ticket", table_name="copilot_session")
    op.drop_index("ix_copilot_session_officer_id", table_name="copilot_session")
    op.drop_table("copilot_session")
