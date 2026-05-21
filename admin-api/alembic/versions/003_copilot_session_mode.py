"""copilot_session.mode: separate transcripts for the signature assistant

Revision ID: 003_copilot_session_mode
Revises: 002_copilot_session
Create Date: 2026-05-21 12:00:00

Notes:
    - Wave 4 / Vision req #13: the officer copilot gains a sibling — the
      Head-of-DPMPTSP signature assistant. Both run against the same
      `copilot_session` table, so a `mode` column distinguishes them.
    - The old uniqueness key `(officer_id, ticket)` is replaced by
      `(officer_id, ticket, mode)` so an officer's validation conversation
      and the Head's signing conversation on the same ticket keep separate
      transcripts.
    - Existing rows are all the officer copilot, so `mode` backfills to
      'officer' via the NOT NULL server_default.
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003_copilot_session_mode"
down_revision: str | None = "002_copilot_session"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add `mode` and widen the uniqueness key to include it."""
    op.add_column(
        "copilot_session",
        sa.Column(
            "mode",
            sa.String(length=16),
            nullable=False,
            server_default="officer",
            comment="Copilot variant: 'officer' or 'signature' (Vision #13).",
        ),
    )
    # Swap the uniqueness key: (officer_id, ticket) → (officer_id, ticket, mode).
    op.drop_constraint(
        "uq_copilot_session_officer_ticket",
        "copilot_session",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_copilot_session_officer_ticket_mode",
        "copilot_session",
        ["officer_id", "ticket", "mode"],
    )


def downgrade() -> None:
    """Roll back — restore the old key and drop `mode`.

    Any 'signature'-mode rows would collide with their 'officer' siblings on
    the old (officer_id, ticket) key, so they are removed first.
    """
    op.execute("DELETE FROM copilot_session WHERE mode = 'signature'")
    op.drop_constraint(
        "uq_copilot_session_officer_ticket_mode",
        "copilot_session",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_copilot_session_officer_ticket",
        "copilot_session",
        ["officer_id", "ticket"],
    )
    op.drop_column("copilot_session", "mode")
