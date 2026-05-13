"""
`ingestion_sources` — NEW table owned by admin-api.

Per Decisions.md §5 (unified ingestion pipeline). This is the only table
admin-api creates; everything else is Laravel-owned and reflected.

The actual ingest workers (Sprint C) will read these rows, run the per-kind
parser, and write canonical chunks back to ChromaDB tagged with `source_id`.
The admin UI reads `status` + `chunks_indexed` for live progress.
"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class IngestionSource(Base):
    __tablename__ = "ingestion_sources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Discriminator. Allowed values per Decisions.md §5:
    # pdf | excel_kbli | excel_pb_umku | url | text | docx
    # Stored as a string (not a Postgres ENUM) so adding a new kind is a
    # one-line code change rather than a destructive ALTER TYPE.
    kind: Mapped[str] = mapped_column(String(20))

    display_name: Mapped[str] = mapped_column(String(255))

    # MinIO object key for binary kinds (pdf, excel_*, docx). Null for url/text.
    object_key: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Per-kind config. Examples:
    #   excel_kbli  -> {"sheet_name": "KBLI", "chunk_size": 800}
    #   url         -> {"crawl_depth": 2, "include_subdomains": false}
    #   pdf         -> {"page_range": [1, 50]}
    # Stored as JSON (Postgres JSONB at the DB level — see migration).
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")

    # pending | processing | done | failed
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    chunks_indexed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=func.now()
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
