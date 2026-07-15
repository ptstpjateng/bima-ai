"""
Transparency-notification poller — proactive WhatsApp updates for citizens.

[[BIMA Vision]] req #12 (transparency notifications). Wave 2, slice 1.

SIAP shipped a state-event feed in Wave 1:

  GET /api/v1/license-request/changes?since=<log_id>&limit=<n>
      Authorization: Bearer <BIMA Sanctum token with the `events:read` ability>

It returns every state change a license request went through, newest-cursor
last, as a gap-free monotonic log:

  { "data": { "since", "next_since", "count", "has_more",
      "changes": [ { "log_id", "request_id", "license_id", "profile_id",
        "status", "stereotype", "description", "changed_at",
        "approval_step": {...}, "current_approval_step_id" }, ... ] } }

This module is the consumer half: a background asyncio loop that polls the
feed with a PERSISTED cursor, maps each change to a citizen notification
event, looks up the citizen's phone from the TRUSTED SIAP person record,
and hands it to `services.notifications.notify()`.

Design decisions
----------------
* **Mechanism** — a single background `asyncio` task on a timer, started in
  ai-engine's FastAPI `lifespan` and stopped via an `asyncio.Event`. This is
  the exact pattern admin-api uses for its ingestion status reconciler
  (`admin-api/app/services/status_reconciler.py`) — proven, no extra
  dependency (no Celery/Arq), and ai-engine runs as a single container so a
  per-process loop is sufficient.

* **Cursor persistence** — the last consumed `next_since` is written to a
  small JSON file (`TRANSPARENCY_CURSOR_PATH`, default
  `/app/data/transparency_cursor.json`). A file is the simplest robust
  option here: ai-engine has no migrations of its own and the SIAP DB is
  read-only to us, so a DB row is not available without new infrastructure.
  The file is written atomically (write-temp-then-rename) so a crash mid-write
  cannot corrupt it. On a fresh deploy with no file, the cursor starts at 0
  and SIAP returns the whole backlog — `_INITIAL_CATCHUP_CAP` bounds the
  first few ticks so a cold start cannot spam.

* **N1 (security)** — `recipient_phone` is NEVER taken from the change
  payload. It is derived server-side: change → `profile_id` → a SELECT
  against the trusted `ptsp.person_profile` table → `mobile_phone`. If no
  trusted phone is found, the change is logged (masked) and skipped — we
  never guess a recipient.

* **Idempotency** — every `notify()` call carries a `dedupe_key` of
  `"<ticket>:<log_id>"`. `log_id` is unique per change, so a change can
  never be double-sent even if a tick re-processes it after a crash before
  the cursor was persisted.

Fail-safe contract
------------------
A SIAP outage, an HTTP error, a malformed change row, or a bad person
record must NEVER crash the loop. Every tick is wrapped; every per-change
step is wrapped; the loop catches, logs, and continues. The cursor only
advances past changes that were fully processed in a tick, so a crash
mid-tick simply re-runs the unprocessed tail (dedupe makes that safe).

Feature flag
------------
`BIMA_TRANSPARENCY_POLLER_ENABLED` — default OFF. The loop task is always
created at startup but no-ops every tick until the flag is "true". This
makes the PR safe to merge before the SIAP Sanctum token is provisioned on
the VPS. The downstream `notify()` dispatcher has its OWN flag
(`BIMA_NOTIFICATIONS_ENABLED`) — both must be on for a real WhatsApp send.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

import httpx

from services.notifications import notify
from services.siap_db import get_siap_pool, is_siap_db_configured

logger = logging.getLogger("bima_ai.transparency_poller")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Reuse SIAP_API_BASE — the same SIAP host the read-only siap_client already
# talks to. The changes feed lives under /api/v1 on that host.
_SIAP_API_BASE: str = os.getenv("SIAP_API_BASE", "").rstrip("/")

# Sanctum bearer token for the changes feed. This token needs the
# `events:read` ability — distinct from SIAP_API_TOKEN (the monitoring-berkas
# token), so it gets its own env var. It is minted on Beta-SIAP via
# `php artisan bima:issue-token` (a DEPLOY step, not this code's concern).
# Blank → the poller logs once and no-ops; it never crashes.
_SIAP_EVENTS_TOKEN: str = os.getenv("SIAP_EVENTS_API_TOKEN", "").strip()

# Poll cadence. 60s is a sane default for a citizen-facing transparency feed:
# fast enough to feel "proactive", slow enough not to hammer SIAP.
_POLL_INTERVAL_SECONDS: float = float(
    os.getenv("TRANSPARENCY_POLL_INTERVAL_SECONDS", "60") or "60"
)

# How many changes to pull per request. SIAP caps it server-side; we keep it
# modest so one tick can't process an unbounded batch.
_PAGE_LIMIT: int = int(os.getenv("TRANSPARENCY_POLL_LIMIT", "50") or "50")

# Cold-start guard: on the very first run (cursor file absent) SIAP would
# replay its entire backlog. We still consume + advance the cursor, but cap
# how many changes a SINGLE tick will turn into notifications so a cold start
# cannot fan out hundreds of WhatsApp messages at once. Excess changes in the
# batch still advance the cursor (they are treated as already-historical).
_INITIAL_CATCHUP_CAP: int = int(
    os.getenv("TRANSPARENCY_INITIAL_CATCHUP_CAP", "0") or "0"
)

# Where the monotonic cursor is persisted. A JSON file, written atomically.
_CURSOR_PATH: Path = Path(
    os.getenv("TRANSPARENCY_CURSOR_PATH", "/app/data/transparency_cursor.json")
)

_HTTP_TIMEOUT_SECONDS: float = float(
    os.getenv("TRANSPARENCY_HTTP_TIMEOUT_SECONDS", "10") or "10"
)

# Portal deep-link base for the citizen_needs_fix `fix_url` param. Must be a
# host on the notifications.py URL allowlist (beta-siap.bimaptsp.com is).
_PORTAL_TRACK_URL_BASE: str = os.getenv(
    "PORTAL_TRACK_URL_BASE", "https://beta-siap.bimaptsp.com/track"
).rstrip("/")


def _is_enabled() -> bool:
    """Poller feature flag. Default OFF — flip to 'true' to arm the loop."""
    return os.getenv("BIMA_TRANSPARENCY_POLLER_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# ---------------------------------------------------------------------------
# PII masking — never log a full phone number or NIK.
# ---------------------------------------------------------------------------


def _mask_phone(phone: Optional[str]) -> str:
    if not phone:
        return "<none>"
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 8:
        return "<short>"
    return digits[:4] + "…" + digits[-3:]


# ---------------------------------------------------------------------------
# Cursor persistence — a single integer in a JSON file, written atomically.
# ---------------------------------------------------------------------------


def _load_cursor() -> int:
    """Read the persisted `next_since` cursor. Returns 0 on a fresh deploy or
    any read/parse error — a 0 cursor means "replay from the start", which the
    catch-up cap then bounds."""
    try:
        if not _CURSOR_PATH.exists():
            return 0
        raw = json.loads(_CURSOR_PATH.read_text(encoding="utf-8"))
        value = int(raw.get("next_since", 0))
        return max(0, value)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning(
            "transparency cursor unreadable (%s) — restarting from 0", exc
        )
        return 0


def _save_cursor(next_since: int) -> None:
    """Persist the cursor atomically: write a temp file in the same directory
    then `os.replace` it over the target, so a crash mid-write cannot leave a
    truncated/corrupt cursor file."""
    try:
        _CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(_CURSOR_PATH.parent), prefix=".cursor-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"next_since": int(next_since)}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, _CURSOR_PATH)
        except BaseException:
            # Clean up the temp file if the replace never happened.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        # A cursor we cannot persist is bad (next restart re-replays) but it
        # must not crash the loop. Log loudly and carry on.
        logger.error(
            "FAILED to persist transparency cursor=%s (%s) — a restart will "
            "re-process from the last saved cursor",
            next_since,
            exc,
        )


# ---------------------------------------------------------------------------
# SIAP changes-feed HTTP client
# ---------------------------------------------------------------------------


def _poller_configured() -> bool:
    """True only when both the SIAP host and the events token are present."""
    return bool(_SIAP_API_BASE and _SIAP_EVENTS_TOKEN)


async def _fetch_changes(since: int) -> Optional[dict]:
    """GET one page of the SIAP changes feed. Returns the `data` object on a
    200, or None on any HTTP/network/parse error (soft-fail — the caller skips
    the tick and retries next interval)."""
    url = f"{_SIAP_API_BASE}/api/v1/license-request/changes"
    headers = {
        "Authorization": f"Bearer {_SIAP_EVENTS_TOKEN}",
        "Accept": "application/json",
    }
    params = {"since": since, "limit": _PAGE_LIMIT}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers=headers, params=params)
    except httpx.TimeoutException:
        logger.warning("transparency feed timeout | since=%s", since)
        return None
    except httpx.RequestError as exc:
        logger.warning("transparency feed network error | since=%s | %s", since, exc)
        return None

    if resp.status_code == 401 or resp.status_code == 403:
        logger.error(
            "transparency feed auth rejected (HTTP %d) — SIAP_EVENTS_API_TOKEN "
            "is missing the `events:read` ability or is invalid/expired",
            resp.status_code,
        )
        return None
    if resp.status_code != 200:
        logger.warning(
            "transparency feed non-200 | since=%s | HTTP %d | body=%s",
            since,
            resp.status_code,
            resp.text[:200],
        )
        return None

    try:
        payload = resp.json()
    except ValueError:
        logger.warning("transparency feed returned non-JSON | since=%s", since)
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        logger.warning("transparency feed payload missing `data` object")
        return None
    return data


# ---------------------------------------------------------------------------
# Person lookup — N1: derive the recipient phone server-side from the trusted
# SIAP person record. NEVER from the change payload.
# ---------------------------------------------------------------------------

# Mirrors admin-api/app/services/siap_user_client.py — `ptsp.person_profile`
# stores contact data in a JSONB `properties` column. We only read the two
# fields a notification needs: the display name and the mobile number.
_SQL_PERSON_BY_PROFILE_ID = """
    SELECT properties ->> 'full_name'    AS full_name,
           properties ->> 'mobile_phone' AS mobile
      FROM ptsp.person_profile
     WHERE profile_id = $1
     LIMIT 1
"""


async def _lookup_person(profile_id: int) -> Optional[dict]:
    """Resolve a SIAP `profile_id` to {full_name, mobile} from the trusted
    `ptsp.person_profile` table. Returns None on not-configured / miss /
    error — the caller logs and skips the change (never guesses a recipient)."""
    if not is_siap_db_configured():
        return None
    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SQL_PERSON_BY_PROFILE_ID, int(profile_id))
    except Exception:  # pragma: no cover — defensive: never crash the loop
        logger.exception(
            "transparency person lookup failed | profile_id=%s", profile_id
        )
        return None
    if row is None:
        return None
    return {"full_name": row["full_name"], "mobile": row["mobile"]}


# ---------------------------------------------------------------------------
# Ticket + license-name resolution (for the notification params)
# ---------------------------------------------------------------------------

_SQL_TICKET_BY_REQUEST_ID = """
    SELECT properties ->> 'ticket' AS ticket
      FROM ptsp.license_request
     WHERE request_id = $1
     LIMIT 1
"""

_SQL_LICENSE_NAME_BY_ID = """
    SELECT name
      FROM ptsp.license
     WHERE license_id = $1
     LIMIT 1
"""


async def _resolve_ticket_and_license(
    request_id: Optional[int], license_id: Optional[int]
) -> tuple[Optional[str], Optional[str]]:
    """Best-effort lookup of (ticket, license_name) for a change. Either may
    come back None; the caller decides what to do with a missing ticket."""
    ticket: Optional[str] = None
    license_name: Optional[str] = None
    if not is_siap_db_configured():
        return None, None
    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            if request_id is not None:
                t_row = await conn.fetchrow(
                    _SQL_TICKET_BY_REQUEST_ID, int(request_id)
                )
                if t_row is not None and t_row["ticket"]:
                    ticket = str(t_row["ticket"]).strip()
            if license_id is not None:
                l_row = await conn.fetchrow(
                    _SQL_LICENSE_NAME_BY_ID, int(license_id)
                )
                if l_row is not None and l_row["name"]:
                    license_name = str(l_row["name"]).strip()
    except Exception:  # pragma: no cover — defensive
        logger.exception(
            "transparency ticket/license lookup failed | request_id=%s license_id=%s",
            request_id,
            license_id,
        )
    return ticket, license_name


# ---------------------------------------------------------------------------
# Change → notification-event mapping.
#
# Conservative by design (charter: "if a change doesn't clearly map, skip it,
# don't guess"). SIAP's `status` is a free-ish text field; we match on a
# normalised lowercase form against three buckets:
#
#   REJECTED   → citizen_needs_fix   (request bounced; citizen must act)
#   COMPLETED  → citizen_completed   (final approval / licence issued)
#   ADVANCED   → citizen_progress    (moved forward to a new approval stage)
#
# Anything that does not clearly land in one bucket returns None and is
# skipped. We would rather under-notify than send a wrong/confusing ping.
# ---------------------------------------------------------------------------

# Terminal-success markers — the request is DONE / the licence is issued.
_COMPLETED_TOKENS = (
    "selesai",
    "terbit",
    "diterbitkan",
    "completed",
    "complete",
    "done",
    "issued",
    "approved",       # only when it is the FINAL approval — see _classify_change
    "disetujui",
)

# Rejection markers — the request was bounced and the citizen must fix it.
_REJECTED_TOKENS = (
    "tolak",
    "ditolak",
    "rejected",
    "reject",
    "dikembalikan",   # returned to applicant
    "kembali ke pemohon",
    "perbaikan",      # needs correction
    "revisi",
)

# Progress markers — the file advanced to a new desk / approval stage.
_PROGRESS_TOKENS = (
    "proses",
    "verifikasi",
    "diproses",
    "in_progress",
    "in progress",
    "lanjut",
    "naik tahap",
    "diteruskan",
    "forwarded",
)


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _classify_change(change: dict) -> Optional[str]:
    """Map one SIAP change row to a notification event, or None to skip.

    Decision order (most-specific first):
      1. REJECTED tokens                       → citizen_needs_fix
      2. COMPLETED tokens AND it is the final  → citizen_completed
         step (no `current_approval_step_id`,
         i.e. nothing left in the chain)
      3. The change carries an `approval_step` → citizen_progress
         AND the status reads as in-progress
    Anything else → None (skip; do not guess).
    """
    status = _norm(change.get("status"))
    stereotype = _norm(change.get("stereotype"))
    description = _norm(change.get("description"))
    haystack = f"{status} {stereotype} {description}"

    # 1. Rejection — highest priority: a citizen who must act should never be
    #    told "progress" instead.
    if any(tok in haystack for tok in _REJECTED_TOKENS):
        return "citizen_needs_fix"

    # 2. Completion. A request is COMPLETE only when a terminal-success token
    #    is present AND there is no further approval step queued. SIAP signals
    #    "nothing left" with a null/0 `current_approval_step_id`.
    next_step = change.get("current_approval_step_id")
    is_final = next_step in (None, 0, "0", "")
    if is_final and any(tok in haystack for tok in _COMPLETED_TOKENS):
        return "citizen_completed"

    # 3. Progress — the change moved the file to a new approval step. We
    #    require an `approval_step` object (i.e. SIAP actually advanced a
    #    stage) AND a progress-flavoured status, so a no-op status edit does
    #    not trigger a ping.
    has_step = isinstance(change.get("approval_step"), dict) and bool(
        change.get("approval_step")
    )
    if has_step and any(tok in haystack for tok in _PROGRESS_TOKENS):
        return "citizen_progress"

    # An "approved" intermediate step (not final) is also genuine progress.
    if has_step and not is_final and any(
        tok in haystack for tok in ("approved", "disetujui", "setuju")
    ):
        return "citizen_progress"

    return None


def _stage_label(change: dict) -> str:
    """A human-readable name for the stage the request advanced to, for the
    citizen_progress `next_stage` param. Falls back gracefully."""
    step = change.get("approval_step")
    if isinstance(step, dict):
        for key in ("name", "label", "title", "description"):
            value = step.get(key)
            if value and str(value).strip():
                return str(value).strip()
    desc = change.get("description")
    if desc and str(desc).strip():
        return str(desc).strip()
    return "tahap berikutnya"


# ---------------------------------------------------------------------------
# Per-change processing
# ---------------------------------------------------------------------------


async def _process_change(change: dict) -> None:
    """Turn ONE change row into (at most) one citizen notification.

    Every failure mode here is contained: a missing field, a person miss, a
    SIAP-DB error — all just log (masked) and return. The caller's tick loop
    is never broken by a single bad row.
    """
    log_id = change.get("log_id")
    profile_id = change.get("profile_id")
    request_id = change.get("request_id")
    license_id = change.get("license_id")

    if log_id is None:
        logger.warning("transparency change missing log_id — skipped")
        return

    event = _classify_change(change)
    if event is None:
        logger.info(
            "transparency change %s not actionable (status=%r) — skipped",
            log_id,
            _norm(change.get("status")),
        )
        return

    if profile_id is None:
        logger.info(
            "transparency change %s has no profile_id — cannot resolve a "
            "trusted recipient, skipped",
            log_id,
        )
        return

    # N1 — recipient phone is derived ONLY from the trusted person record.
    person = await _lookup_person(int(profile_id))
    if not person or not person.get("mobile"):
        logger.info(
            "transparency change %s — no trusted phone for profile_id=%s "
            "(person miss or blank mobile), skipped",
            log_id,
            profile_id,
        )
        return

    recipient_phone = str(person["mobile"]).strip()
    citizen_name = (person.get("full_name") or "").strip().title() or "Bapak/Ibu"

    ticket, license_name = await _resolve_ticket_and_license(request_id, license_id)
    if not ticket:
        # Without a ticket the templates cannot render (ticket is a required
        # param for all three citizen events) — and the dedupe key would be
        # weaker. Skip rather than send a malformed message.
        logger.info(
            "transparency change %s — could not resolve a ticket "
            "(request_id=%s), skipped",
            log_id,
            request_id,
        )
        return

    license_label = license_name or "permohonan Anda"
    # dedupe_key is per-change: ticket scopes it to the request, log_id makes
    # it unique so a re-run after a crash cannot double-send.
    dedupe_key = f"{ticket}:{log_id}"

    if event == "citizen_progress":
        params = {
            "name": citizen_name,
            "license_type": license_label,
            "ticket": ticket,
            "next_stage": _stage_label(change),
            # ETA is not in the change payload; "—" is an honest placeholder
            # (the template still renders, the citizen is not misled).
            "eta_days": "—",
        }
    elif event == "citizen_completed":
        params = {
            "name": citizen_name,
            "license_type": license_label,
            "ticket": ticket,
        }
    else:  # citizen_needs_fix
        issue = (change.get("description") or "").strip()
        params = {
            "name": citizen_name,
            "ticket": ticket,
            "issue_summary": issue or "permohonan perlu diperiksa kembali",
            "fix_url": f"{_PORTAL_TRACK_URL_BASE}/{ticket}",
        }

    logger.info(
        "transparency dispatch | log_id=%s event=%s ticket=%s phone=%s",
        log_id,
        event,
        ticket,
        _mask_phone(recipient_phone),
    )

    # notify() never raises and is itself feature-flagged + rate-limited +
    # idempotent. We pass our own dedupe_key as a second idempotency layer.
    try:
        await notify(
            event=event,  # type: ignore[arg-type]  # event is a valid EventKind
            recipient_phone=recipient_phone,
            params=params,
            dedupe_key=dedupe_key,
        )
    except Exception:  # pragma: no cover — notify() shouldn't raise, belt+braces
        logger.exception(
            "transparency notify() raised unexpectedly | log_id=%s", log_id
        )


# ---------------------------------------------------------------------------
# One poll tick
# ---------------------------------------------------------------------------


async def _poll_once() -> None:
    """One poll tick: fetch a page of changes from the persisted cursor,
    process each, then advance + persist the cursor.

    Idempotent and crash-safe: the cursor only advances after the page is
    processed, and every per-change send carries a dedupe_key, so a crash
    mid-tick just re-runs the tail next interval.
    """
    if not _poller_configured():
        logger.info(
            "transparency poller skipped — SIAP_API_BASE or "
            "SIAP_EVENTS_API_TOKEN not configured"
        )
        return

    since = _load_cursor()
    data = await _fetch_changes(since)
    if data is None:
        return  # soft-fail already logged; retry next interval

    changes = data.get("changes")
    if not isinstance(changes, list) or not changes:
        return  # nothing new

    next_since = data.get("next_since")
    count = len(changes)

    # Cold-start guard: if the persisted cursor was 0 (fresh deploy) the feed
    # may replay a large backlog. Only turn the FIRST `_INITIAL_CATCHUP_CAP`
    # of them into notifications; the rest still advance the cursor (treated
    # as already-historical) so we never replay them again.
    cold_start = since == 0
    if cold_start and count > _INITIAL_CATCHUP_CAP:
        logger.warning(
            "transparency cold start — %d backlog changes, notifying only "
            "the first %d, fast-forwarding the cursor past the rest",
            count,
            _INITIAL_CATCHUP_CAP,
        )
        to_process = changes[:_INITIAL_CATCHUP_CAP]
    else:
        to_process = changes

    processed = 0
    for change in to_process:
        if not isinstance(change, dict):
            logger.warning("transparency change is not an object — skipped")
            continue
        try:
            await _process_change(change)
            processed += 1
        except Exception:  # pragma: no cover — a bad row must not stop the tick
            logger.exception("transparency change processing crashed — continuing")

    # Advance the cursor. Prefer SIAP's authoritative `next_since`; fall back
    # to the max log_id we actually saw if the field is absent/garbage.
    advanced_to: Optional[int] = None
    if isinstance(next_since, int) and next_since >= since:
        advanced_to = next_since
    else:
        log_ids = [
            int(c["log_id"])
            for c in changes
            if isinstance(c, dict) and isinstance(c.get("log_id"), int)
        ]
        if log_ids:
            advanced_to = max(log_ids)

    if advanced_to is not None and advanced_to != since:
        _save_cursor(advanced_to)
        logger.info(
            "transparency tick done | processed=%d/%d | cursor %s → %s | "
            "has_more=%s",
            processed,
            count,
            since,
            advanced_to,
            data.get("has_more"),
        )


# ---------------------------------------------------------------------------
# Background loop — wired into ai-engine's FastAPI lifespan.
# ---------------------------------------------------------------------------


async def run_transparency_poller_loop(stop_event: asyncio.Event) -> None:
    """Background loop: poll the SIAP changes feed every interval until
    `stop_event` is set.

    Always safe to start: when the feature flag is OFF, every tick no-ops
    immediately. A crashing tick is caught and logged — the loop never dies.
    """
    logger.info(
        "transparency poller loop started | interval=%.0fs | enabled=%s | "
        "cursor_file=%s",
        _POLL_INTERVAL_SECONDS,
        _is_enabled(),
        _CURSOR_PATH,
    )
    while not stop_event.is_set():
        try:
            if _is_enabled():
                await _poll_once()
        except Exception:  # pragma: no cover — last-resort guard
            logger.exception("transparency poller tick crashed — loop continues")
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=_POLL_INTERVAL_SECONDS
            )
        except asyncio.TimeoutError:
            pass  # normal tick interval
    logger.info("transparency poller loop stopped")
