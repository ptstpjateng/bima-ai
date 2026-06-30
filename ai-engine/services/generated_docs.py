"""In-memory store for BIMA-generated PDFs, served at a public short-lived URL
so APTANA can fetch them and deliver them to the citizen as WhatsApp *document*
messages.

The token is unguessable (`secrets`), entries expire fast (default 15 min) AND
burn after a few fetches, and the PDF itself is AES-encrypted (opens only with
the citizen's NIK). So a leaked URL yields an unreadable, soon-dead link. The
only reader is APTANA fetching the file moments after BIMA sends the message; it
re-hosts on Meta's CDN, so the citizen opens it from WhatsApp, not from here.
In-memory is intentional: a container restart simply means BIMA regenerates on
the next turn — no durable PII at rest from this path.
"""
from __future__ import annotations

import secrets
import time
from threading import Lock

_TTL_SECONDS = 15 * 60   # short window — APTANA fetches within seconds of the send
_MAX_FETCHES = 5         # burn after a few reads (APTANA HEAD+GET + a citizen re-open)
# token -> [pdf, filename, expiry_ts, fetches_left]
_store: dict[str, list] = {}
_lock = Lock()


def _sweep(now: float) -> None:
    for k in [k for k, v in list(_store.items()) if v[2] < now]:
        _store.pop(k, None)


def store(pdf: bytes, filename: str, ttl: int = _TTL_SECONDS,
          max_fetches: int = _MAX_FETCHES) -> str:
    """Stash a PDF; return the unguessable token to put in the /dl/{token} URL.

    Short TTL + a fetch cap mean the link dies seconds after APTANA collects the
    file (which it re-hosts on Meta's CDN), shrinking the leak window to near
    zero — defence-in-depth on top of the per-document AES encryption.
    """
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        _sweep(now)
        _store[token] = [pdf, filename, now + ttl, max(1, max_fetches)]
    return token


def fetch(token: str) -> tuple[bytes, str] | None:
    """Return (pdf_bytes, filename) for a live token, else None. Consumes one of
    the token's allowed fetches and burns it once exhausted or expired."""
    now = time.time()
    with _lock:
        item = _store.get(token)
        if item is None:
            return None
        pdf, name, exp, left = item
        if exp < now or left <= 0:
            _store.pop(token, None)
            return None
        item[3] = left - 1
        if item[3] <= 0:
            _store.pop(token, None)  # burn after the last allowed fetch
        return pdf, name
