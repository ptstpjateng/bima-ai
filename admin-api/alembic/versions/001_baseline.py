"""baseline: create ingestion_sources; Laravel tables are already-applied

Revision ID: 001_baseline
Revises:
Create Date: 2026-05-13 00:00:00

Notes:
    - admin-api shares the Postgres `bima_ai` database with Laravel/Filament.
    - Every Laravel-managed table (`users`, `ai_interactions`, `kblis`,
      `kbli_scrape_targets`, `magic_link_tokens`, `businesses`,
      `permit_applications`, …) already exists in the live DB courtesy of
      Laravel's own migration system, which writes to its `migrations` table.
    - This Alembic baseline does NOT recreate those — `alembic/env.py` filters
      them out of autogenerate, and we explicitly DO NOT add CREATE TABLE
      statements for them here.
    - The ONLY DDL this migration emits is the new `ingestion_sources` table
      (Decisions.md §5).
    - If admin-api ever runs on a fresh DB without Laravel having seeded it,
      this migration will succeed but Laravel-dependent queries will fail at
      runtime. That's acceptable for Sprint B because the cutover plan
      (Decisions.md §6) keeps Laravel writing to the same DB during Phase 1.
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the only table admin-api owns: ingestion_sources."""
    op.create_table(
        "ingestion_sources",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.String(length=20),
            nullable=False,
            comment="pdf | excel_kbli | excel_pb_umku | url | text | docx",
        ),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column(
            "object_key",
            sa.String(length=500),
            nullable=True,
            comment="MinIO key for binary kinds; null for url/text.",
        ),
        sa.Column(
            "metadata_json",
            # JSONB on Postgres; falls back to JSON on other backends so
            # tests against sqlite still work.
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
            comment="pending | processing | done | failed",
        ),
        sa.Column(
            "chunks_indexed",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Indexes for the admin dashboard filters we'll need in Phase 2.
    op.create_index(
        "ix_ingestion_sources_status",
        "ingestion_sources",
        ["status"],
    )
    op.create_index(
        "ix_ingestion_sources_kind",
        "ingestion_sources",
        ["kind"],
    )
    op.create_index(
        "ix_ingestion_sources_created_at",
        "ingestion_sources",
        ["created_at"],
    )


def downgrade() -> None:
    """Roll back — drop the new table. Laravel tables are not touched."""
    op.drop_index("ix_ingestion_sources_created_at", table_name="ingestion_sources")
    op.drop_index("ix_ingestion_sources_kind", table_name="ingestion_sources")
    op.drop_index("ix_ingestion_sources_status", table_name="ingestion_sources")
    op.drop_table("ingestion_sources")
