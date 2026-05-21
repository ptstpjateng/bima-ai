"""
Officer inbox router — Wave 4 (the multi-ticket officer inbox).

Endpoint, JWT-gated (officers/admins, same trust boundary as `/case/*`):

    GET /inbox  — the authenticated officer's working queue: every active
                  SIAP case, urgency-ranked, with SOP (jangka waktu) context.

Where `/case/{ticket}` shows ONE case, `/inbox` shows them ALL at once so an
officer can triage by urgency instead of opening tickets blind. Read-only on
SIAP — one parameterised SELECT via the SIAP read-only engine.

Officer → cases mapping (documented decision):
  SIAP's `bloksistem_nama` says which desk holds a file, but the admin-api
  `User` model has no desk / SIAP role-id, so there is no clean officer→desk
  join for Beta. The inbox returns ALL active cases and accepts an optional
  `?desk=` substring filter so an officer can narrow to their desk by hand.
  See `services/siap_inbox_client.py` for the full rationale.

The inbox ranker (`_urgency` below):
  Urgency rises with days-open *relative to* the licence's SOP days, so a case
  at 12/14 days is more urgent than one at 2/14. See the function docstring
  for the exact formula and the no-SOP fallback.
"""

from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.deps import get_current_admin_user
from app.schemas.inbox import InboxCase, InboxResponse
from app.services.siap_inbox_client import MAX_INBOX_ROWS, fetch_officer_inbox

logger = logging.getLogger("bima_admin_api.inbox")

router = APIRouter()


# ---------------------------------------------------------------------------
# The inbox ranker
# ---------------------------------------------------------------------------

# Urgency thresholds on the days-open / SOP-days *ratio*. Kept here (not in the
# schema) because they are pure ranking policy — tuning these never changes the
# API contract. They also drive `sop_state`, the coarse bucket the calm UI
# progress indicator colours itself by.
_APPROACHING_RATIO = 0.70  # >= 70% of SOP used → "approaching", gentle amber
_OVER_RATIO = 1.0  # >= 100% of SOP used → "over", still calm, never red-shame

# No-SOP fallback: when a case has no resolvable SOP we cannot compute a
# ratio. We still want old cases to float up, so we map days-open onto a mild
# absolute curve that tops out below a genuine "ratio = 1.0" case — an unknown
# SOP must never out-rank a case we *know* has blown its statutory deadline.
_NO_SOP_REFERENCE_DAYS = 30.0  # days-open that maps to the fallback ceiling
_NO_SOP_CEILING = 0.65  # fallback urgency never exceeds this


def _urgency(days_open: int, sop_days: Optional[int]) -> tuple[float, str]:
    """
    Score one case and bucket its SOP state.

    Returns `(urgency, sop_state)` where:
      * urgency is a float in roughly [0, 1.5]. The list is sorted by it,
        descending. With a known SOP it is the days-open / SOP-days ratio,
        lightly boosted once the case is *over* SOP so genuinely-late work
        always sits at the top. Without a SOP it falls back to a mild
        absolute-age curve capped below 1.0.
      * sop_state is one of 'on_track' | 'approaching' | 'over' | 'unknown'.

    Why a *ratio*, not raw days: a permit with a 3-day SOP that has sat 4 days
    is in worse shape than a 60-day-SOP permit that has sat 10 days. Ranking on
    the ratio surfaces the case closest to (or past) its own deadline first —
    exactly what the brief asks for ("12/14 more urgent than 2/14").
    """
    d = max(0, days_open)

    if sop_days is not None and sop_days > 0:
        ratio = d / float(sop_days)
        if ratio >= _OVER_RATIO:
            sop_state = "over"
            # Boost past-SOP cases by their overshoot so the most-overdue
            # case ranks above one that just crossed the line. The +0.5 base
            # guarantees every "over" case outranks every "approaching" one.
            urgency = 1.0 + 0.5 * min(ratio - 1.0, 1.0)
        elif ratio >= _APPROACHING_RATIO:
            sop_state = "approaching"
            urgency = ratio
        else:
            sop_state = "on_track"
            urgency = ratio
        return round(urgency, 4), sop_state

    # ---- No resolvable SOP — mild absolute-age fallback --------------------
    fallback = _NO_SOP_CEILING * min(d / _NO_SOP_REFERENCE_DAYS, 1.0)
    return round(fallback, 4), "unknown"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=InboxResponse,
    responses={
        503: {"description": "SIAP DB integration is not configured / unreachable."},
    },
    summary="The authenticated officer's urgency-ranked case inbox.",
)
async def get_inbox(
    _current_user=Depends(get_current_admin_user),
    desk: Annotated[
        Optional[str],
        Query(
            max_length=120,
            description=(
                "Optional case-insensitive substring filter on the SIAP desk "
                "(`bloksistem_nama`). Omit to see every active case."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_INBOX_ROWS,
            description=f"Max cases to return (1–{MAX_INBOX_ROWS}).",
        ),
    ] = MAX_INBOX_ROWS,
) -> InboxResponse:
    """
    Return the officer's working queue — every active SIAP case, ranked by
    urgency descending so the case nearest its statutory deadline is first.

    Each row carries `days_open`, `sop_days` (jangka waktu) and an `urgency`
    score; the SOP/SLA is surfaced as a *motivator* on the frontend, not a
    whip — see the `/inbox` page.

    Raises 503 when the SIAP DB integration is disabled or unreachable; a
    genuinely empty queue is a 200 with `cases: []`.
    """
    rows = await fetch_officer_inbox(desk=desk, limit=limit)
    if rows is None:
        logger.warning("Inbox request but SIAP DB unavailable | desk=%s", desk)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SIAP integration is not configured or is unreachable.",
        )

    cases: list[InboxCase] = []
    for r in rows:
        urgency, sop_state = _urgency(r["days_open"], r["sop_days"])
        cases.append(
            InboxCase(
                ticket=r["ticket"],
                license_name=r["license_name"],
                sector_name=r["sector_name"],
                applicant_name=r["applicant_name"],
                current_desk=r["current_desk"],
                status=r["status"],
                submitted_at=r["submitted_at"],
                days_open=r["days_open"],
                sop_days=r["sop_days"],
                urgency=urgency,
                sop_state=sop_state,
            )
        )

    # Sort by urgency descending; tie-break on days_open so two cases with the
    # same ratio land oldest-first. Deterministic ordering keeps the UI stable
    # across refreshes.
    cases.sort(key=lambda c: (c.urgency, c.days_open), reverse=True)

    return InboxResponse(
        cases=cases,
        total=len(cases),
        desk=(desk.strip() if desk and desk.strip() else None),
    )
