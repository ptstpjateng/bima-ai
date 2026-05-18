"""
Officer-facing case detail + validator proxy.

Two endpoints, both JWT-gated (officers/admins, not service-to-service):

    GET  /case/{ticket}            — fetch SIAP case data (cheap, page header)
    POST /case/{ticket}/validate   — run BIMA validator on attached docs,
                                     return combined SIAP record + validation

Why split GET from POST:
  The bima-admin case page renders SIAP fields immediately on load (header,
  status pill, applicant) and only fires validation on user click. Running
  Gemini Vision on every page view would be ~10s of latency per render and
  burn API quota. So GET is fast and POST is opt-in.

Auth contrast with `/tracking/{ticket}`:
  /tracking is service-to-service (portal SSR → admin-api, X-Internal-Key),
  serves anonymous citizens looking up their own ticket. /case is officers
  triaging the queue from the admin console — JWT-gated with admin role.
  Same SIAP read underneath, different trust boundary.

Sprint D scope:
  Demo fixture passthrough (`?demo_fixture=clean|name_mismatch|nik_typo`) so
  rehearsals don't depend on Gemini latency or quota. See [[Decisions]] §21.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field

from app.deps import get_current_admin_user
from app.services.siap_client import get_siap_tracking_client
from app.services.validator_client import get_validator_client

logger = logging.getLogger("bima_admin_api.case")

router = APIRouter()

# Same ticket grammar used by /tracking — 4-9 digits, zero-padded inside.
_TICKET_PATTERN = re.compile(r"^\d{4,9}$")
_ALLOWED_DEMO_FIXTURES = frozenset({"clean", "name_mismatch", "nik_typo"})


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class CaseRecord(BaseModel):
    """Normalized SIAP record for the case detail view (snake_case).

    Wider than /tracking's `TrackingResponse` because officers want raw fields
    visible (NIK, raw status enum) that we hide from citizens.
    """

    ticket: str = Field(..., description="Zero-padded 9-digit ticket number.")
    license_name: str = Field(..., description="Human-readable permit name.")
    sector_name: Optional[str] = Field(None)
    applicant_name: str = Field(..., description="Title-cased applicant name.")
    current_desk: str = Field(..., description="`posisi_berkas` from SIAP.")
    status: str = Field(..., description="`status_permohonan` from SIAP.")
    submitted_at: Optional[str] = Field(None)
    raw: dict[str, Any] = Field(
        default_factory=dict,
        description="Untransformed SIAP record — for fields not yet promoted to first-class.",
    )


class ValidateRequest(BaseModel):
    """Body for POST /case/{ticket}/validate."""

    documents: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description=(
            "List of `{doc_type, filename, mime_type, content_base64}` dicts. "
            "Required unless `demo_fixture` is set."
        ),
    )
    demo_fixture: Optional[str] = Field(
        default=None,
        description="One of: clean | name_mismatch | nik_typo. Bypasses Gemini Vision.",
    )


class CaseWithValidation(BaseModel):
    case: CaseRecord
    validation: dict[str, Any] = Field(
        ...,
        description="Raw ValidateResponse from ai-engine (score, issues, per_document).",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_ticket(ticket: str) -> str:
    """Reject malformed input early; zero-pad to canonical 9 digits."""
    if not _TICKET_PATTERN.match(ticket):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ticket must be 4-9 digits.",
        )
    return ticket.zfill(9)


def _to_case_record(record: dict, ticket: str) -> CaseRecord:
    return CaseRecord(
        ticket=record.get("no_tiket", ticket),
        license_name=record.get("nama_perizinan", "—"),
        sector_name=record.get("nama_bidang"),
        applicant_name=(record.get("nama_pemohon") or "").title(),
        current_desk=record.get("posisi_berkas", "—"),
        status=record.get("status_permohonan", "—"),
        submitted_at=record.get("tanggal_permohonan"),
        raw=record,
    )


async def _fetch_case_or_404(padded_ticket: str) -> CaseRecord:
    """Shared lookup used by both GET and POST. Translates client errors uniformly."""
    siap = get_siap_tracking_client()
    if not siap.is_configured():
        logger.warning("Case request for %s but SIAP client not configured", padded_ticket)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SIAP integration is not configured on this environment.",
        )

    record = await siap.get_status_by_ticket(padded_ticket)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No case found for ticket {padded_ticket}.",
        )
    return _to_case_record(record, padded_ticket)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{ticket}",
    response_model=CaseRecord,
    responses={
        404: {"description": "No case matches the given ticket."},
        503: {"description": "SIAP integration not configured or upstream is down."},
    },
    summary="Fetch a single case by ticket (officer view).",
)
async def get_case(
    ticket: Annotated[str, Path(min_length=4, max_length=9, pattern=r"^\d{4,9}$")],
    _current_user=Depends(get_current_admin_user),
) -> CaseRecord:
    """
    Return the SIAP record for one ticket.

    Cheap, ~200ms — used as the case page's header data. Validation is a
    separate POST so we don't burn Gemini quota on every page view.
    """
    padded_ticket = _validate_ticket(ticket)
    return await _fetch_case_or_404(padded_ticket)


@router.post(
    "/{ticket}/validate",
    response_model=CaseWithValidation,
    responses={
        400: {"description": "Bad payload (no documents and no demo_fixture, or unknown fixture)."},
        404: {"description": "No case matches the given ticket."},
        502: {"description": "ai-engine validator returned an error or unreachable."},
        503: {"description": "SIAP or ai-engine not configured."},
    },
    summary="Run BIMA validator on a case's documents.",
)
async def validate_case(
    ticket: Annotated[str, Path(min_length=4, max_length=9, pattern=r"^\d{4,9}$")],
    body: ValidateRequest,
    _current_user=Depends(get_current_admin_user),
) -> CaseWithValidation:
    """
    Combined endpoint: re-confirms the ticket exists in SIAP, then runs the
    submission validator against the supplied documents (or a demo fixture).

    Officer workflow:
      1. Open the case page → GET /case/{ticket} renders header instantly.
      2. Officer attaches scanned docs (KTP, NIB, NPWP) via the upload UI.
      3. Click "Validate" → POST /case/{ticket}/validate with documents.
      4. UI renders score + issues without losing the case header context.

    Demo mode:
      Pass `demo_fixture: "clean" | "name_mismatch" | "nik_typo"` and the
      validator returns a deterministic, sub-second canned response. The
      `documents` field is then optional — we still pass a one-element stub
      to satisfy ai-engine's `min_length=1` constraint.
    """
    padded_ticket = _validate_ticket(ticket)

    # ---- Payload sanity -------------------------------------------------
    if body.demo_fixture is not None and body.demo_fixture not in _ALLOWED_DEMO_FIXTURES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unknown demo_fixture {body.demo_fixture!r}. "
                f"Allowed: {sorted(_ALLOWED_DEMO_FIXTURES)}"
            ),
        )

    if body.demo_fixture is None and not body.documents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one document is required when demo_fixture is not set.",
        )

    # ---- Confirm the ticket is real before burning a Gemini call -------
    case_record = await _fetch_case_or_404(padded_ticket)

    # ---- Validator ------------------------------------------------------
    validator = get_validator_client()
    if not validator.is_configured():
        logger.warning(
            "Validate request for %s but validator client not configured",
            padded_ticket,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Validator (ai-engine) is not configured on this environment.",
        )

    if body.demo_fixture is not None:
        # ai-engine requires min_length=1 even in demo mode. Stub satisfies
        # the schema without doing any real upload/decoding.
        docs_to_send: list[dict] = [
            {
                "doc_type": "ktp",
                "filename": "_demo_stub.bin",
                "mime_type": "application/octet-stream",
                "content_base64": "",
            }
        ]
    else:
        docs_to_send = body.documents or []

    ok, payload = await validator.validate(
        documents=docs_to_send,
        demo_fixture=body.demo_fixture,
    )
    if not ok:
        # Distinguish upstream-error (502) from upstream-not-configured (503,
        # already handled above). Any other failure mode goes through 502.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": "Validator (ai-engine) failed to return a result.",
                "validator_error": payload,
            },
        )

    # Bridge the per_document contract: ai-engine returns a dict keyed by
    # doc_type (ktp/nib/npwp) → extracted fields; the bima-admin client
    # expects an array of {filename, fields_extracted, fields_expected}.
    # We transform here so neither side has to know about the other's
    # natural shape. See QA finding C2 (2026-05-19) for the original
    # drift incident.
    payload = {
        **payload,
        "per_document": _transform_per_document(payload.get("per_document")),
    }

    return CaseWithValidation(case=case_record, validation=payload)


# Field counts per doc type — kept in sync with the schemas declared in
# ai-engine/services/agents/validator.py (_KTP_SCHEMA, _NIB_SCHEMA, _NPWP_SCHEMA).
# Used to compute fields_extracted / fields_expected for the UI gauge.
_EXPECTED_FIELDS_BY_DOC_TYPE: dict[str, int] = {"ktp": 16, "nib": 8, "npwp": 4}
_DOC_TYPE_LABEL: dict[str, str] = {
    "ktp": "Kartu Tanda Penduduk (KTP)",
    "nib": "Nomor Induk Berusaha (NIB)",
    "npwp": "Nomor Pokok Wajib Pajak (NPWP)",
}


def _transform_per_document(per_doc: dict[str, dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Reshape ai-engine's dict-keyed-by-doc-type into the array the client wants.

    Returns one item per uploaded doc type with the count of non-empty
    fields, total expected fields, and a human label for the chip row.
    """
    if not per_doc:
        return []
    result: list[dict[str, Any]] = []
    for doc_type, extracted in per_doc.items():
        if not isinstance(extracted, dict):
            continue
        filled = sum(1 for v in extracted.values() if v not in (None, "", [], {}))
        result.append(
            {
                "filename": _DOC_TYPE_LABEL.get(doc_type, doc_type.upper()),
                "fields_extracted": filled,
                "fields_expected": _EXPECTED_FIELDS_BY_DOC_TYPE.get(
                    doc_type, len(extracted)
                ),
                "notes": None,
            }
        )
    return result
