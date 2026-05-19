"""Citizen + officer notification dispatcher (proactive WhatsApp).

Single entry point for "BIMA pings someone unprompted":
  - New case landed in officer queue   →  bima_new_case
  - SLA at risk                        →  bima_sla_warn
  - Citizen progress notification      →  bima_citizen_progress
  - Citizen license issued             →  bima_citizen_completed
  - Citizen needs correction           →  bima_citizen_needs_fix

The 24-hour Meta session window forces a two-mode design:
  - INSIDE the window  →  freeform text via send_text() is allowed and
    preferred (no template approval needed, more natural copy)
  - OUTSIDE the window →  approved template is the only legal path

This dispatcher tries the freeform path when a fresh inbound is on
record (TODO: track per-recipient last-inbound timestamp once we wire
the message store) and falls back to template otherwise.

Feature-flagged via BIMA_NOTIFICATIONS_ENABLED. Default OFF — flipping
the env to "true" arms the dispatcher. Until Meta approves all five
templates, leaving it OFF prevents an embarrassing 132xxx error in
prod the moment a notification fires.

Decisions §20 documents the template strategy + approval timeline.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal, Sequence

from services.whatsapp_sender import send_text
from services.whatsapp_template import send_template

logger = logging.getLogger(__name__)

EventKind = Literal[
    "new_case",
    "sla_warn",
    "citizen_progress",
    "citizen_completed",
    "citizen_needs_fix",
]


@dataclass(frozen=True)
class TemplateSpec:
    """Approved Meta template + the freeform fallback copy.

    `params` enforces positional-parameter arity at call time so we
    can't drift away from what Meta approved.
    """

    name: str
    params: tuple[str, ...]
    freeform_body: str  # uses {param} placeholders matching `params`


# ---------------------------------------------------------------------------
# Template registry.
#
# Names + parameter shapes are the lowercase_snake_case versions submitted
# to Meta (per APTANA dashboard screenshot 2026-05-19). Keep this table
# in lock-step with what's actually approved — if a template gets rejected
# and we resubmit with a tweaked name, update HERE before re-arming the
# flag, or sends will silently 400.
# ---------------------------------------------------------------------------
_TEMPLATES: dict[EventKind, TemplateSpec] = {
    "new_case": TemplateSpec(
        name="bima_new_case",
        params=("ticket",),
        freeform_body=(
            "📋 Berkas {ticket} baru di meja Anda. "
            "Ketik 'buka' untuk lihat detail."
        ),
    ),
    "sla_warn": TemplateSpec(
        name="bima_sla_warn",
        params=("ticket", "days_open", "sop_days"),
        freeform_body=(
            "⏰ Berkas {ticket} sudah {days_open} hari kerja, "
            "mendekati SOP ({sop_days} hari). Mohon diprioritaskan."
        ),
    ),
    "citizen_progress": TemplateSpec(
        name="bima_citizen_progress",
        params=("name", "license_type", "next_stage", "eta_days"),
        freeform_body=(
            "🔔 Pak/Bu {name}, izin {license_type} naik tahap ke "
            "{next_stage}. Estimasi selesai {eta_days} hari kerja."
        ),
    ),
    "citizen_completed": TemplateSpec(
        name="bima_citizen_completed",
        params=("name", "license_type", "download_url"),
        freeform_body=(
            "🎉 Selamat Pak/Bu {name}, izin {license_type} sudah terbit. "
            "Unduh SK di: {download_url}"
        ),
    ),
    "citizen_needs_fix": TemplateSpec(
        name="bima_citizen_needs_fix",
        params=("name", "ticket", "issue_summary", "fix_url"),
        freeform_body=(
            "⚠️ Pak/Bu {name}, permohonan {ticket} perlu koreksi: "
            "{issue_summary}. Klik {fix_url} untuk perbaiki."
        ),
    ),
}


def _is_enabled() -> bool:
    """Master feature flag. Default OFF — flip to 'true' to arm."""
    return os.getenv("BIMA_NOTIFICATIONS_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _coerce_params(spec: TemplateSpec, params: dict[str, str]) -> tuple[str, ...]:
    """Project a kwargs dict onto the template's positional arity.

    Raises KeyError if a required parameter is missing — callers should
    always supply every key in `spec.params`. Missing keys are a bug,
    not an edge case to default-paper-over (Meta will 400 anyway).
    """
    return tuple(str(params[k]) for k in spec.params)


async def notify(
    *,
    event: EventKind,
    recipient_phone: str,
    params: dict[str, str],
    inside_session_window: bool = False,
) -> bool:
    """Dispatch a proactive WhatsApp notification.

    Args:
        event: One of the registered EventKind values.
        recipient_phone: Any common Indonesian format — normalized inside
            whatsapp_sender/whatsapp_template.
        params: Must contain every key the template's `params` declares.
            Extra keys are ignored.
        inside_session_window: When True, prefer the freeform text path
            (no template approval needed, more natural copy). When False
            or unknown, fall back to template. Until we wire a
            last-inbound timestamp store, callers default to False —
            templates always work, freeform sometimes doesn't.

    Returns True on a 2xx from APTANA, False on any failure or when
    the feature flag is off (so callers can fire-and-forget without
    branching on flag state).
    """
    if not _is_enabled():
        logger.info(
            "notify suppressed (flag off) | event=%s phone=%s",
            event,
            _mask_phone(recipient_phone),
        )
        return False

    spec = _TEMPLATES.get(event)
    if spec is None:
        logger.error("notify unknown event | event=%s", event)
        return False

    try:
        positional = _coerce_params(spec, params)
    except KeyError as missing:
        logger.error(
            "notify missing param | event=%s missing=%s supplied=%s",
            event,
            missing,
            sorted(params.keys()),
        )
        return False

    if inside_session_window:
        # Freeform path — natural Indonesian copy, no template approval
        # needed. Used inside the 24h Meta session window.
        body = spec.freeform_body.format(**params)
        return await send_text(recipient_phone=recipient_phone, body=body)

    return await send_template(
        recipient_phone=recipient_phone,
        template_name=spec.name,
        body_params=positional,
    )


def list_event_kinds() -> Sequence[EventKind]:
    """Introspection helper for tests and the future admin debug page."""
    return tuple(_TEMPLATES.keys())


def _mask_phone(phone: str) -> str:
    if len(phone) < 8:
        return "<short>"
    return phone[:4] + "…" + phone[-4:]
