"""WhatsApp delivery-status tracker — "did the citizen read the notification?".

BIMA sends proactive notifications (citizen_completed, citizen_needs_fix, …)
via APTANA. Meta then reports the delivery lifecycle back through the
`ReceiveWhatsAppBusinessWebhookMeta` worker → /webhook/aptana/status:
    sent → delivered → read   (or → failed)

This module links those status callbacks to the ticket they belong to, so the
admin/case view can show "✓✓ Pemohon membaca notifikasi · 20:58" and the
watchdog can flag a notice that was delivered but never read.

Design
------
- Keyed by recipient phone (normalized to digits). At BIMA's current volume —
  and with the 2-messages-per-citizen-per-day notification cap (see
  notifications.py) — a phone has at most a couple of in-flight notifications,
  so phone-keyed matching is accurate. We store the MOST RECENT outbound
  notification per phone; a new send for the same phone replaces the prior
  record (the older one has already reached its terminal status by then).
- Status is ADVANCE-ONLY: WhatsApp status webhooks arrive out of order (a
  `delivered` can land after a `read`). We rank sent<delivered<read and never
  regress, so a late `delivered` can't overwrite a `read`.
- In-memory dict (single ai-engine container) + atomic JSON persistence so a
  restart doesn't lose recent read state. Bounded to the most recent N phones.

This module never sends anything and never raises to its callers.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_STORE_PATH = Path(
    os.getenv("DELIVERY_TRACKER_PATH", "/app/data/delivery_status.json")
)
_MAX_RECORDS = int(os.getenv("DELIVERY_TRACKER_MAX", "2000") or "2000")

# Status ordering — only ever advance to a higher rank.
_RANK = {"sent": 1, "delivered": 2, "read": 3}

# phone(digits) → record dict. OrderedDict for LRU eviction.
_records: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _mask(phone: str) -> str:
    d = _digits(phone)
    return d[:4] + "…" + d[-3:] if len(d) >= 7 else "<short>"


def _load() -> None:
    global _records
    try:
        if _STORE_PATH.exists():
            raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _records = OrderedDict(raw)
    except Exception as exc:  # pragma: no cover — corrupt file, start fresh
        logger.warning("delivery tracker: could not load %s (%s)", _STORE_PATH, exc)
        _records = OrderedDict()


def _save() -> None:
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STORE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_records), encoding="utf-8")
        tmp.replace(_STORE_PATH)
    except Exception as exc:  # pragma: no cover
        logger.warning("delivery tracker: could not persist (%s)", exc)


_load()


def record_sent(phone: str, ticket: str | None, event: str) -> None:
    """Record that a notification was just sent to `phone` for `ticket`.

    Called from notifications.notify() right after a successful send. Replaces
    any prior record for this phone (the prior notification has reached its
    terminal status by now) and resets the lifecycle to 'sent'.
    """
    key = _digits(phone)
    if not key:
        return
    _records[key] = {
        "ticket": ticket,
        "event": event,
        "phone_masked": _mask(phone),
        "sent_at": time.time(),
        "status": "sent",
        "status_at": time.time(),
    }
    _records.move_to_end(key)
    while len(_records) > _MAX_RECORDS:
        _records.popitem(last=False)
    _save()


def update_status(phone: str, status: str, ts: Optional[str | float] = None) -> None:
    """Advance the delivery status for `phone` from a Meta status callback.

    Advance-only: a lower-ranked status (e.g. a late 'delivered') never
    overwrites a higher one ('read'). 'failed' is recorded only if the message
    hasn't already been read.
    """
    key = _digits(phone)
    if not key:
        return
    rec = _records.get(key)
    if rec is None:
        # A status for a phone we have no send record for — e.g. an ad-hoc
        # APTANA send outside notify(), or a record evicted/lost on restart.
        # Nothing to attach it to; ignore quietly.
        return

    when = _coerce_ts(ts)

    if status == "failed":
        if _RANK.get(rec.get("status"), 0) < _RANK["read"]:
            rec["status"] = "failed"
            rec["status_at"] = when
            _save()
        return

    incoming_rank = _RANK.get(status, 0)
    if incoming_rank == 0:
        return  # unknown status, ignore
    if incoming_rank > _RANK.get(rec.get("status"), 0):
        rec["status"] = status
        rec["status_at"] = when
        _records.move_to_end(key)
        _save()


def _coerce_ts(ts: Optional[str | float]) -> float:
    if ts is None:
        return time.time()
    try:
        return float(ts)
    except (TypeError, ValueError):
        return time.time()


def get_by_phone(phone: str) -> Optional[dict[str, Any]]:
    """Return the delivery record for a phone, or None."""
    return _records.get(_digits(phone))


def get_by_ticket(ticket: str) -> list[dict[str, Any]]:
    """Return all delivery records whose ticket matches (newest first).

    Small linear scan — the store is bounded and per-ticket lookups are
    admin-triggered (not hot-path).
    """
    if not ticket:
        return []
    out = [
        {**rec, "phone": phone_key_masked}
        for phone_key_masked, rec in (
            (rec.get("phone_masked", "<unknown>"), rec) for rec in _records.values()
        )
        if rec.get("ticket") == ticket
    ]
    out.sort(key=lambda r: r.get("sent_at", 0), reverse=True)
    return out
