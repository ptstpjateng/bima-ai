"""
Submission validator HTTP surface.

POST /v1/validator/submission
  Internal endpoint called by `admin-api` (or directly by a test client)
  to score a citizen's permit submission. Returns a structured
  ValidationResult with completion %, per-document extracted fields,
  and a prioritized issue list. See `services.agents.validator` for
  the core logic and [[BIMA Vision]] req #6 for the product framing.

Auth: gated by X-Internal-Key. Mirrors admin-api's /tracking pattern.
"""

from __future__ import annotations

import base64
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from deps import require_internal_key
from services.agents.validator import (
    SUPPORTED_DOC_TYPES,
    Document,
    validate_submission,
)

logger = logging.getLogger("bima_ai.validator")

router = APIRouter(prefix="/v1/validator", tags=["Validator"])

# Hard cap to keep memory predictable. Real KTP scans are ~1-3 MB; we
# allow 8 MB per file. PDFs of NIB/NPWP are typically under 1 MB. If a
# user uploads a 250 MB shapefile here, reject at the door.
_MAX_BYTES_PER_DOC = 8 * 1024 * 1024
_MAX_DOCS_PER_REQUEST = 10


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class DocumentInput(BaseModel):
    doc_type: str = Field(..., description=f"One of: {', '.join(SUPPORTED_DOC_TYPES)}")
    filename: str = Field(..., min_length=1, max_length=255)
    mime_type: str = Field(..., min_length=1, max_length=100)
    content_base64: str = Field(..., description="Base64-encoded file bytes")

    @field_validator("doc_type")
    @classmethod
    def _doc_type_supported(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in SUPPORTED_DOC_TYPES:
            raise ValueError(
                f"doc_type {v!r} not supported. "
                f"Currently: {', '.join(SUPPORTED_DOC_TYPES)}"
            )
        return v


class ValidateRequest(BaseModel):
    documents: list[DocumentInput] = Field(..., min_length=1)
    license_id: int | None = Field(
        default=None,
        description="Optional SIAP license_id for license-specific validation (Phase 2.5 — currently unused).",
    )


class IssueOut(BaseModel):
    severity: str
    field: str
    message: str
    related_docs: list[str]


class PerDocOut(BaseModel):
    extracted: dict[str, Any]


class ValidateResponse(BaseModel):
    score: float = Field(..., ge=0.0, le=1.0)
    score_percent: int = Field(..., ge=0, le=100)
    status: str
    summary: str
    per_document: dict[str, dict[str, Any]]
    issues: list[IssueOut]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/submission",
    response_model=ValidateResponse,
    summary="Validate a permit submission and return a completion score",
)
async def validate_submission_endpoint(
    body: ValidateRequest,
    _internal: Annotated[bool, Depends(require_internal_key)],
) -> ValidateResponse:
    if len(body.documents) > _MAX_DOCS_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Maximum {_MAX_DOCS_PER_REQUEST} documents per request.",
        )

    # Decode + size-check inputs before handing to the agent. Failures here
    # surface as 4xx to the caller; failures inside the agent surface as
    # extraction-failed issues in the response.
    documents: list[Document] = []
    for doc_input in body.documents:
        try:
            content = base64.b64decode(doc_input.content_base64, validate=True)
        except (ValueError, Exception) as e:
            logger.warning("base64 decode failed | filename=%s | err=%s", doc_input.filename, e)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Document '{doc_input.filename}' is not valid base64.",
            )

        if len(content) > _MAX_BYTES_PER_DOC:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"Document '{doc_input.filename}' is {len(content) / 1024 / 1024:.1f} MB, "
                    f"max {_MAX_BYTES_PER_DOC / 1024 / 1024:.0f} MB allowed per file."
                ),
            )

        documents.append(Document(
            doc_type=doc_input.doc_type,
            mime_type=doc_input.mime_type,
            filename=doc_input.filename,
            content=content,
        ))

    logger.info(
        "Validator request | docs=%d | types=%s | license_id=%s",
        len(documents),
        [d.doc_type for d in documents],
        body.license_id,
    )

    result = await validate_submission(documents)

    return ValidateResponse(
        score=result.score,
        score_percent=int(round(result.score * 100)),
        status=result.status,
        summary=result.summary,
        per_document=result.per_document,
        issues=[
            IssueOut(
                severity=i.severity,
                field=i.field,
                message=i.message,
                related_docs=i.related_docs,
            )
            for i in result.issues
        ],
    )
