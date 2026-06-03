"""
Durable session store — Redis-backed persistence for the two in-flight chat
session types so an ai-engine restart no longer drops a citizen's guided
submission or an officer's in-progress case.

────────────────────────────────────────────────────────────────────────────
WHY
────────────────────────────────────────────────────────────────────────────
`guided_submission.SubmissionSession` and `officer_bridge.OfficerCaseSession`
both live in bounded in-memory `OrderedDict` LRUs today. That is fine while
the process stays up, but a redeploy mid-flow strands the user (the citizen
loses their half-filled form + the live document score; the officer loses the
case they were reviewing). Both modules already document Redis as the planned
follow-up — this module is that follow-up.

────────────────────────────────────────────────────────────────────────────
DESIGN
────────────────────────────────────────────────────────────────────────────
* This module owns NO knowledge of either dataclass. The owning module passes
  a (encode, decode) pair of callables — encode(obj) -> str (JSON), decode(str)
  -> obj. That keeps `session_store` import-light and free of circular imports
  with `guided_submission` / `officer_bridge`.
* The Redis client is `redis.asyncio` (the maintained async client that ships
  inside the `redis` package since 4.2 — no separate `aioredis` dependency).
  The import is LAZY (inside `_get_client`) so this module imports cleanly even
  when the `redis` wheel isn't installed (e.g. the unit-test harness, or an
  APTANA-only deploy that hasn't added the dep yet).
* DURABILITY-WITH-FALLBACK: every public call is best-effort. On ANY Redis
  error (not installed, unreachable, auth failure, serialization error) it
  logs once at WARNING and returns a sentinel (None / False) so the caller
  falls straight back to its in-memory store. Redis is an enhancement, never
  a hard dependency — nothing breaks when it's down.

────────────────────────────────────────────────────────────────────────────
FEATURE FLAG
────────────────────────────────────────────────────────────────────────────
`BIMA_REDIS_SESSIONS_ENABLED` (default "false"). While off, `is_enabled()`
returns False and every store/load is a no-op — the owning modules behave
exactly as they did before this module existed. Flip it on (plus REDIS_* env)
to make sessions durable. This mirrors the conservative default of the other
BIMA flags so the change is safe to ship before Redis is provisioned for
ai-engine.

────────────────────────────────────────────────────────────────────────────
ENV
────────────────────────────────────────────────────────────────────────────
  REDIS_HOST              default "redis"  (docker-compose service name)
  REDIS_PORT              default 6379
  REDIS_DB                default 0
  REDIS_PASSWORD          default ""       (set on the VPS once requirepass is
                                            configured — hardening task #57)
  BIMA_REDIS_SESSIONS_ENABLED  default "false"

────────────────────────────────────────────────────────────────────────────
PII / SECURITY
────────────────────────────────────────────────────────────────────────────
* Document bytes are serialized by the OWNING module's encode() as base64
  inside the JSON blob. This module NEVER logs the serialized payload, only
  its byte length. Keys (which embed the user_id / officer channel id) are
  MASKED in every log line via `_mask`.
* No password or token is ever logged. The Redis password is read from env
  and passed straight to the client.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("bima_ai.session_store")


# ===========================================================================
# Feature flag + config
# ===========================================================================


def is_enabled() -> bool:
    """Master flag for Redis-backed durable sessions. Default OFF.

    Read at call time (not import time) so tests/ops can flip the env without
    a restart — same pattern as the other BIMA feature flags.
    """
    return os.getenv("BIMA_REDIS_SESSIONS_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _redis_params() -> dict[str, Any]:
    """Read the discrete REDIS_* env vars into kwargs for `redis.asyncio.Redis`.

    Discrete vars (host/port/db/password) match the docker-compose service and
    the way admin-api is configured, rather than a single REDIS_URL — keeps the
    .env consistent across services.

    Passing the password as a DISCRETE kwarg (not interpolated into a redis://
    URL) avoids the URL-corruption class of bug: a requirepass value containing
    `@ : # / ?` or whitespace would otherwise mangle the URL's userinfo and
    silently break auth (durable sessions would degrade to OFF without a clear
    signal). The client handles any password byte verbatim this way.
    """
    host = os.getenv("REDIS_HOST", "redis").strip() or "redis"
    port_raw = os.getenv("REDIS_PORT", "6379").strip() or "6379"
    db_raw = os.getenv("REDIS_DB", "0").strip() or "0"
    password = os.getenv("REDIS_PASSWORD", "")
    try:
        port = int(port_raw)
    except ValueError:
        port = 6379
    try:
        db = int(db_raw)
    except ValueError:
        db = 0
    params: dict[str, Any] = {"host": host, "port": port, "db": db}
    if password:
        params["password"] = password
    return params


def _password_configured() -> bool:
    """True when REDIS_PASSWORD is set (used to make an auth-broken ping loud
    instead of silently degrading to in-memory)."""
    return bool(os.getenv("REDIS_PASSWORD", ""))


def _mask(value: Optional[str]) -> str:
    """Mask a key / id for logs — never log a full user_id or channel id."""
    if not value:
        return "<none>"
    s = str(value)
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


# ===========================================================================
# Lazy, cached async client
# ===========================================================================

# Cached client + a one-shot flag so a missing dep / unreachable server logs
# ONCE rather than on every turn.
_client: Any = None
_client_init_failed = False
_warned_unavailable = False


def _warn_once(msg: str, *args: Any) -> None:
    global _warned_unavailable
    if not _warned_unavailable:
        logger.warning(msg, *args)
        _warned_unavailable = True


async def _get_client() -> Any:
    """Return a cached `redis.asyncio.Redis`, or None if Redis is unavailable.

    Lazy-imports `redis.asyncio` so the module imports without the dep. The
    client is created with `decode_responses=True` (we only ever store/read
    UTF-8 JSON strings — document bytes live base64-encoded INSIDE the JSON).
    Never raises.
    """
    global _client, _client_init_failed
    if _client is not None:
        return _client
    if _client_init_failed:
        return None
    try:
        from redis import asyncio as aioredis  # lazy: dep may be absent
    except Exception:
        _client_init_failed = True
        _warn_once(
            "session_store: `redis` package not installed — durable sessions "
            "disabled, falling back to in-memory. Add `redis` to requirements "
            "and set BIMA_REDIS_SESSIONS_ENABLED=true to enable."
        )
        return None
    try:
        _client = aioredis.Redis(
            **_redis_params(),
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
    except Exception:
        _client_init_failed = True
        _warn_once("session_store: failed to construct Redis client — using in-memory")
        return None
    return _client


async def ping() -> bool:
    """Best-effort health check used by the FastAPI lifespan startup hook.

    Returns True when Redis is reachable AND the flag is on. Logs a single
    line either way. Never raises.
    """
    if not is_enabled():
        logger.info("session_store: durable sessions disabled (flag off)")
        return False
    client = await _get_client()
    if client is None:
        return False
    try:
        pong = await client.ping()
        if pong:
            logger.info("session_store: Redis reachable — durable sessions ON")
            return True
        logger.warning(
            "session_store: Redis ping returned falsy — durable sessions OFF "
            "(falling back to in-memory)"
        )
        return False
    except Exception as exc:
        # Make a degraded/auth-broken state visible at startup rather than
        # silently degrading. When a password IS configured, a ping failure is
        # very likely an auth mismatch (wrong/missing REDIS_PASSWORD vs the
        # server's requirepass) — call that out explicitly. Never log the
        # password or the exception's message (it can echo connection details).
        if _password_configured():
            logger.error(
                "session_store: Redis ping FAILED while REDIS_PASSWORD is set "
                "(err=%s) — likely an auth mismatch with the server's "
                "requirepass; durable sessions are OFF (in-memory only). "
                "Verify REDIS_PASSWORD.",
                exc.__class__.__name__,
            )
        else:
            logger.warning(
                "session_store: Redis ping failed (err=%s) — falling back to "
                "in-memory",
                exc.__class__.__name__,
            )
        return False


# ===========================================================================
# Generic store / load / delete — the owning module supplies (encode, decode).
# ===========================================================================


async def save(
    key: str,
    obj: Any,
    *,
    encode: Callable[[Any], str],
    ttl_seconds: int,
) -> bool:
    """Serialize `obj` via `encode` and SET it at `key` with a TTL.

    Returns True on a successful write, False on any failure (caller keeps its
    in-memory copy regardless — this is write-through, not write-only). Never
    raises. Logs only the masked key + payload byte length, never the payload.
    """
    if not is_enabled():
        return False
    client = await _get_client()
    if client is None:
        return False
    try:
        blob = encode(obj)
    except Exception:
        logger.exception("session_store: encode failed | key=%s", _mask(key))
        return False
    try:
        # ex= sets the TTL atomically with the write so an abandoned session
        # self-expires exactly like the in-memory TTL would drop it.
        await client.set(key, blob, ex=max(1, int(ttl_seconds)))
        logger.debug(
            "session_store: saved | key=%s | bytes=%d | ttl=%ds",
            _mask(key), len(blob), ttl_seconds,
        )
        return True
    except Exception:
        _warn_once("session_store: SET failed | key=%s — using in-memory", _mask(key))
        return False


async def load(
    key: str,
    *,
    decode: Callable[[str], Any],
) -> Optional[Any]:
    """GET `key` and deserialize via `decode`. Returns the object, or None when
    the key is absent, Redis is unavailable, or decoding fails. Never raises."""
    if not is_enabled():
        return None
    client = await _get_client()
    if client is None:
        return None
    try:
        blob = await client.get(key)
    except Exception:
        _warn_once("session_store: GET failed | key=%s — using in-memory", _mask(key))
        return None
    if blob is None:
        return None
    try:
        return decode(blob)
    except Exception:
        # A malformed / schema-drifted blob must not strand the user. Drop it
        # and fall back to in-memory (treat as a cache miss).
        logger.exception("session_store: decode failed | key=%s — dropping", _mask(key))
        try:
            await client.delete(key)
        except Exception:
            pass
        return None


async def delete(key: str) -> bool:
    """DELETE `key`. Returns True on a successful delete call, False otherwise.
    Never raises — clearing a session must never block a terminal transition."""
    if not is_enabled():
        return False
    client = await _get_client()
    if client is None:
        return False
    try:
        await client.delete(key)
        return True
    except Exception:
        _warn_once("session_store: DELETE failed | key=%s", _mask(key))
        return False


# A small convenience the owning modules can use to namespace keys so the two
# session types never collide in the shared Redis db.
def submission_key(user_id: str) -> str:
    return f"bima:submission_session:{user_id}"


def officer_key(channel_id: str) -> str:
    return f"bima:officer_session:{channel_id}"
