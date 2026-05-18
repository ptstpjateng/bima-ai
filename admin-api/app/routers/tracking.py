"""
Portal-facing permit tracking.

A citizen visits `portal.nolongin.com/track/{ticket}` to check the status of
their license application. The portal renders server-side (Next.js Server
Component) and fetches from this admin-api endpoint, which in turn calls SIAP
Jateng's read-only Sanctum API. The browser never sees the SIAP bearer.

Auth: gated by X-Internal-Key (same shared secret as ai-engine and the legacy
Laravel backend). Portal SSR knows the key; the browser does not. This stops
casual scraping of the endpoint while keeping the wire simple.

Rate limiting: not implemented at the application layer — Caddy in front of
admin-api enforces it. If we ever expose this without Caddy in front, wrap
with `slowapi` or similar.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field

from app.deps import require_internal_key
from app.services.siap_client import get_siap_tracking_client

logger = logging.getLogger("bima_admin_api.tracking")

router = APIRouter()

# Ticket is 4-9 digits; we accept un-padded input and zero-pad inside.
_TICKET_PATTERN = re.compile(r"^\d{4,9}$")


class TrackingResponse(BaseModel):
    """Shape returned to the portal. Field names match BIMA conventions (snake_case)."""

    ticket: str = Field(..., description="Zero-padded 9-digit ticket number.")
    license_name: str = Field(..., description="Human-readable permit name.")
    sector_name: Optional[str] = Field(None, description="Bidang/sektor.")
    applicant_name: str = Field(..., description="Title-cased applicant name.")
    current_desk: str = Field(..., description="Where the file is right now (e.g. 'Front Office PTSP').")
    status: str = Field(..., description="Status enum from SIAP (e.g. 'VERIFIKASI').")
    submitted_at: Optional[str] = Field(
        None,
        description="ISO 8601 submission timestamp as returned by SIAP.",
    )


def _to_response(record: dict, ticket: str) -> TrackingResponse:
    """Map SIAP's snake_case-ish field names to our public response shape."""
    return TrackingResponse(
        ticket=record.get("no_tiket", ticket),
        license_name=record.get("nama_perizinan", "—"),
        sector_name=record.get("nama_bidang"),
        applicant_name=(record.get("nama_pemohon") or "").title(),
        current_desk=record.get("posisi_berkas", "—"),
        status=record.get("status_permohonan", "—"),
        submitted_at=record.get("tanggal_permohonan"),
    )


@router.get(
    "/{ticket}",
    response_model=TrackingResponse,
    responses={
        404: {"description": "No permit application matches the given ticket."},
        503: {"description": "SIAP integration not configured or upstream is down."},
    },
    summary="Look up permit status by ticket (portal-facing)",
)
async def get_tracking(
    ticket: Annotated[str, Path(min_length=4, max_length=9, pattern=r"^\d{4,9}$")],
    _internal: Annotated[bool, Depends(require_internal_key)],
) -> TrackingResponse:
    """
    Return the current status of one permit application.

    Used by the portal SSR page `portal.nolongin.com/track/{ticket}` to render
    a public-facing tracking view. Requires `X-Internal-Key` header — only the
    portal's server side knows it; the browser does not.
    """
    if not _TICKET_PATTERN.match(ticket):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ticket must be 4-9 digits.",
        )

    padded_ticket = ticket.zfill(9)
    client = get_siap_tracking_client()

    if not client.is_configured():
        logger.warning("Tracking request for %s but SIAP client not configured", padded_ticket)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SIAP integration is not configured on this environment.",
        )

    record = await client.get_status_by_ticket(padded_ticket)
    if record is None:
        # Could be: real not-found, or SIAP-down. We return 404 either way —
        # the portal SSR translates to a friendly "permit not found" page.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No permit found for ticket {padded_ticket}.",
        )

    return _to_response(record, padded_ticket)
