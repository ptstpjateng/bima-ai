"""Citizen + officer notification dispatcher (proactive, multi-channel).

Single entry point for "BIMA pings someone unprompted":
  - New case landed in officer queue   →  bimanewcase   (DISABLED — see below)
  - SLA at risk                        →  bima_sla_warn
  - Citizen progress notification      →  bima_citizen_progress
  - Citizen license issued             →  bima_citizen_completed
  - Citizen needs correction           →  bima_citizen_needs_fix

Channel routing (Vision reqs #7 + #12 — multi-channel dispatch).
-----------------------------------------------------------------
Meta disabled the DPMPTSP WhatsApp Business Account on 2026-05-21 (review
pending). Until that clears we still need transparency notifications to
work — so this dispatcher accepts a `channel` hint:

  - APTANA_WA  →  APTANA WhatsApp (the historical path; freeform-or-
                  template depending on 24h session window).
  - TELEGRAM   →  Telegram bot via services.telegram_sender.send_text.
                  Telegram has no Meta template approval — it accepts
                  free-form Indonesian text directly. The TemplateSpec
                  carries an optional `telegram_body` for cases where the
                  copy should differ from the WhatsApp freeform body;
                  otherwise the freeform body is reused.

Recipient resolution per channel:
  - APTANA_WA  →  `recipient_phone`  (E.164-ish Indonesian)
  - TELEGRAM   →  `recipient_telegram_chat_id` (int from `chat.id` in the
                  Telegram update)

For the demo, the caller (the transparency poller) decides the channel
based on whether it knows a Telegram chat_id for this citizen. There is
no DB-backed citizen↔channel table yet — see transparency_poller.py for
the env-pinned interim mapping. Following the demo we will replace that
with a proper citizens table; until then this is a deliberate scope cut.

The 24-hour Meta session window only applies to APTANA_WA:
  - INSIDE the window  →  freeform text via whatsapp_sender.send_text()
  - OUTSIDE the window →  approved template is the only legal path

Feature-flagged via BIMA_NOTIFICATIONS_ENABLED. Default OFF — flipping
the env to "true" arms the dispatcher. The flag is CHANNEL-AGNOSTIC: it
gates Telegram sends too, so during the Meta review window a single env
flip still controls every proactive ping BIMA emits.

Decisions §20 documents the template strategy + approval timeline.
"""

from __future__ import annotations

import enum
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


class Channel(str, enum.Enum):
    """Outbound transport selector for a notification.

    A str-Enum (not a plain Enum) so it serializes naturally over JSON
    and round-trips through Pydantic without extra config.
    """

    APTANA_WA = "aptana_wa"
    TELEGRAM = "telegram"

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
    """Approved Meta template + the freeform fallback copy + Telegram copy.

    `params` enforces positional-parameter arity at call time so we
    can't drift away from what Meta approved.

    `telegram_body` is OPTIONAL Telegram-specific copy. Telegram has no
    template approval — anything goes — so we can write a slightly more
    natural Indonesian message that uses Telegram conventions (Markdown,
    emoji, longer copy). When None, the WhatsApp freeform body is reused
    verbatim, which is also fine because the freeform copy was authored
    to read well as plain text anyway.
    """

    name: str
    params: tuple[str, ...]
    freeform_body: str  # uses {param} placeholders matching `params`
    telegram_body: str | None = None  # Telegram-specific copy (optional)


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
        telegram_body=(
            "🔔 Halo {name}, izin {license_type} ({ticket}) sudah naik "
            "tahap ke {next_stage}.\n"
            "Estimasi selesai {eta_days} hari kerja.\n\n"
            "Cek status: https://portal.nolongin.com/track/{ticket}"
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
        telegram_body=(
            "🎉 Selamat {name}!\n"
            "Izin {license_type} ({ticket}) sudah TERBIT.\n\n"
            "Unduh SK ber-TTE: https://portal.nolongin.com/track/{ticket}"
        ),
    ),
    "citizen_needs_fix": TemplateSpec(
        name="bima_citizen_needs_fix",
        params=("name", "ticket", "issue_summary", "fix_url"),
        freeform_body=(
            "⚠️ Pak/Bu {name}, permohonan {ticket} perlu koreksi: "
            "{issue_summary}. Klik {fix_url} untuk perbaiki."
        ),
        telegram_body=(
            "⚠️ Pak/Bu {name}, permohonan {ticket} perlu koreksi.\n\n"
            "Catatan: {issue_summary}\n\n"
            "Perbaiki di: {fix_url}"
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
        "portal.nolongin.com",
        "nolongin.com",
        "beta-siap.nolongin.com",
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


def _rate_limit_ok(rate_key: str) -> str | None:
    """Return None if the send is within rate limits, else a reason string.

    `rate_key` is channel-prefixed by the caller — "wa:<phone>" or
    "tg:<chat_id>" — so a citizen reachable on both transports can still
    receive the full per-channel quota.

    Checks BOTH a per-recipient cap and a global cap over a 1-hour rolling
    window. Does not record the send — `_record_send` does that, called
    only after all pre-send checks pass.
    """
    now = time.time()
    cutoff = now - _RATE_WINDOW_SECONDS

    recip = [t for t in _send_ts_by_recipient.get(rate_key, []) if t > cutoff]
    _send_ts_by_recipient[rate_key] = recip
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


def _record_send(rate_key: str) -> None:
    """Record a send against both rate-limit counters. Call only after the
    send actually goes out. `rate_key` is the channel-prefixed identifier
    the limiter checked — keep the two in sync."""
    now = time.time()
    _send_ts_by_recipient[rate_key].append(now)
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


# ---------------------------------------------------------------------------
# Channel-specific send paths.
#
# These take an already-coerced positional tuple + a kwargs dict (for
# freeform-body templating) and a recipient identifier appropriate to the
# channel. They are the ONLY places that touch the underlying transport
# modules — `notify()` itself stays channel-agnostic so adding a new
# channel later is one new helper, not a fork in the main flow.
# ---------------------------------------------------------------------------


async def _send_aptana_wa(
    *,
    spec: TemplateSpec,
    positional: tuple[str, ...],
    params: dict[str, str],
    recipient_phone: str,
    inside_session_window: bool,
) -> bool:
    """Send via APTANA WhatsApp. Inside the 24h Meta session window we may
    use freeform text; otherwise the approved template is the only legal
    path."""
    if inside_session_window:
        body = spec.freeform_body.format(**params)
        return await send_text(recipient_phone=recipient_phone, body=body)
    return await send_template(
        recipient_phone=recipient_phone,
        template_name=spec.name,
        body_params=positional,
    )


async def _send_telegram(
    *,
    spec: TemplateSpec,
    params: dict[str, str],
    chat_id: int | str,
) -> bool:
    """Send via Telegram. Telegram has no template approval — anything
    goes — so we use `telegram_body` if set, otherwise the freeform body
    verbatim. The import is deferred so this module still imports cleanly
    in environments where the telegram channel hasn't been provisioned
    yet (e.g. a stripped-down dev container)."""
    try:
        # Lazy import: keeps this module's import-time clean if
        # telegram_sender is missing (e.g. an old branch); the failure
        # only happens when Telegram is actually requested.
        from services import telegram_sender  # type: ignore[import-not-found]
    except ImportError:
        logger.error(
            "Telegram send refused | services.telegram_sender unavailable "
            "(channel not provisioned in this build)"
        )
        return False

    body_template = spec.telegram_body or spec.freeform_body
    body = body_template.format(**params)
    return await telegram_sender.send_text(chat_id=chat_id, body=body)


async def notify(
    *,
    event: EventKind,
    params: dict[str, str],
    recipient_phone: str | None = None,
    recipient_telegram_chat_id: int | str | None = None,
    channel: Channel | None = None,
    inside_session_window: bool = False,
    dedupe_key: str | None = None,
    force: bool = False,
) -> bool:
    """Dispatch one proactive notification through the chosen channel.

    Args:
        event: One of the registered EventKind values.
        params: Must contain every key the template's `params` declares.
            Extra keys are ignored.
        recipient_phone: Required when `channel=APTANA_WA`. Any common
            Indonesian format — normalized inside whatsapp_sender /
            whatsapp_template. SECURITY: the caller MUST derive this from
            a trusted source (the SIAP case record / person_profile.mobile),
            never from unvalidated client input. See security review
            2026-05-21 finding N1.
        recipient_telegram_chat_id: Required when `channel=TELEGRAM`. The
            Telegram `chat.id` (int). For citizens this is captured when
            they first message the bot — see routers/telegram.py.
        channel: Which transport to use. If None, the channel is inferred:
            a telegram chat_id alone → TELEGRAM; a phone alone → APTANA_WA.
            Supplying both with no `channel` is an error — pass channel
            explicitly to disambiguate.
        inside_session_window: APTANA-only. When True, prefer the freeform
            text path (no template approval needed). When False or
            unknown, fall back to template. Ignored for Telegram (no such
            concept on Telegram).
        dedupe_key: Idempotency key (typically the ticket number). If a
            notification with the same (event, dedupe_key) was already
            sent within the dedupe window (6h), this call is suppressed —
            a retrying reconciler can't double-send. None opts out of
            dedupe (rate limiting still applies). Channel-agnostic on
            purpose: re-sending the same logical update over a different
            transport is still a duplicate from the citizen's POV.
        force: TESTING ONLY. When True, bypass the BIMA_NOTIFICATIONS_ENABLED
            master flag AND the idempotency check so the internal test-fire
            endpoint can re-verify a template repeatedly. It does NOT bypass
            the _DISABLED_EVENTS guard, the rate limiter, or the URL
            allowlist — those stay enforced even under force. Real call
            sites must leave this False.

    Returns True on a successful send (2xx from the underlying transport),
    False on any failure or when the feature flag is off (so callers can
    fire-and-forget without branching on flag state).
    """
    # ------------------------------------------------------------------
    # Resolve channel + recipient identity.
    #
    # The "rate-limit key" we use throughout is channel-prefixed so a
    # citizen who is reachable on BOTH transports can still get 10
    # legitimate WhatsApps AND 10 Telegrams an hour without one
    # cannibalising the other's quota — but each transport stays bounded
    # in case a leaked credential becomes a spam relay.
    # ------------------------------------------------------------------
    resolved_channel = channel
    if resolved_channel is None:
        if recipient_telegram_chat_id is not None and recipient_phone is None:
            resolved_channel = Channel.TELEGRAM
        elif recipient_phone is not None and recipient_telegram_chat_id is None:
            resolved_channel = Channel.APTANA_WA
        elif recipient_phone is None and recipient_telegram_chat_id is None:
            logger.error(
                "notify no recipient | event=%s — neither phone nor "
                "telegram_chat_id supplied",
                event,
            )
            return False
        else:
            logger.error(
                "notify ambiguous channel | event=%s — both phone and "
                "telegram_chat_id supplied with channel=None; pass channel "
                "explicitly to disambiguate",
                event,
            )
            return False

    if resolved_channel == Channel.APTANA_WA:
        if not recipient_phone:
            logger.error(
                "notify missing phone | event=%s channel=APTANA_WA", event
            )
            return False
        rate_key = f"wa:{recipient_phone}"
        masked_recipient = _mask_phone(recipient_phone)
    elif resolved_channel == Channel.TELEGRAM:
        if recipient_telegram_chat_id is None:
            logger.error(
                "notify missing chat_id | event=%s channel=TELEGRAM", event
            )
            return False
        rate_key = f"tg:{recipient_telegram_chat_id}"
        masked_recipient = _mask_chat(recipient_telegram_chat_id)
    else:  # pragma: no cover — Channel is a closed enum
        logger.error("notify unknown channel | event=%s channel=%s", event, resolved_channel)
        return False

    # ------------------------------------------------------------------
    # Master feature flag — channel-agnostic on purpose. During the Meta
    # review window we want a single env flip to silence EVERY proactive
    # ping BIMA emits, regardless of transport.
    # ------------------------------------------------------------------
    if not _is_enabled():
        if not force:
            logger.info(
                "notify suppressed (flag off) | event=%s channel=%s recipient=%s",
                event, resolved_channel.value, masked_recipient,
            )
            return False
        logger.warning(
            "notify FORCED past disabled flag (test-fire) | event=%s "
            "channel=%s recipient=%s — BIMA_NOTIFICATIONS_ENABLED is off",
            event, resolved_channel.value, masked_recipient,
        )

    spec = _TEMPLATES.get(event)
    if spec is None:
        logger.error("notify unknown event | event=%s", event)
        return False

    if event in _DISABLED_EVENTS:
        logger.warning(
            "notify blocked (disabled event) | event=%s template=%s — "
            "new_case template bimanewcase has wrong body — disabled "
            "pending APTANA fix",
            event, spec.name,
        )
        return False

    try:
        positional = _coerce_params(spec, params)
    except KeyError as missing:
        logger.error(
            "notify missing param | event=%s missing=%s supplied=%s",
            event, missing, sorted(params.keys()),
        )
        return False

    # N3 — URL allowlist applies on EVERY channel. A non-BIMA URL in a
    # Telegram message is just as effective a phishing relay as in a
    # WhatsApp message — same trust-of-sender problem.
    url_error = _validate_param_urls(params)
    if url_error:
        logger.error(
            "notify blocked (URL allowlist) | event=%s channel=%s recipient=%s — %s",
            event, resolved_channel.value, masked_recipient, url_error,
        )
        return False

    # N2 — idempotency: don't double-send the same logical notification.
    # force=True (test-fire) bypasses this so an operator can re-test.
    if not force and not _idempotency_ok(event, dedupe_key):
        logger.warning(
            "notify suppressed (duplicate) | event=%s dedupe_key=%s "
            "channel=%s recipient=%s — already sent within %dh window",
            event, dedupe_key, resolved_channel.value, masked_recipient,
            int(_IDEMPOTENCY_WINDOW_SECONDS // 3600),
        )
        return False

    # N2 — rate limiting always applies, even under force. A leaked
    # credential must not become an unbounded spam relay.
    rate_error = _rate_limit_ok(rate_key)
    if rate_error:
        logger.warning(
            "notify blocked (rate limit) | event=%s channel=%s recipient=%s — %s",
            event, resolved_channel.value, masked_recipient, rate_error,
        )
        return False

    # ------------------------------------------------------------------
    # Dispatch through the channel-specific send path.
    # ------------------------------------------------------------------
    if resolved_channel == Channel.APTANA_WA:
        assert recipient_phone is not None  # checked above; for type-checkers
        sent = await _send_aptana_wa(
            spec=spec,
            positional=positional,
            params=params,
            recipient_phone=recipient_phone,
            inside_session_window=inside_session_window,
        )
    else:  # Channel.TELEGRAM
        assert recipient_telegram_chat_id is not None
        sent = await _send_telegram(
            spec=spec,
            params=params,
            chat_id=recipient_telegram_chat_id,
        )

    if sent:
        _record_send(rate_key)
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


def _mask_chat(chat_id: int | str) -> str:
    """Masked Telegram chat_id for log lines. Mirrors telegram_sender's
    masking shape — `chat.id` resolves to a real user identity, so we
    only ever log the first 2 + last 3 digits."""
    s = str(chat_id)
    if len(s) <= 5:
        return "…" + s[-2:]
    return s[:2] + "…" + s[-3:]
