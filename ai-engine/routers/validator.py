"""
Submission validator HTTP surface.

POST /v1/validator/submission
  Internal endpoint called by `admin-api` (or directly by a test client)
  to score a citizen's permit submission. Returns a structured
  ValidationResult with completion %, per-document extracted fields,
  and a prioritized issue list. See `services.agents.validator` for
  the core logic and [[BIMA Vision]] req #6 for the product framing.

Auth: gated by X-Internal-Key. Mirrors admin-api's /tracking pattern.

Demo mode:
  When `ENABLE_DEMO_FIXTURES=true` (default), callers can pass
  `?demo_fixture=clean|name_mismatch|nik_typo` to skip Gemini Vision
  entirely and return a canned ValidateResponse from
  `ai-engine/tests/fixtures/<name>.json`. Used by the bima-admin case
  page for stage rehearsals — no PII, no quota burn, no flaky OCR.
  Flip the env flag to false before the production cutover; with it off,
  demo_fixture requests return 403 and the real path is unaffected.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from deps import require_internal_key
from services import notifications
from services.pii import mask_pii
from services.agents.validator import (
    SUPPORTED_DOC_TYPES,
    Document,
    Issue as ValidatorIssue,
    ValidationResult,
    cross_validate as v1_cross_validate,
    extract_document as v1_extract_document,
    validate_submission,
)
from services.agents.suitability_judge import (
    Issue as SuitabilityIssue,
    SuitabilityResult,
    UploadedDoc,
    judge_submission,
)

logger = logging.getLogger("bima_ai.validator")

router = APIRouter(prefix="/v1/validator", tags=["Validator"])

# Hard cap to keep memory predictable. Real KTP scans are ~1-3 MB; we
# allow 8 MB per file. PDFs of NIB/NPWP are typically under 1 MB. If a
# user uploads a 250 MB shapefile here, reject at the door.
_MAX_BYTES_PER_DOC = 8 * 1024 * 1024
_MAX_DOCS_PER_REQUEST = 10

# Demo fixtures live next to the validator agent tests. Resolved at import
# time so the path lookup happens once. The directory is checked in to git
# under `ai-engine/tests/fixtures/` — see that folder's README for the
# fixture contents and the prod-rollout flag-flip checklist.
_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
DemoFixtureName = Literal["clean", "name_mismatch", "nik_typo"]


def _demo_fixtures_enabled() -> bool:
    """Read at call-time, not import-time — lets tests/admin-api flip the env.

    Defaults to OFF: a stray `?demo_fixture=clean` must never let a real ticket
    receive a canned "ready" validation that masks a fraudulent submission.
    """
    return os.getenv("ENABLE_DEMO_FIXTURES", "false").strip().lower() in {"1", "true", "yes", "on"}


def _load_fixture(name: DemoFixtureName) -> dict[str, Any]:
    """Load a fixture JSON from disk. Raises 500 on missing/malformed file."""
    path = _FIXTURES_DIR / f"{name}.json"
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("Demo fixture not found | path=%s", path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Demo fixture '{name}' not found on server.",
        )
    except json.JSONDecodeError as e:
        logger.error("Demo fixture malformed | path=%s | err=%s", path, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Demo fixture '{name}' is malformed JSON.",
        )


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
    # --- Trusted notification context (set by admin-api only) ----------
    # These two fields are how a citizen_needs_fix WhatsApp gets a
    # destination. They MUST be populated by admin-api from the SIAP
    # person record tied to the submission — NEVER from unvalidated
    # client/portal input. See the SECURITY note on
    # _maybe_notify_needs_fix() below. If they're absent, the validator
    # simply skips the notification (logged) — it never guesses a phone.
    ticket: str | None = Field(
        default=None,
        max_length=32,
        description=(
            "SIAP ticket number this submission belongs to. Used to build "
            "the portal fix URL and label the notification. Trusted: set "
            "by admin-api, not by end users."
        ),
    )
    citizen_phone: str | None = Field(
        default=None,
        max_length=20,
        description=(
            "Citizen's WhatsApp number, sourced by admin-api from the "
            "trusted SIAP person record. MUST NOT come from unvalidated "
            "client input. When absent, no notification is sent."
        ),
    )
    citizen_name: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "Citizen's name for the notification greeting. Trusted source "
            "(SIAP person record). Falls back to a generic greeting if absent."
        ),
    )


class IssueOut(BaseModel):
    severity: str
    field: str
    message: str
    related_docs: list[str]


class PerDocOut(BaseModel):
    extracted: dict[str, Any]


ValidationStatus = Literal["ready", "minor_issues", "major_issues", "unverified"]


class ValidateResponse(BaseModel):
    score: float = Field(..., ge=0.0, le=1.0)
    score_percent: int = Field(..., ge=0, le=100)
    # Locked enum — must match admin/src/lib/case-types.ts ValidationStatus.
    # See QA finding C1 (2026-05-19) for the original drift incident.
    status: ValidationStatus
    summary: str
    per_document: dict[str, dict[str, Any]]
    issues: list[IssueOut]


# ---------------------------------------------------------------------------
# citizen_needs_fix notification wiring
# ---------------------------------------------------------------------------

# Issue severities (from services.agents.validator.IssueLevel) that make a
# minor_issues result still worth a "please fix" WhatsApp.
_NEEDS_FIX_SEVERITIES = {"critical", "high"}

_PORTAL_TRACK_URL = "https://portal.nolongin.com/track/{ticket}"


def _is_needs_fix(result: ValidationResult) -> bool:
    """A result warrants a citizen_needs_fix ping when it has major issues,
    or minor issues that still include at least one CRITICAL/HIGH item."""
    if result.status == "major_issues":
        return True
    if result.status == "minor_issues":
        return any(
            i.severity.lower() in _NEEDS_FIX_SEVERITIES for i in result.issues
        )
    return False


def _top_issue_summary(result: ValidationResult) -> str:
    """A short human summary of the most important issue, for the WhatsApp
    body. Picks the highest-severity issue's message and truncates it so a
    single template parameter stays compact."""
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    ranked = sorted(
        result.issues,
        key=lambda i: severity_rank.get(i.severity.lower(), 99),
    )
    if not ranked:
        return result.summary[:160]
    msg = " ".join(ranked[0].message.split())
    return msg if len(msg) <= 160 else msg[:157] + "…"


def _mask_phone(phone: str) -> str:
    if len(phone) < 8:
        return "<short>"
    return phone[:4] + "…" + phone[-4:]


async def _maybe_notify_needs_fix(
    *,
    result: ValidationResult,
    ticket: Optional[str],
    citizen_phone: Optional[str],
    citizen_name: Optional[str],
) -> None:
    """Fire a citizen_needs_fix WhatsApp when the validation result calls
    for it. Designed to run as a detached asyncio task — it never raises
    and never blocks the validation response.

    SECURITY: `citizen_phone` MUST originate from a trusted source — the
    SIAP person record tied to the submission, passed in by admin-api.
    It is NEVER taken from unvalidated client/portal input, because doing
    so would let an attacker make BIMA WhatsApp an arbitrary number. If
    no trusted phone (or no ticket) is available we log and skip — we do
    NOT guess a recipient.

    notifications.notify() itself respects BIMA_NOTIFICATIONS_ENABLED, so
    wiring this in while the flag is off is safe — it becomes a no-op.
    """
    try:
        if not _is_needs_fix(result):
            return

        if not citizen_phone or not ticket:
            logger.info(
                "needs_fix notify skipped — no trusted recipient | "
                "ticket=%s has_phone=%s status=%s",
                ticket or "<none>",
                bool(citizen_phone),
                result.status,
            )
            return

        fix_url = _PORTAL_TRACK_URL.format(ticket=ticket)
        params = {
            "name": citizen_name or "Pemohon",
            "ticket": ticket,
            "issue_summary": _top_issue_summary(result),
            "fix_url": fix_url,
        }

        logger.info(
            "needs_fix notify dispatching | ticket=%s phone=%s status=%s",
            ticket,
            _mask_phone(citizen_phone),
            result.status,
        )
        # dedupe_key includes the score so an identical re-run (e.g. a
        # retrying caller) is suppressed within the 6h window, but a
        # genuine re-validation producing a DIFFERENT score still
        # notifies the citizen. result.score is a 0.0-1.0 float.
        await notifications.notify(
            event="citizen_needs_fix",
            recipient_phone=citizen_phone,
            params=params,
            dedupe_key=f"{ticket}:{int(round(result.score * 100))}",
        )
    except Exception:
        # Fire-and-forget: a notification failure must never surface to the
        # caller or crash the background task. Log with traceback.
        logger.exception(
            "needs_fix notify task crashed | ticket=%s", ticket or "<none>"
        )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/submission",
    response_model=ValidateResponse,
    summary="Validate a permit submission and return a completion score",
)
async def validate_submission_endpoint(
    _internal: Annotated[bool, Depends(require_internal_key)],
    # `Body(default=None, embed=False)` is the explicit form that tells
    # FastAPI/Pydantic v2 the body is genuinely optional — without `embed`,
    # an empty body with `Content-Type: application/json` would trigger the
    # global RequestValidationError handler before the demo short-circuit
    # could run. Per QA finding H1 (2026-05-19).
    body: Annotated[Optional[ValidateRequest], Body(embed=False)] = None,
    demo_fixture: Optional[DemoFixtureName] = Query(
        default=None,
        description=(
            "If set (and ENABLE_DEMO_FIXTURES=true), return a canned "
            "ValidateResponse from disk and skip Gemini Vision entirely. "
            "One of: clean, name_mismatch, nik_typo."
        ),
    ),
) -> ValidateResponse:
    # --- Demo fixture short-circuit -------------------------------------
    # Runs BEFORE body validation so the caller can hit the endpoint with
    # only a query param — useful for the bima-admin case page demo where
    # no real upload exists.
    if demo_fixture is not None:
        if not _demo_fixtures_enabled():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Demo fixtures are disabled in this environment "
                    "(ENABLE_DEMO_FIXTURES=false). Submit a real document instead."
                ),
            )
        logger.warning(
            "Demo fixture mode | fixture=%s | bypassing Gemini Vision",
            demo_fixture,
        )
        payload = _load_fixture(demo_fixture)
        return ValidateResponse(**payload)

    # --- Real path: body is required ------------------------------------
    if body is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request body is required when demo_fixture is not set.",
        )

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

    # Fire-and-forget: if the submission needs corrections, ping the
    # citizen on WhatsApp. asyncio.create_task so this never delays the
    # validation response. The notification is gated three ways:
    #   1. _maybe_notify_needs_fix only acts on a needs-fix status;
    #   2. it only sends to a trusted phone supplied by admin-api;
    #   3. notifications.notify respects BIMA_NOTIFICATIONS_ENABLED.
    asyncio.create_task(
        _maybe_notify_needs_fix(
            result=result,
            ticket=body.ticket,
            citizen_phone=body.citizen_phone,
            citizen_name=body.citizen_name,
        )
    )

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

# ---------------------------------------------------------------------------
# /v1/validator/suitability — Phase 5+ §5B (post-demo)
#
# Three new scoring dimensions on top of the v1 validator:
#   1. completeness  — required docs present?
#   2. type_correctness — does each file match its claimed type?
#   3. suitability — does each doc satisfy its requirement text?
#
# The existing cross-doc consistency checks (v1) are still produced and
# passed through as `compatibility_findings` so the admin UI's "Issues"
# tab still surfaces them. Auth-gated like /submission; the admin
# follow-up PR will surface results in a tabbed view at /case/[ticket].
# ---------------------------------------------------------------------------


class SuitabilityDocumentInput(BaseModel):
    """One uploaded document for the suitability endpoint.

    Note: unlike /submission, `claimed_type` is FREE TEXT here — the agent
    canonicalises it. This lets admin-api forward whatever label the
    citizen attached without re-validating against the v1 enum.
    """

    file_id: str = Field(..., min_length=1, max_length=128, description="Opaque admin-api file id.")
    claimed_type: str = Field(..., min_length=1, max_length=64)
    filename: str = Field(..., min_length=1, max_length=255)
    mime_type: str = Field(..., min_length=1, max_length=100)
    content_base64: str = Field(..., description="Base64-encoded file bytes.")


class SuitabilityRequest(BaseModel):
    license_id: int = Field(..., gt=0, description="SIAP license_id — required.")
    documents: list[SuitabilityDocumentInput] = Field(..., min_length=1)
    # Mirror /submission so admin-api can route the same notification
    # context through if it wants to follow up with a citizen ping later.
    # The suitability endpoint itself does NOT auto-notify (post-demo
    # scope; the UI surfaces results first).
    ticket: str | None = Field(default=None, max_length=32)


class CompletenessOut(BaseModel):
    score: float = Field(..., ge=0.0, le=1.0)
    missing: list[str]
    required: list[str]
    note: Optional[str] = None


class TypeCorrectnessOut(BaseModel):
    file: str
    file_id: str
    claimed_type: str
    detected_type: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    matches: bool
    note: Optional[str] = None


class SuitabilityFindingOut(BaseModel):
    requirement: str
    file: Optional[str] = None
    file_id: Optional[str] = None
    judgement: str  # match | partial | mismatch | unknown
    evidence: str
    confidence: float = Field(..., ge=0.0, le=1.0)


class SuitabilityIssueOut(BaseModel):
    id: str
    severity: str
    title: str
    detail: str


class SuitabilityResponse(BaseModel):
    license_id: int
    completeness: CompletenessOut
    type_correctness: list[TypeCorrectnessOut]
    suitability: list[SuitabilityFindingOut]
    compatibility_findings: list[SuitabilityIssueOut]
    overall_suitability_score: float = Field(..., ge=0.0, le=1.0)
    overall_suitability_percent: int = Field(..., ge=0, le=100)
    issues: list[SuitabilityIssueOut]


def _v1_issues_to_suitability_issues(
    v1_issues: list[ValidatorIssue],
) -> list[SuitabilityIssue]:
    """Reshape v1 validator Issues (severity/field/message) into the
    suitability Issue model (id/severity/title/detail). The title is the
    field name humanised; the detail is the message. Filenames in
    related_docs are appended to the detail so the admin card still has
    cross-reference info."""
    out: list[SuitabilityIssue] = []
    for i in v1_issues:
        detail = i.message
        if i.related_docs:
            detail = f"{detail} (berkas: {', '.join(i.related_docs)})"
        out.append(SuitabilityIssue(
            id=f"compat:{i.field}",
            severity=i.severity,
            title=i.field.replace("_", " ").capitalize(),
            detail=detail,
        ))
    return out


async def _run_v1_compatibility(documents: list[UploadedDoc]) -> list[SuitabilityIssue]:
    """Run the v1 extract+cross_validate pipeline on the upload set and
    return its Issues reshaped for the suitability schema.

    Wrapped in defensive try/except: a compatibility failure must not
    crash the whole suitability response — the dimension simply degrades
    to empty.
    """
    try:
        # The v1 validator only knows ktp/nib/npwp. Map by canonical
        # class so the suitability caller can pass arbitrary labels.
        from services.agents.suitability_judge import _canonical_class  # local import
        v1_docs: list[Document] = []
        canonical_to_v1 = {"KTP": "ktp", "NIB": "nib", "NPWP": "npwp"}
        for d in documents:
            v1_type = canonical_to_v1.get(_canonical_class(d.claimed_type))
            if v1_type is None:
                continue
            v1_docs.append(Document(
                doc_type=v1_type,
                mime_type=d.mime_type,
                filename=d.filename,
                content=d.content,
            ))
        if not v1_docs:
            return []

        extracted_lists = await asyncio.gather(
            *[v1_extract_document(d) for d in v1_docs], return_exceptions=False
        )
        per_doc: dict[str, dict[str, Any]] = {}
        for v1_doc, fields in zip(v1_docs, extracted_lists):
            if fields is None or v1_doc.doc_type in per_doc:
                continue
            per_doc[v1_doc.doc_type] = fields
        v1_issues = v1_cross_validate(per_doc, v1_docs)
        return _v1_issues_to_suitability_issues(v1_issues)
    except Exception:
        logger.exception("v1 compatibility pass failed in suitability endpoint")
        return []


@router.post(
    "/suitability",
    response_model=SuitabilityResponse,
    summary="Score a submission's completeness, type-correctness, and suitability",
)
async def suitability_endpoint(
    _internal: Annotated[bool, Depends(require_internal_key)],
    body: SuitabilityRequest,
) -> SuitabilityResponse:
    if len(body.documents) > _MAX_DOCS_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Maximum {_MAX_DOCS_PER_REQUEST} documents per request.",
        )

    documents: list[UploadedDoc] = []
    for doc_input in body.documents:
        try:
            content = base64.b64decode(doc_input.content_base64, validate=True)
        except (ValueError, Exception) as e:
            logger.warning(
                "suitability base64 decode failed | filename=%s | err=%s",
                doc_input.filename, e,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Document '{doc_input.filename}' is not valid base64.",
            )

        if len(content) > _MAX_BYTES_PER_DOC:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"Document '{doc_input.filename}' is "
                    f"{len(content) / 1024 / 1024:.1f} MB, max "
                    f"{_MAX_BYTES_PER_DOC / 1024 / 1024:.0f} MB allowed per file."
                ),
            )

        documents.append(UploadedDoc(
            file_id=doc_input.file_id,
            claimed_type=doc_input.claimed_type,
            filename=doc_input.filename,
            mime_type=doc_input.mime_type,
            content=content,
        ))

    logger.info(
        "Suitability request | license_id=%s docs=%d ticket=%s",
        body.license_id, len(documents), body.ticket or "<none>",
    )

    # Run v1 compatibility in parallel with the new suitability layer so
    # the endpoint's wall-clock matches /submission. judge_submission
    # accepts compatibility_findings as input — but we want to compute
    # them in parallel, so we kick both off then await.
    compat_task = asyncio.create_task(_run_v1_compatibility(documents))
    suit_task = asyncio.create_task(
        judge_submission(
            license_id=body.license_id,
            documents=documents,
            compatibility_findings=None,  # injected below
        )
    )

    compatibility_findings = await compat_task
    suit_result: SuitabilityResult = await suit_task

    # Merge the parallel-computed compatibility findings into the result
    # without re-running the suitability/type/completeness logic. We
    # re-sort the issue list afterwards because adding HIGH/CRITICAL
    # compatibility issues can change priority order.
    suit_result.compatibility_findings = compatibility_findings
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    merged_issues = list(suit_result.issues)
    # Drop any leftover passthrough placeholders the agent may have added,
    # then re-append the freshly-computed compatibility issues.
    merged_issues = [
        i for i in merged_issues if not i.id.startswith("compat:")
    ] + compatibility_findings
    merged_issues.sort(key=lambda i: severity_rank.get(i.severity, 99))
    suit_result.issues = merged_issues

    return SuitabilityResponse(
        license_id=body.license_id,
        completeness=CompletenessOut(
            score=suit_result.completeness.score,
            missing=suit_result.completeness.missing,
            required=suit_result.completeness.required,
            note=suit_result.completeness.note,
        ),
        type_correctness=[
            TypeCorrectnessOut(
                file=f.file,
                file_id=f.file_id,
                claimed_type=f.claimed_type,
                detected_type=f.detected_type,
                confidence=f.confidence,
                matches=f.matches,
                note=f.note,
            )
            for f in suit_result.type_correctness
        ],
        suitability=[
            SuitabilityFindingOut(
                requirement=f.requirement,
                file=f.file,
                file_id=f.file_id,
                judgement=f.judgement,
                evidence=mask_pii(f.evidence),
                confidence=f.confidence,
            )
            for f in suit_result.suitability
        ],
        compatibility_findings=[
            SuitabilityIssueOut(
                id=i.id,
                severity=i.severity,
                title=mask_pii(i.title),
                detail=mask_pii(i.detail),
            )
            for i in suit_result.compatibility_findings
        ],
        overall_suitability_score=suit_result.overall_suitability_score,
        overall_suitability_percent=int(round(suit_result.overall_suitability_score * 100)),
        issues=[
            SuitabilityIssueOut(
                id=i.id,
                severity=i.severity,
                title=mask_pii(i.title),
                detail=mask_pii(i.detail),
            )
            for i in suit_result.issues
        ],
    )
