"""
Pydantic schemas for the officer inbox (Wave 4 — multi-ticket officer inbox).

`InboxCase` is one urgency-ranked row; `InboxResponse` wraps the sorted list
plus light metadata so the frontend can render a header without a second call.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class InboxCase(BaseModel):
    """One officer-inbox row — a single live SIAP case, urgency-scored."""

    ticket: str = Field(..., description="Zero-padded 9-digit SIAP ticket.")
    license_name: Optional[str] = Field(None, description="Human-readable permit name.")
    sector_name: Optional[str] = Field(None, description="SIAP `nama_bidang`.")
    applicant_name: Optional[str] = Field(None, description="Title-cased applicant name.")
    current_desk: Optional[str] = Field(
        None, description="`bloksistem_nama` — the desk currently holding the file."
    )
    status: Optional[str] = Field(None, description="Raw SIAP `status_permohonan`.")
    submitted_at: Optional[str] = Field(None, description="ISO-8601 submission timestamp.")

    days_open: int = Field(
        ..., ge=0, description="Whole calendar days since the case was submitted."
    )
    sop_days: Optional[int] = Field(
        None,
        ge=0,
        description=(
            "Statutory SLA (jangka waktu) in days, parsed from the licence's "
            "`time_period`. None when the licence/SLA could not be resolved."
        ),
    )

    urgency: float = Field(
        ...,
        ge=0.0,
        description=(
            "Inbox-ranker score in [0, ~1.5]. Rises with days-open relative to "
            "SOP days — a case at 12/14 days outranks one at 2/14. The list is "
            "sorted by this value, descending."
        ),
    )
    sop_state: str = Field(
        ...,
        description=(
            "Coarse SOP bucket for the UI: 'on_track' | 'approaching' | "
            "'over' | 'unknown'. Drives the calm progress indicator's colour."
        ),
    )


class InboxResponse(BaseModel):
    """The full officer inbox — urgency-sorted cases plus a tiny summary."""

    cases: list[InboxCase] = Field(
        default_factory=list,
        description="Active cases, sorted by `urgency` descending.",
    )
    total: int = Field(..., ge=0, description="Number of cases returned.")
    desk: Optional[str] = Field(
        None, description="The desk filter applied, if any (echoes the query param)."
    )
