"""Auto-resend a WhatsApp *document* on a transient Meta delivery failure.

BIMA sends a document (a `/dl/{token}` link APTANA fetches + delivers) via
`whatsapp_sender.send_document`. That call returns True once APTANA ACCEPTS the
POST — but Meta delivers the media asynchronously and can fail LATER with a
transient error, reported through the status webhook (routers/aptana.py). The
one we've seen live is Meta code **131053** ("media upload error") — a Meta-side
media fetch/upload glitch that a plain re-send clears (a rehearsal confirmed a
manual re-send delivered fine seconds later). This module auto-resends so the
officer/citizen doesn't have to re-ask.

Correlation
-----------
`send_document` does not capture Meta's message id (wamid), so — exactly like
`delivery_tracker` keys read-receipts by phone — we correlate the failed status
to the original send by RECIPIENT (digits) and resend the MOST RECENT document
sent to that recipient.

Loop safety (critical)
----------------------
A resend re-calls `send_document`, which re-`register()`s the SAME link. So
`register()` KEEPS the retry counter when the link is unchanged and resets it to
0 ONLY for a genuinely new document. `maybe_resend()` increments the counter
BEFORE scheduling and refuses once it hits `_MAX_RETRIES`. Net: at most
`_MAX_RETRIES` resends per document — total `/dl` fetches stay within the token's
~5-fetch budget, and a repeated 131053 can never loop forever.

Never raises; never logs the download token, filename, caption, or full phone —
only a masked recipient + the Meta code + the retry count.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Any, Optional

logger = logging.getLogger(__name__)

# --- Tunables (env-overridable, no redeploy to flip) -----------------------
_MAX_RETRIES = int(os.getenv("BIMA_DOC_RESEND_MAX", "2") or "2")
_BACKOFF_SECONDS = float(os.getenv("BIMA_DOC_RESEND_BACKOFF", "4") or "4")


def _parse_codes(raw: str) -> set[int]:
    out: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out or {131053}


# Meta delivery error codes worth a transient resend. 131053 = "media upload
# error" (Meta-side, transient). NOT 131047 (24h re-engagement — a template is
# the only fix, resend won't help) or hard errors.
_RETRYABLE_CODES: set[int] = _parse_codes(os.getenv("BIMA_DOC_RESEND_CODES", "131053"))

_MAX_RECORDS = int(os.getenv("BIMA_DOC_RESEND_MAX_RECORDS", "1000") or "1000")

# digits(recipient) -> {"link","filename","caption","sent_at","retries"}
_records: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

# Strong refs to in-flight resend tasks (the loop only weakly references them).
_resend_tasks: set[asyncio.Task] = set()


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _mask(phone: str) -> str:
    d = _digits(phone)
    return d[:4] + "…" + d[-3:] if len(d) >= 7 else "<short>"


def register(
    recipient: str, link: str, filename: str, caption: Optional[str] = None
) -> None:
    """Record the document just sent to `recipient` so a later transient failure
    can be auto-resent. Call ONLY on a successful send.

    Loop safety: a re-register with the SAME link (i.e. a resend) KEEPS the retry
    counter; a DIFFERENT link (a new document) resets it to 0. Never raises; the
    link/filename/caption are stored but NEVER logged."""
    try:
        key = _digits(recipient)
        if not key or not link:
            return
        prev = _records.get(key)
        retries = prev["retries"] if (prev and prev.get("link") == link) else 0
        _records[key] = {
            "link": link,
            "filename": filename or "",
            "caption": caption,
            "sent_at": time.time(),
            "retries": retries,
        }
        _records.move_to_end(key)
        while len(_records) > _MAX_RECORDS:
            _records.popitem(last=False)
    except Exception:  # pragma: no cover — a tracker glitch must never break a send
        logger.exception("doc_resend.register failed (non-fatal)")


def maybe_resend(recipient: str, error_codes: Optional[list]) -> bool:
    """On a Meta `failed` status, resend the recipient's last document IFF the
    failure carries a retryable code (default 131053) and the retry cap allows.

    Returns True when a resend was scheduled. Best-effort: never raises. Schedules
    a fire-and-forget task (backoff then send_document); on no running loop it
    logs and returns False."""
    try:
        codes = {int(c) for c in (error_codes or []) if c is not None and str(c).lstrip("-").isdigit()}
        if not codes & _RETRYABLE_CODES:
            return False
        key = _digits(recipient)
        rec = _records.get(key)
        if not rec:
            return False
        if rec.get("retries", 0) >= _MAX_RETRIES:
            logger.warning(
                "doc_resend giving up | to=%s code=%s retries=%d (max=%d)",
                _mask(recipient), sorted(codes & _RETRYABLE_CODES),
                rec.get("retries", 0), _MAX_RETRIES,
            )
            return False
        # Confirm a running loop BEFORE building the coroutine, so a no-loop path
        # never leaves an un-awaited coroutine (nor counts a retry that never
        # scheduled). Shouldn't happen on the async webhook path.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "doc_resend could not schedule (no loop) | to=%s", _mask(recipient)
            )
            return False

        link = rec["link"]
        filename = rec.get("filename", "")
        caption = rec.get("caption")
        attempt = rec.get("retries", 0) + 1
        task = asyncio.create_task(
            _resend_after_backoff(key, link, filename, caption, attempt)
        )
        rec["retries"] = attempt
        _resend_tasks.add(task)
        task.add_done_callback(_discard_task)
        logger.info(
            "doc_resend scheduled | to=%s code=%s attempt=%d/%d backoff=%.0fs",
            _mask(recipient), sorted(codes & _RETRYABLE_CODES), attempt,
            _MAX_RETRIES, _BACKOFF_SECONDS,
        )
        return True
    except Exception:  # pragma: no cover — never break the webhook handler
        logger.exception("doc_resend.maybe_resend failed (non-fatal)")
        return False


def _discard_task(task: asyncio.Task) -> None:
    _resend_tasks.discard(task)
    if not task.cancelled():
        exc = task.exception()
        if exc is not None:  # pragma: no cover — resend coro swallows its own errors
            logger.warning("doc_resend task errored (non-fatal): %s", exc)


async def _resend_after_backoff(
    key: str, link: str, filename: str, caption: Optional[str], attempt: int
) -> None:
    """Wait the backoff, then re-send the document. Lazy-imports send_document to
    avoid an import cycle. Never raises; never logs the link/filename."""
    try:
        await asyncio.sleep(_BACKOFF_SECONDS)
        from services.whatsapp_sender import send_document  # lazy: break cycle

        ok = await send_document(key, link, filename, caption=caption)
        logger.info(
            "doc_resend attempt %d/%d %s | to=%s",
            attempt, _MAX_RETRIES, "sent" if ok else "failed", _mask(key),
        )
    except Exception:  # pragma: no cover — best-effort; swallow
        logger.exception("doc_resend attempt raised (non-fatal) | to=%s", _mask(key))


def _reset_for_tests() -> None:  # pragma: no cover — test helper only
    _records.clear()
    _resend_tasks.clear()
