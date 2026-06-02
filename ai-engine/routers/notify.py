"""
Internal notification test-fire + introspection surface.

POST /v1/notify/test
  Fire ONE proactive WhatsApp notification on demand, straight through
  `services.notifications.notify`. Built for the go-live verification
  step: now that Meta has approved the five BIMA templates, an operator
  needs to send each one to their OWN phone to confirm (a) the template
  body renders correctly and (b) the `{{N}}` parameter count matches the
  TemplateSpec.params arity we declared in code. The APTANA dashboard
  truncates the body preview, so this empirical check is the only
  reliable way to verify arity before the cutover.

GET /v1/notify/events
  List the registered event kinds with their Meta template name, the
  declared param tuple, and whether the event is disabled. Lets the
  operator eyeball the registry without reading source.

Auth: gated by X-Internal-Key — mirrors the validator and copilot
routers exactly. This endpoint is internal-only and MUST NOT be exposed
to citizens: it can send WhatsApp messages to an arbitrary number.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from deps import require_internal_key
from services import notifications
from services.notifications import EventKind

logger = logging.getLogger("bima_ai.notify")

router = APIRouter(prefix="/v1/notify", tags=["Notifications"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class NotifyTestRequest(BaseModel):
    event: EventKind = Field(
        ...,
        description=(
            "Which registered notification event to fire. One of: "
            "new_case, sla_warn, citizen_progress, citizen_completed, "
            "citizen_needs_fix. Note: new_case is currently disabled "
            "(see /v1/notify/events) and will be refused even with force."
        ),
    )
    recipient_phone: str = Field(
        ...,
        min_length=8,
        max_length=20,
        description=(
            "Destination phone in any common Indonesian format — "
            "normalized downstream. For a test-fire, use your OWN number."
        ),
    )
    params: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Positional template parameters keyed by name. Must contain "
            "every key the event's TemplateSpec declares — see "
            "/v1/notify/events for the exact param list per event."
        ),
    )


class NotifyTestResponse(BaseModel):
    event: EventKind
    sent: bool = Field(..., description="True only on a 2xx from APTANA.")
    forced: bool = Field(
        ...,
        description="True if the master feature flag was bypassed for this send.",
    )
    detail: str = Field(
        ...,
        description=(
            "Human-readable outcome. On failure, check the ai-engine logs "
            "for the APTANA error body / Meta 132xxx code."
        ),
    )


class EventInfo(BaseModel):
    event: str
    template_name: str
    params: list[str]
    param_count: int
    disabled: bool


class EventsResponse(BaseModel):
    events: list[EventInfo]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/test",
    response_model=NotifyTestResponse,
    summary="Fire one proactive WhatsApp notification (internal test-fire)",
)
async def notify_test_endpoint(
    body: NotifyTestRequest,
    _internal: Annotated[bool, Depends(require_internal_key)],
    force: Annotated[
        bool,
        Query(
            description=(
                "TESTING ONLY. When true, bypass BIMA_NOTIFICATIONS_ENABLED "
                "so a template can be verified before go-live. Logged loudly. "
                "Does NOT bypass the disabled-event guard."
            ),
        ),
    ] = False,
) -> NotifyTestResponse:
    """Send one notification immediately and report the boolean outcome.

    Unlike the real call sites, this awaits the send (no fire-and-forget)
    so the operator gets the pass/fail result synchronously. `notify()`
    never raises, so any APTANA/Meta failure surfaces as sent=False with
    the error detail written to the ai-engine logs.
    """
    if force:
        logger.warning(
            "notify/test FORCE flag used | event=%s — bypassing "
            "BIMA_NOTIFICATIONS_ENABLED for a test-fire",
            body.event,
        )

    logger.info(
        "notify/test request | event=%s force=%s param_keys=%s",
        body.event,
        force,
        sorted(body.params.keys()),
    )

    sent = await notifications.notify(
        event=body.event,
        recipient_phone=body.recipient_phone,
        params=body.params,
        force=force,
    )

    if sent:
        detail = (
            f"Notification '{body.event}' dispatched to APTANA (2xx). "
            "Check the recipient's WhatsApp to verify the template body "
            "and parameter arity."
        )
    else:
        detail = (
            f"Notification '{body.event}' was NOT sent. Likely causes: "
            "the master flag is off and force was not set; the event is "
            "disabled (see /v1/notify/events); a required param is missing; "
            "or APTANA/Meta rejected the send. See ai-engine logs for the "
            "exact reason (incl. any Meta 132xxx code)."
        )

    return NotifyTestResponse(
        event=body.event,
        sent=sent,
        forced=force,
        detail=detail,
    )


@router.get(
    "/events",
    response_model=EventsResponse,
    summary="List registered notification events, template names and arity",
)
async def notify_events_endpoint(
    _internal: Annotated[bool, Depends(require_internal_key)],
) -> EventsResponse:
    """Introspect the notification registry — handy for confirming each
    event's Meta template name and declared param tuple before go-live."""
    return EventsResponse(
        events=[EventInfo(**row) for row in notifications.describe_events()]
    )


@router.get(
    "/delivery/{ticket}",
    summary="WhatsApp delivery status (sent/delivered/read) for a ticket's notifications",
)
async def notify_delivery_endpoint(
    ticket: str,
    _internal: Annotated[bool, Depends(require_internal_key)],
) -> dict:
    """Return the delivery lifecycle of notifications BIMA sent for `ticket`.

    Powers the admin/case "✓✓ Pemohon membaca notifikasi" surface. Each record:
        {event, status: sent|delivered|read|failed, phone (masked),
         sent_at, status_at}
    `status_at` for a 'read' record is when the citizen read the notification.
    Returns an empty list if no notification was sent (or it predates tracking).
    """
    from services import delivery_tracker

    records = delivery_tracker.get_by_ticket(ticket)
    return {
        "ticket": ticket,
        "count": len(records),
        "records": [
            {
                "event": r.get("event"),
                "status": r.get("status"),
                "phone": r.get("phone_masked"),
                "sent_at": r.get("sent_at"),
                "status_at": r.get("status_at"),
            }
            for r in records
        ],
    }
