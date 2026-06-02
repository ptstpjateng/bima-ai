"""Citizen + officer notification dispatcher (proactive WhatsApp).

Single entry point for "BIMA pings someone unprompted":
  - New case landed in officer queue   →  bimanewcase   (DISABLED — see below)
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
import re
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Literal, Sequence
from urllib.parse import urlparse

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

# ---------------------------------------------------------------------------
# Events that are registered but NOT safe to actually send.
#
# `new_case` maps to the Meta template `bimanewcase`. That template was
# Approved by Meta on 2026-05-21, BUT its approved body is wrong — the
# APTANA dashboard preview shows "⏰ Berkas {{1}} sudah {{2}} …", which is
# the bima_sla_warn copy, not a new-case message. Someone pasted the
# SLA-warn text when creating the template. Firing it would WhatsApp an
# officer an SLA-warning worded message for a brand-new case.
#
# Until the template is recreated correctly in APTANA (and re-approved),
# `notify()` short-circuits any `new_case` event with a loud warning.
# Remove "new_case" from this set ONLY after the APTANA template body is
# fixed and re-verified via the /v1/notify/test endpoint.
# ---------------------------------------------------------------------------
_DISABLED_EVENTS: set[str] = {"new_case"}


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
    # NOTE: `name` is the EXACT approved Meta template name. Meta approved
    # this one as "bimanewcase" (no underscores) — APTANA dashboard
    # 2026-05-21. The event is in _DISABLED_EVENTS above because the
    # approved body is wrong (SLA-warn copy); do not re-enable until the
    # APTANA template is recreated with the correct new-case body.
    "new_case": TemplateSpec(
        name="bimanewcase",
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
        # Approved Meta body has SIX params (verified by test-fire 2026-05-21
        # — Meta rejected a 4-param send with "The body has 6 parameters"):
        #   "🔔 Halo {{1}}, izin {{2}} ({{3}}) sudah naik tahap ke {{4}}.
        #    Estimasi selesai {{5}} hari kerja. Cek di
        #    nolongin.com/track/{{6}} kapan saja."
        # {{3}} (inline) and {{6}} (URL path) are BOTH the ticket — so
        # "ticket" appears twice in the tuple. The caller still supplies a
        # single "ticket" key; _coerce_params fills both positions from it.
        params=("name", "license_type", "ticket", "next_stage", "eta_days", "ticket"),
        freeform_body=(
            "🔔 Halo {name}, izin {license_type} ({ticket}) sudah naik "
            "tahap ke {next_stage}. Estimasi selesai {eta_days} hari kerja. "
            "Cek di nolongin.com/track/{ticket} kapan saja."
        ),
    ),
    "citizen_completed": TemplateSpec(
        name="bima_citizen_completed",
        # Approved Meta body has FOUR params (verified by test-fire 2026-05-21
        # — Meta rejected a 3-param send with "The body has 4 parameters"):
        #   "🎉 Selamat {{1}}, izin {{2}} ({{3}}) sudah TERBIT. Unduh SK
        #    ber-TTE di nolongin.com/track/{{4}} sekarang."
        # {{3}} (inline) and {{4}} (URL path) are BOTH the ticket — see the
        # citizen_progress note above.
        params=("name", "license_type", "ticket", "ticket"),
        freeform_body=(
            "🎉 Selamat {name}, izin {license_type} ({ticket}) sudah "
            "TERBIT. Unduh SK ber-TTE di nolongin.com/track/{ticket} "
            "sekarang."
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


# ---------------------------------------------------------------------------
# N3 — URL allowlist (security review 2026-05-21, finding N3).
#
# Template params can carry links (fix_url, download_url). If a param value
# ever derives from attacker-influenced data (SIAP free-text fields, citizen
# notes), a malicious URL would be delivered FROM the official BIMA WABA
# number — a turnkey phishing relay. Any param value containing a URL whose
# host is not in this set causes the send to be rejected.
# ---------------------------------------------------------------------------
_ALLOWED_URL_HOSTS: frozenset[str] = frozenset(
    {
        # Legacy hackathon domain — still live during the bimaptsp.com
        # migration window. Remove these once Caddy cuts over and SEO has
        # had time to bake (~30 days post-cutover, per Domain Migration
        # Runbook).
        "portal.nolongin.com",
        "nolongin.com",
        "beta-siap.nolongin.com",
        # New DPMPTSP-owned domain. Listed BEFORE DNS cutover so any
        # PORTAL_TRACK_URL_BASE flipped to bimaptsp.com still passes the
        # phishing-relay guard — the domain doesn't have to be live to be
        # allowlisted.
        "portal.bimaptsp.com",
        "bimaptsp.com",
        "beta-siap.bimaptsp.com",
        # SIAP production domain — stays allowlisted regardless of which
        # apex BIMA itself lives on.
        "perizinan.jatengprov.go.id",
    }
)
_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def _validate_param_urls(params: dict[str, str]) -> str | None:
    """Return an error string if any param value contains a URL whose host
    is not allowlisted; None if all URLs (if any) are BIMA-owned.

    Strips common trailing punctuation from the matched URL before parsing
    so "(...fix_url).". doesn't poison the host extraction.
    """
    for key, value in params.items():
        for raw in _URL_PATTERN.findall(str(value)):
            cleaned = raw.rstrip(").,;'\"")
            host = (urlparse(cleaned).hostname or "").lower()
            if host not in _ALLOWED_URL_HOSTS:
                return (
                    f"param '{key}' contains non-allowlisted URL host "
                    f"'{host or '<unparseable>'}'"
                )
    return None


# ---------------------------------------------------------------------------
# N2 — Rate limiting + idempotency (security review 2026-05-21, finding N2).
#
# Without these, a leaked INTERNAL_API_KEY or a buggy reconciler loop turns
# BIMA into a WhatsApp spam relay on a government WABA number — burning Meta
# messaging limits and risking a quality-rating downgrade or number ban.
#
# In-memory (per-process) is deliberate: ai-engine runs as a single
# container, so a process-local limiter is sufficient and avoids adding a
# Redis dependency to this service. If ai-engine is ever horizontally
# scaled, move this state to Redis.
# ---------------------------------------------------------------------------
_RATE_WINDOW_SECONDS = 3600.0          # 1-hour rolling window
_MAX_PER_RECIPIENT_PER_WINDOW = 10     # one number shouldn't get >10 pings/hr
_MAX_GLOBAL_PER_WINDOW = 500           # whole-system ceiling per hour
_send_ts_by_recipient: dict[str, list[float]] = defaultdict(list)
_send_ts_global: list[float] = []

# Idempotency — a retrying reconciler must not double-send the same logical
# notification. Keyed on (event, dedupe_key) — caller supplies dedupe_key
# (typically the ticket number). No dedupe_key → idempotency is skipped
# (rate limiting still applies).
_IDEMPOTENCY_WINDOW_SECONDS = 6 * 3600.0   # 6 hours
_MAX_IDEMPOTENCY_KEYS = 5000
_recent_sends: OrderedDict[str, float] = OrderedDict()


def _rate_limit_ok(recipient_phone: str) -> str | None:
    """Return None if the send is within rate limits, else a reason string.

    Checks BOTH a per-recipient cap and a global cap over a 1-hour rolling
    window. Does not record the send — `_record_send` does that, called
    only after all pre-send checks pass.
    """
    now = time.time()
    cutoff = now - _RATE_WINDOW_SECONDS

    recip = [t for t in _send_ts_by_recipient.get(recipient_phone, []) if t > cutoff]
    _send_ts_by_recipient[recipient_phone] = recip
    if len(recip) >= _MAX_PER_RECIPIENT_PER_WINDOW:
        return (
            f"per-recipient rate limit hit "
            f"({_MAX_PER_RECIPIENT_PER_WINDOW}/hr)"
        )

    global _send_ts_global
    _send_ts_global = [t for t in _send_ts_global if t > cutoff]
    if len(_send_ts_global) >= _MAX_GLOBAL_PER_WINDOW:
        return f"global rate limit hit ({_MAX_GLOBAL_PER_WINDOW}/hr)"

    return None


def _record_send(recipient_phone: str) -> None:
    """Record a send against both rate-limit counters. Call only after the
    send actually goes out."""
    now = time.time()
    _send_ts_by_recipient[recipient_phone].append(now)
    _send_ts_global.append(now)


def _idempotency_ok(event: str, dedupe_key: str | None) -> bool:
    """Return False if (event, dedupe_key) was sent within the dedupe window.

    No dedupe_key → always True (caller opted out of dedupe; rate limiting
    still protects). Evicts the oldest key when the table is full.
    """
    if not dedupe_key:
        return True
    now = time.time()
    composite = f"{event}:{dedupe_key}"
    prior = _recent_sends.get(composite)
    if prior is not None and (now - prior) < _IDEMPOTENCY_WINDOW_SECONDS:
        return False
    return True


def _record_idempotency(event: str, dedupe_key: str | None) -> None:
    """Mark (event, dedupe_key) as sent. Call only after a successful send."""
    if not dedupe_key:
        return
    composite = f"{event}:{dedupe_key}"
    _recent_sends[composite] = time.time()
    _recent_sends.move_to_end(composite)
    while len(_recent_sends) > _MAX_IDEMPOTENCY_KEYS:
        _recent_sends.popitem(last=False)


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
    dedupe_key: str | None = None,
    force: bool = False,
) -> bool:
    """Dispatch a proactive WhatsApp notification.

    Args:
        event: One of the registered EventKind values.
        recipient_phone: Any common Indonesian format — normalized inside
            whatsapp_sender/whatsapp_template. SECURITY: the caller MUST
            derive this from a trusted source (the SIAP case record /
            person_profile.mobile), never from unvalidated client input.
            See security review 2026-05-21 finding N1.
        params: Must contain every key the template's `params` declares.
            Extra keys are ignored.
        inside_session_window: When True, prefer the freeform text path
            (no template approval needed, more natural copy). When False
            or unknown, fall back to template. Until we wire a
            last-inbound timestamp store, callers default to False —
            templates always work, freeform sometimes doesn't.
        dedupe_key: Idempotency key (typically the ticket number). If a
            notification with the same (event, dedupe_key) was already
            sent within the dedupe window (6h), this call is suppressed —
            a retrying reconciler can't double-send. None opts out of
            dedupe (rate limiting still applies).
        force: TESTING ONLY. When True, bypass the BIMA_NOTIFICATIONS_ENABLED
            master flag AND the idempotency check so the internal test-fire
            endpoint can re-verify a template repeatedly. It does NOT bypass
            the _DISABLED_EVENTS guard, the rate limiter, or the URL
            allowlist — those stay enforced even under force. Real call
            sites must leave this False.

    Returns True on a 2xx from APTANA, False on any failure or when
    the feature flag is off (so callers can fire-and-forget without
    branching on flag state).
    """
    if not _is_enabled():
        if not force:
            logger.info(
                "notify suppressed (flag off) | event=%s phone=%s",
                event,
                _mask_phone(recipient_phone),
            )
            return False
        logger.warning(
            "notify FORCED past disabled flag (test-fire) | event=%s phone=%s "
            "— BIMA_NOTIFICATIONS_ENABLED is off; this send bypasses it",
            event,
            _mask_phone(recipient_phone),
        )

    spec = _TEMPLATES.get(event)
    if spec is None:
        logger.error("notify unknown event | event=%s", event)
        return False

    if event in _DISABLED_EVENTS:
        # The template for this event is registered but not safe to send.
        # See _DISABLED_EVENTS above for the rationale (currently:
        # bimanewcase has the wrong approved body in APTANA).
        logger.warning(
            "notify blocked (disabled event) | event=%s template=%s — "
            "new_case template bimanewcase has wrong body — disabled "
            "pending APTANA fix",
            event,
            spec.name,
        )
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

    # N3 — reject any param carrying a URL to a non-BIMA host BEFORE send.
    url_error = _validate_param_urls(params)
    if url_error:
        logger.error(
            "notify blocked (URL allowlist) | event=%s phone=%s — %s",
            event,
            _mask_phone(recipient_phone),
            url_error,
        )
        return False

    # N2 — idempotency: don't double-send the same logical notification.
    # force=True (test-fire) bypasses this so an operator can re-test.
    if not force and not _idempotency_ok(event, dedupe_key):
        logger.warning(
            "notify suppressed (duplicate) | event=%s dedupe_key=%s phone=%s "
            "— already sent within %dh window",
            event,
            dedupe_key,
            _mask_phone(recipient_phone),
            int(_IDEMPOTENCY_WINDOW_SECONDS // 3600),
        )
        return False

    # N2 — rate limiting always applies, even under force. A leaked
    # X-Internal-Key must not become an unbounded spam relay.
    rate_error = _rate_limit_ok(recipient_phone)
    if rate_error:
        logger.warning(
            "notify blocked (rate limit) | event=%s phone=%s — %s",
            event,
            _mask_phone(recipient_phone),
            rate_error,
        )
        return False

    if inside_session_window:
        # Freeform path — natural Indonesian copy, no template approval
        # needed. Used inside the 24h Meta session window.
        body = spec.freeform_body.format(**params)
        sent = await send_text(recipient_phone=recipient_phone, body=body)
    else:
        sent = await send_template(
            recipient_phone=recipient_phone,
            template_name=spec.name,
            body_params=positional,
        )

    if sent:
        _record_send(recipient_phone)
        _record_idempotency(event, dedupe_key)

    return sent


def list_event_kinds() -> Sequence[EventKind]:
    """Introspection helper for tests and the future admin debug page."""
    return tuple(_TEMPLATES.keys())


def describe_events() -> list[dict[str, object]]:
    """Introspection: registered events, their Meta template name, the
    declared positional param arity, and whether the event is disabled.

    Used by the internal /v1/notify/events endpoint so an operator can
    confirm template names + param tuples line up with what Meta approved.
    """
    return [
        {
            "event": event,
            "template_name": spec.name,
            "params": list(spec.params),
            "param_count": len(spec.params),
            "disabled": event in _DISABLED_EVENTS,
        }
        for event, spec in _TEMPLATES.items()
    ]


def _mask_phone(phone: str) -> str:
    if len(phone) < 8:
        return "<short>"
    return phone[:4] + "…" + phone[-4:]
