"""In-memory store for BIMA-generated PDFs, served at a public short-lived URL
so APTANA can fetch them and deliver them to the citizen as WhatsApp *document*
messages.

The token is unguessable (`secrets`), entries expire AND burn after a few
fetches, and the PDF itself is AES-encrypted (opens only with the citizen's
NIK). So a leaked URL yields an unreadable, soon-dead link. In-memory is
intentional: a container restart simply means BIMA regenerates on the next turn
— no durable PII at rest from this path.

TTL — MEASURED, NOT ASSUMED (2026-07-16)
  This module used to say "APTANA fetches within seconds of the send" and set a
  15-minute TTL on that belief. It is false. On the live PKPP run:

      10:35:00  doc-prep pack stored; APTANA accepted the send (HTTP 200)
      10:50:00  TTL expired
      11:06:31  APTANA issued its FIRST fetch — 31m31s after the send

  Every link was already dead when APTANA asked for it: 46 fetches, 0 hits, and
  APTANA then retried each dead token ~23 times. The citizen and the officer
  both saw "document failed to send" for documents BIMA had generated correctly.

  So the TTL must cover APTANA's real queueing latency plus its retry window,
  not an imagined one. The default below is ~4x the worst latency observed. The
  short-lived-link property is preserved by `_MAX_FETCHES`, which is the control
  that actually matters: the link dies the moment APTANA has collected the file
  (it re-hosts on Meta's CDN), regardless of the TTL. TTL is only the backstop
  for a link that is never fetched at all.

  If APTANA's latency changes, tune BIMA_DL_TTL_SECONDS — don't guess. The
  `dl miss: EXPIRED` log line below prints exactly how late the fetch was.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from threading import Lock

logger = logging.getLogger("bima_ai.generated_docs")


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive int from env, falling back on anything unparseable."""
    try:
        return max(minimum, int(os.getenv(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


# 2h: ~4x the 31-minute first-fetch latency measured on the 2026-07-16 run.
_TTL_SECONDS = _int_env("BIMA_DL_TTL_SECONDS", 2 * 60 * 60, minimum=60)
# The real "short-lived" control: the link burns as soon as APTANA has it.
# 10 (was 5) leaves room for a HEAD+GET plus a retry or two without burning a
# link APTANA has not actually collected yet.
_MAX_FETCHES = _int_env("BIMA_DL_MAX_FETCHES", 10, minimum=1)

# token -> [pdf, filename, expiry_ts, fetches_left, mime]
_store: dict[str, list] = {}
_lock = Lock()


def _sweep(now: float) -> None:
    for k in [k for k, v in list(_store.items()) if v[2] < now]:
        _store.pop(k, None)


def store(pdf: bytes, filename: str, ttl: int = _TTL_SECONDS,
          max_fetches: int = _MAX_FETCHES, *, mime: str | None = None) -> str:
    """Stash a file; return the unguessable token to put in the /dl/{token} URL.

    `mime` defaults to a guess from the filename extension (so PDF callers stay
    unchanged, DOCX callers get the right Content-Type) — APTANA infers the
    WhatsApp document type from what /dl serves, so the mime must be correct.
    """
    if mime is None:
        import mimetypes
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        _sweep(now)
        _store[token] = [pdf, filename, now + ttl, max(1, max_fetches), mime]
        live = len(_store)
    # Token PREFIX only (6 of 32 chars — not enough to guess), and the filename
    # LENGTH not the name, matching the PII discipline in officer_bridge.
    logger.info(
        "dl store | tok=%s… | ttl=%ds | fetches=%d | name_len=%d | bytes=%d | live=%d",
        token[:6], ttl, max_fetches, len(filename or ""), len(pdf or b""), live,
    )
    return token


def fetch(token: str) -> tuple[bytes, str, str] | None:
    """Return (bytes, filename, mime) for a live token, else None. Consumes one
    of the token's allowed fetches and burns it once exhausted or expired.

    Logs WHY a miss happened. The old code logged a single ambiguous
    "expired/unknown token", which is what made the 2026-07-16 outage take an
    hour to diagnose: it could not distinguish "APTANA was 16 minutes too late"
    from "this token never existed", and those have opposite fixes.
    """
    now = time.time()
    with _lock:
        item = _store.get(token)
        if item is None:
            logger.info(
                "dl miss: UNKNOWN token (never stored, or already burned/swept) "
                "| tok=%s… | live=%d",
                token[:6], len(_store),
            )
            return None
        pdf, name, exp, left, mime = item
        if exp < now:
            _store.pop(token, None)
            logger.info(
                "dl miss: EXPIRED — fetched %.0fs AFTER expiry (ttl=%ds). The "
                "fetcher is slower than the TTL; raise BIMA_DL_TTL_SECONDS "
                "| tok=%s…",
                now - exp, _TTL_SECONDS, token[:6],
            )
            return None
        if left <= 0:
            _store.pop(token, None)
            logger.info(
                "dl miss: BURNED — fetch cap %d exhausted | tok=%s…",
                _MAX_FETCHES, token[:6],
            )
            return None
        item[3] = left - 1
        if item[3] <= 0:
            _store.pop(token, None)  # burn after the last allowed fetch
        logger.info(
            "dl hit | tok=%s… | age=%.0fs | fetches_left=%d",
            token[:6], (now - (exp - _TTL_SECONDS)), item[3],
        )
        return pdf, name, mime
