"""
Officer-facing case detail + validator proxy + copilot.

Endpoints, all JWT-gated (officers/admins, not service-to-service):

    GET  /case/{ticket}              — fetch SIAP case data (cheap, page header)
    POST /case/{ticket}/validate     — run BIMA validator on attached docs,
                                       return combined SIAP record + validation
    POST /case/{ticket}/copilot/chat — run one Officer Copilot turn against a
                                       persistent per-officer-per-case session

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

Copilot session ownership (Decisions §22):
  admin-api owns the `copilot_session` table — one row per (officer, ticket).
  ai-engine's `/v1/copilot/chat` is stateless. The `/copilot/chat` endpoint
  here loads the prior history, calls ai-engine, and persists the returned
  history. SECURITY: `officer_id` is ALWAYS `current_user.id` from the
  verified JWT — an officer can only ever load their own session.

Sprint D scope:
  Demo fixture passthrough (`?demo_fixture=clean|name_mismatch|nik_typo`) so
  rehearsals don't depend on Gemini latency or quota. See [[Decisions]] §21.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_admin_user, get_db
from app.models import CopilotSession, User
from app.services.copilot_client import get_copilot_client
from app.services.siap_client import get_siap_tracking_client
from app.services.validator_client import get_validator_client

logger = logging.getLogger("bima_admin_api.case")

router = APIRouter()

# Copilot guardrails — kept loose since the surface is internal/officer-only,
# but bounded so a runaway client cannot grow a session row without limit.
_MAX_COPILOT_MESSAGE_CHARS = 4000
# Hard cap on stored turns. ai-engine's own router rejects history > 40 turns,
# so we trim to the most recent 40 before sending. Each turn is user+model, so
# 40 turns ≈ 20 exchanges — comfortable for a case-analysis session.
_MAX_COPILOT_HISTORY_TURNS = 40

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


# --- Copilot ----------------------------------------------------------------


class CopilotHistoryTurn(BaseModel):
    """One stored conversation turn. Matches ai-engine's HistoryTurn shape."""

    role: str = Field(..., description="'user' or 'model'.")
    text: str = Field(..., min_length=1)


class CopilotChatRequest(BaseModel):
    """Body for POST /case/{ticket}/copilot/chat.

    Deliberately minimal: NO `officer_id` and NO `history` field. The officer
    is taken from the JWT; the history is loaded server-side from the
    `copilot_session` table. Accepting either from the client would let an
    officer impersonate another or inject a forged transcript.
    """

    message: str = Field(
        ...,
        min_length=1,
        max_length=_MAX_COPILOT_MESSAGE_CHARS,
        description="The officer's new message to the copilot.",
    )
    validation: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional BIMA validator result the case page already holds "
            "(score, status, summary, issues). Forwarded to the copilot so "
            "its get_validation_summary tool can surface it without a fresh "
            "Gemini Vision pass. Omit if the page has not validated yet."
        ),
    )


class CopilotToolCall(BaseModel):
    name: str
    args: dict[str, Any]
    result_preview: str


class CopilotChatResponse(BaseModel):
    reply: str = Field(..., description="The copilot's Indonesian reply.")
    tool_calls: list[CopilotToolCall] = Field(
        default_factory=list,
        description="Transcript of tool calls the agent made this turn.",
    )
    history: list[CopilotHistoryTurn] = Field(
        default_factory=list,
        description="The full persisted conversation after this turn.",
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

    # In demo mode, send no documents at all — ai-engine accepts a body-less
    # POST when `?demo_fixture=` is set (QA fix H1+H2, 2026-05-19). This
    # removes the former empty-base64 stub workaround.
    docs_to_send: Optional[list[dict]] = (
        None if body.demo_fixture is not None else (body.documents or [])
    )

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


# ---------------------------------------------------------------------------
# Copilot session persistence
# ---------------------------------------------------------------------------


def _coerce_history(raw: Any) -> list[dict[str, str]]:
    """Defensively normalize stored history_json into a clean turn list.

    The column is JSON, so an old/corrupt row could hold anything. We keep
    only well-formed {role: user|model, text: non-empty str} turns so a bad
    row degrades to "shorter history" rather than crashing the turn.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for turn in raw:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role", "")).strip().lower()
        text = turn.get("text", "")
        if role not in {"user", "model"} or not isinstance(text, str) or not text:
            continue
        out.append({"role": role, "text": text})
    return out


async def _get_or_create_session(
    db: AsyncSession, *, officer_id: int, ticket: str
) -> CopilotSession:
    """Load the (officer_id, ticket) copilot session, creating it on first use.

    SECURITY: the lookup is keyed on `officer_id` (always the JWT subject) AND
    `ticket`, so an officer can only ever touch their own session row.
    """
    result = await db.execute(
        select(CopilotSession).where(
            CopilotSession.officer_id == officer_id,
            CopilotSession.ticket == ticket,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        session = CopilotSession(
            officer_id=officer_id,
            ticket=ticket,
            history_json=[],
        )
        db.add(session)
        # Flush so the row exists for this transaction; commit happens via the
        # get_db dependency's clean-return path.
        await db.flush()
    return session


@router.post(
    "/{ticket}/copilot/chat",
    response_model=CopilotChatResponse,
    responses={
        400: {"description": "Malformed ticket or empty message."},
        404: {"description": "No case matches the given ticket."},
        502: {"description": "Copilot upstream (ai-engine) returned an error."},
        503: {"description": "SIAP or ai-engine copilot not configured."},
    },
    summary="Run one Officer Copilot turn against a persistent session.",
)
async def copilot_chat(
    ticket: Annotated[str, Path(min_length=4, max_length=9, pattern=r"^\d{4,9}$")],
    body: CopilotChatRequest,
    current_user: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CopilotChatResponse:
    """
    Run one turn of the Officer Copilot for the authenticated officer.

    Flow (Decisions §22 — admin-api owns durability, ai-engine is stateless):
      1. Derive `officer_id` from the verified JWT (never the request body).
      2. Confirm the ticket exists in SIAP.
      3. Load (or create) the officer's `copilot_session` row for this ticket.
      4. POST {message, prior history, validation} to ai-engine's
         `/v1/copilot/chat`.
      5. Persist the updated history back to the session row + bump
         `last_active_at`.
      6. Return reply + tool transcript + the full persisted history.

    SECURITY: the session is scoped to `current_user.id`. Officer A calling
    this endpoint can never load or mutate officer B's conversation, because
    every query is filtered on `officer_id == current_user.id`.
    """
    padded_ticket = _validate_ticket(ticket)
    officer_id = current_user.id

    # Confirm the ticket is real before spinning up a copilot turn. Reuses the
    # same SIAP lookup the case header uses — raises 404/503 uniformly.
    await _fetch_case_or_404(padded_ticket)

    copilot = get_copilot_client()
    if not copilot.is_configured():
        logger.warning(
            "Copilot chat for %s but copilot client not configured", padded_ticket
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Officer Copilot (ai-engine) is not configured on this environment.",
        )

    session = await _get_or_create_session(
        db, officer_id=officer_id, ticket=padded_ticket
    )

    # Trim to the most recent N turns so we never exceed ai-engine's history
    # cap (40) even on a long-running session.
    prior_history = _coerce_history(session.history_json)[-_MAX_COPILOT_HISTORY_TURNS:]

    logger.info(
        "Copilot turn | officer_id=%s | ticket=%s | prior_turns=%d | "
        "has_validation=%s",
        officer_id,
        padded_ticket,
        len(prior_history),
        body.validation is not None,
    )

    ok, payload = await copilot.chat(
        ticket=padded_ticket,
        officer_id=officer_id,
        message=body.message,
        history=prior_history,
        validation=body.validation,
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": "Officer Copilot (ai-engine) failed to return a reply.",
                "copilot_error": payload,
            },
        )

    # ai-engine returns the full updated history (prior + new user turn + new
    # model reply). Re-coerce defensively, trim, and persist.
    updated_history = _coerce_history(payload.get("history"))[
        -_MAX_COPILOT_HISTORY_TURNS:
    ]
    session.history_json = updated_history
    session.last_active_at = datetime.now(timezone.utc)
    # The get_db dependency commits on clean return.

    tool_calls = [
        CopilotToolCall(
            name=tc.get("name", ""),
            args=tc.get("args") or {},
            result_preview=tc.get("result_preview", ""),
        )
        for tc in (payload.get("tool_calls") or [])
        if isinstance(tc, dict)
    ]

    return CopilotChatResponse(
        reply=payload.get("reply", ""),
        tool_calls=tool_calls,
        history=[
            CopilotHistoryTurn(role=h["role"], text=h["text"])
            for h in updated_history
        ],
    )


@router.get(
    "/{ticket}/copilot/session",
    response_model=CopilotChatResponse,
    responses={
        400: {"description": "Malformed ticket."},
    },
    summary="Load the officer's persisted copilot session for a ticket.",
)
async def get_copilot_session(
    ticket: Annotated[str, Path(min_length=4, max_length=9, pattern=r"^\d{4,9}$")],
    current_user: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CopilotChatResponse:
    """
    Return the authenticated officer's persisted copilot conversation for a
    ticket, so the case page can rehydrate the panel after a navigation.

    No SIAP call, no ai-engine call — a pure read of `copilot_session`. When
    no session exists yet the history is empty (the page then triggers the
    auto-generated validation opener). `reply` is the last model turn, if any,
    purely as a convenience for the client.
    """
    padded_ticket = _validate_ticket(ticket)

    result = await db.execute(
        select(CopilotSession).where(
            CopilotSession.officer_id == current_user.id,
            CopilotSession.ticket == padded_ticket,
        )
    )
    session = result.scalar_one_or_none()
    history = _coerce_history(session.history_json) if session else []

    last_model = next(
        (h["text"] for h in reversed(history) if h["role"] == "model"), ""
    )
    return CopilotChatResponse(
        reply=last_model,
        tool_calls=[],
        history=[
            CopilotHistoryTurn(role=h["role"], text=h["text"]) for h in history
        ],
    )
