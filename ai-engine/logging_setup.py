"""Centralized logging hardening for ai-engine.

This module exists because today (2026-06-02) we found two log paths that leak
secrets into container stdout:

  1. The Telegram bot token appears verbatim in httpx's outbound URL INFO log
     line. The token is the path segment after `bot` in
     `https://api.telegram.org/bot<TOKEN>/sendMessage`. Anyone with VPS log
     access (or the log aggregator we'd add later) can recover full bot
     control.

  2. Our own request-id middleware logs `request.url.path` for every inbound
     request. For `/webhook/telegram/<secret>` and
     `/webhook/aptana/inbound/<secret>` the path-secret — which authenticates
     the webhook — is part of the URL path, so it gets logged on every hit.

The fix here is defense-in-depth: redact at the source (httpx URL log) AND
at the middleware (path log), so a future leak in either layer alone doesn't
re-expose the secret.

WHY a logging.Filter instead of dropping the URL field:
  - httpx logs are useful for debugging upstream failures (connection refused,
    timeout, 5xx); we want them, just without the credential. A filter that
    rewrites the record's `args`/`msg` preserves the diagnostic value.

WHY we keep INFO level on httpx (not lift to WARNING):
  - That would suppress legitimately useful messages like "HTTP/1.1 429 Too
    Many Requests" against Gemini. The filter approach is strictly better.

The patterns covered (in mask_url_secrets):
  - api.telegram.org/bot<TOKEN>/...      → api.telegram.org/bot<redacted>/...
  - graph.facebook.com/.../?access_token=<TOKEN>  → ...?access_token=<redacted>
  - generativelanguage.googleapis.com/...?key=<KEY> → ...?key=<redacted>  (Gemini/Gemma)
  - /webhook/telegram/<secret>           → /webhook/telegram/<redacted>
  - /webhook/aptana/inbound/<secret>     → /webhook/aptana/inbound/<redacted>
  - /webhook/aptana/status/<secret>      → /webhook/aptana/status/<redacted>
  - /webhook/aptana/greeting/<secret>    → /webhook/aptana/greeting/<redacted>

All patterns are conservative: they require enough context around the secret
that we won't accidentally redact unrelated path segments.

PII masking (mask_pii):
  - user_id=wa-<msisdn>      → user_id=wa-****<last4>
  - user_id=tg-<chat_id>     → user_id=tg-****<last4>

Channel-prefixed user_ids are how ai_handler keys its in-memory session dict
('wa-{msisdn}' / 'tg-{chat_id}'). Logged verbatim that's a full phone number
or Telegram chat-id on every line.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Tuple

__all__ = [
    "mask_url_secrets",
    "mask_pii",
    "SecretRedactingFilter",
    "configure_logging",
]


# ---------------------------------------------------------------------------
# Redaction patterns
#
# Order matters only for readability; the substitutions are independent and
# all are applied to every input. Using non-greedy `[^/?#\s"']+` so we never
# eat past a path/query separator or into adjacent log text.
# ---------------------------------------------------------------------------

_REDACTED = "<redacted>"

_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    # Telegram bot API: token sits between `/bot` and the next `/`.
    # Example: https://api.telegram.org/bot123:ABC-DEF/sendMessage
    (
        re.compile(r"(api\.telegram\.org/bot)[^/?#\s\"']+", re.IGNORECASE),
        r"\1" + _REDACTED,
    ),
    # Meta Graph API access_token query param, used by the direct Meta
    # WhatsApp Cloud API sender. Permanent system-user tokens here are
    # equally damaging if leaked.
    (
        re.compile(
            r"(graph\.facebook\.com/[^?\s]*[?&]access_token=)[^&\s\"']+",
            re.IGNORECASE,
        ),
        r"\1" + _REDACTED,
    ),
    # Google Generative Language API (Gemini / Gemma) API key. httpx logs the
    # full request URL at INFO, and the key (AIza...) is the `?key=<KEY>` query
    # param — e.g. generativelanguage.googleapis.com/v1beta/models/...:generateContent?key=AIza...
    (
        re.compile(
            r"(generativelanguage\.googleapis\.com/[^?\s]*[?&]key=)[^&\s\"']+",
            re.IGNORECASE,
        ),
        r"\1" + _REDACTED,
    ),
    # BIMA webhook path-secrets. Each route has a `{path_secret}` segment
    # we constant-time-compare in the handler; logging that segment defeats
    # the purpose. Trailing `(?=$|[/?#\s])` so we don't redact past the
    # boundary if extra path components are appended later.
    (
        re.compile(r"(/webhook/telegram/)[^/?#\s\"']+"),
        r"\1" + _REDACTED,
    ),
    (
        re.compile(r"(/webhook/aptana/(?:inbound|status|greeting)/)[^/?#\s\"']+"),
        r"\1" + _REDACTED,
    ),
)


def mask_url_secrets(text: str) -> str:
    """Return `text` with any known secret-bearing URL segments redacted.

    Safe to call on arbitrary strings: if no pattern matches, the input is
    returned unchanged. Idempotent (running it twice produces the same
    output as running it once).
    """
    if not text:
        return text
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


# ---------------------------------------------------------------------------
# PII patterns — only used at the log boundary, never on stored data.
#
# We deliberately keep the last 4 characters visible so an operator can still
# correlate logs across services for one user during an incident, without ever
# revealing enough to re-identify the citizen externally.
# ---------------------------------------------------------------------------

_PII_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    # Channel-prefixed user_id in either KEY=VALUE form or whitespace-bounded
    # bare token. The lookbehind / boundary handling keeps us from matching
    # inside the middle of an unrelated identifier.
    (
        # user_id=wa-628123456789  OR  user_id=tg-987654321
        re.compile(r"\b(user_id=(?:wa|tg)-)([^\s,|]{4,})", re.IGNORECASE),
        lambda m: m.group(1) + _short_tail(m.group(2)),
    ),
    (
        # Bare phone-like sequence: msisdn=628123456789
        re.compile(r"\b(msisdn=)(\+?\d{8,15})", re.IGNORECASE),
        lambda m: m.group(1) + _short_tail(m.group(2)),
    ),
    (
        # NIK: Indonesian National ID, exactly 16 digits. Logging the whole
        # thing is a citizen-data breach under PDP Law (UU 27/2022).
        re.compile(r"\b(nik=|identity_number=)(\d{16})", re.IGNORECASE),
        lambda m: m.group(1) + "************" + m.group(2)[-4:],
    ),
)


def _short_tail(value: str) -> str:
    """Keep the last 4 chars; mask the rest. Stable across log lines."""
    if len(value) <= 4:
        return "*" * len(value)
    return "****" + value[-4:]


def mask_pii(text: str) -> str:
    """Return `text` with PII (NIK, msisdn, channel-prefixed user_id) masked.

    Complementary to mask_url_secrets — that one handles auth credentials in
    URLs; this one handles personal data in structured log fields.
    """
    if not text:
        return text
    out = text
    for pattern, repl in _PII_PATTERNS:
        out = pattern.sub(repl, out)
    return out


# ---------------------------------------------------------------------------
# logging.Filter — applied to httpx (and any other library whose INFO log
# may carry a credential in its URL field).
#
# We mutate the record in-place so downstream handlers see the redacted
# version. Both `record.msg` (the format template) and `record.args` (any
# positional/dict substitutions) are scrubbed because httpx-style loggers
# put the URL in either depending on call site.
# ---------------------------------------------------------------------------


class SecretRedactingFilter(logging.Filter):
    """Strip known secrets AND PII from a LogRecord before it's emitted.

    Attach to any logger via `addFilter`. This is cheap — the regex set is
    small and the input is one log line.

    Implementation detail — WHY we materialize the message before scrubbing:
      A naïve filter scrubs `record.msg` and `record.args` separately. That
      misses cases where the secret only forms a recognizable pattern AFTER
      printf-style substitution. Example:

          log.info("HTTP Request: POST %s", url_with_token)

      Here `record.msg` has no token and `record.args[0]` is just the bare
      token (no surrounding URL context), so neither piece matches our
      patterns individually — but the substituted line does. Calling
      `record.getMessage()` first and stuffing the result back into `msg`
      with empty `args` lets us scrub the already-substituted text. Side
      effect: the formatter no longer does %-substitution downstream, which
      is fine because we already did it.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        try:
            rendered = record.getMessage()
        except Exception:  # noqa: BLE001
            # If substitution itself fails (caller bug), fall back to best-
            # effort scrubbing on the raw msg so we never drop the log.
            if isinstance(record.msg, str):
                record.msg = _scrub_text(record.msg)
            return True

        record.msg = _scrub_text(rendered)
        record.args = ()
        return True  # always allow the record through (just redacted)


def _scrub_text(text: str) -> str:
    """Apply both URL-secret and PII redaction passes."""
    return mask_pii(mask_url_secrets(text))


# ---------------------------------------------------------------------------
# configure_logging — call once at app startup, after logging.basicConfig.
#
# The list of target loggers is deliberately conservative: any library that
# might log a URL with embedded credentials. If we add more outbound HTTP
# clients (aiohttp, requests, urllib3) extend `_REDACTED_LOGGERS` below.
# ---------------------------------------------------------------------------

_REDACTED_LOGGERS: Iterable[str] = (
    "httpx",      # Gemini, SIAP, Meta WhatsApp, APTANA, Telegram outbound
    "httpcore",   # httpx's transport — logs the same URL in DEBUG / INFO
    "urllib3",    # belt-and-suspenders if something falls back to requests
)


def configure_logging() -> None:
    """Install the secret-redacting filter on every relevant logger AND on the
    root stream handler.

    WHY both:
      - Per-logger filters only run when the log record originates on that
        named logger; they catch httpx's "HTTP Request" line.
      - The root-handler filter catches application logs too (bima_ai.*),
        because logging.Filter only runs against records emitted directly to
        the logger it's attached to — but handler-level filters run for any
        record routed to that handler, regardless of source logger.

    Idempotent — safe to call multiple times (we de-dupe by filter class).
    Must be called AFTER `logging.basicConfig` so the handlers exist.
    """
    f = SecretRedactingFilter()

    # Per-logger filters — useful as a belt-and-suspenders for third-party
    # libraries that log directly without going through our handler.
    for name in _REDACTED_LOGGERS:
        log = logging.getLogger(name)
        if not any(isinstance(existing, SecretRedactingFilter) for existing in log.filters):
            log.addFilter(f)

    # Handler-level filter on the root logger — catches every log record
    # routed to stdout, including our own bima_ai.* lines and any logger we
    # forgot to enumerate above.
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(existing, SecretRedactingFilter) for existing in handler.filters):
            handler.addFilter(f)

    # Mute ChromaDB's telemetry logger. It spams ~230 ERROR lines/hour —
    # "Failed to send telemetry event ClientStartEvent: capture() takes 1
    # positional argument but 3 were given" — a chromadb↔posthog version-mismatch
    # bug that fires on every client init and is NOT silenced by
    # ANONYMIZED_TELEMETRY or per-client Settings in this chromadb build. It is
    # harmless (Chroma works fine; only the telemetry send fails) but drowns real
    # errors, so we suppress the logger outright.
    logging.getLogger("chromadb.telemetry").setLevel(logging.CRITICAL)
