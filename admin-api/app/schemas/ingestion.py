"""
Pydantic schemas for the unified ingestion pipeline (Decisions.md §5).

Phase 1 only declares the shapes — the CRUD router lands in Phase 2 once the
worker side is wired.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Whitelist of accepted source kinds. Adding `docx`, `text`, more later is a
# pure code change — no DB enum to ALTER.
IngestionKind = Literal["pdf", "excel_kbli", "excel_pb_umku", "url", "text", "docx"]
IngestionStatus = Literal["pending", "processing", "done", "failed"]


class IngestionSourceCreate(BaseModel):
    """Body for `POST /admin/ingestion-sources` (Phase 2)."""

    kind: IngestionKind
    display_name: str = Field(min_length=1, max_length=255)
    # Required for binary kinds; the router will validate kind ↔ key coupling.
    object_key: str | None = Field(default=None, max_length=500)
    # Free-form per-kind config; the parser picks fields it knows.
    metadata_json: dict = Field(default_factory=dict)


class IngestionSourceOut(BaseModel):
    """List/detail response shape."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: IngestionKind
    display_name: str
    object_key: str | None
    metadata_json: dict
    status: IngestionStatus
    chunks_indexed: int
    error: str | None
    created_by: int | None
    created_at: datetime
    last_run_at: datetime | None
