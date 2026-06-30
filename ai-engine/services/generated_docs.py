"""In-memory store for BIMA-generated PDFs, served at a public short-lived URL
so APTANA can fetch them and deliver them to the citizen as WhatsApp *document*
messages.

The token is unguessable (`secrets`) and entries expire fast (default 2h). The
document carries the citizen's own data, the link is unguessable + short-lived,
and the only reader is APTANA fetching the file moments after BIMA sends the
message. In-memory is intentional: a container restart simply means BIMA
regenerates on the next turn — no durable PII at rest from this path.
"""
from __future__ import annotations

import secrets
import time
from threading import Lock

_TTL_SECONDS = 2 * 3600
_store: dict[str, tuple[bytes, str, float]] = {}  # token -> (pdf, filename, expiry_ts)
_lock = Lock()


def _sweep(now: float) -> None:
    for k in [k for k, (_, _, exp) in list(_store.items()) if exp < now]:
        _store.pop(k, None)


def store(pdf: bytes, filename: str, ttl: int = _TTL_SECONDS) -> str:
    """Stash a PDF; return the unguessable token to put in the /dl/{token} URL."""
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        _sweep(now)
        _store[token] = (pdf, filename, now + ttl)
    return token


def fetch(token: str) -> tuple[bytes, str] | None:
    """Return (pdf_bytes, filename) for a live token, else None."""
    now = time.time()
    with _lock:
        item = _store.get(token)
        if item is None:
            return None
        pdf, name, exp = item
        if exp < now:
            _store.pop(token, None)
            return None
        return pdf, name
