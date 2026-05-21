"""
SIAP Jateng read-only Postgres connection (ai-engine).

The BIMA SIAP tool layer ([[Decisions]] §22, [[SIAP API Inventory]] Half 2)
reads SIAP's `dbsiapjateng` database directly: SIAP exposes no REST endpoint
for its licence catalogue, requirements, legal basis, fee, SLA, or per-ticket
step history — that "deep" knowledge is DB-only. This module owns the single
asyncpg connection pool the Tier-1 tools share.

Why asyncpg directly (not SQLAlchemy)?
  ai-engine has no SQLAlchemy dependency — it is a thin FastAPI service.
  admin-api's `siap_user_client.py` uses SQLAlchemy because admin-api already
  carries it for its own ORM. Here a bare asyncpg pool is lighter and the
  query surface is five read-only SELECTs. The connection *pattern* (a second,
  independently-pooled, lazily-built, read-only engine pointed at SIAP) is
  copied verbatim from admin-api `app/db.py`.

READ-ONLY by contract:
  * Every query in `siap_tools.py` is a parameterised SELECT — no string
    interpolation into SQL, ever.
  * `default_transaction_read_only` is forced on every connection via the
    pool `setup` hook, so even a buggy query physically cannot write.
  * Ideally the DSN points at a SELECT-only Postgres role. Today only the
    `bima` superuser exists on the VPS — see B1 in [[SIAP API Inventory]]
    §8: a `bima_readonly` role is a SIAP-team ask. The read-only transaction
    guard below is the interim safety net.

Configuration:
  SIAP_DB_URL — full asyncpg DSN, e.g.
    postgresql://bima:<pw>@postgres:5432/dbsiapjateng
  Leave it blank to disable the SIAP tool layer entirely (tools then return
  a structured "not configured" result and the agent degrades gracefully).

Failure model:
  Lazy init — the pool is built on first use and only if SIAP_DB_URL is set.
  Callers guard with `is_siap_db_configured()`. A SIAP outage never raises out
  of a tool: tools catch and return a structured error dict.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import asyncpg
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("bima_ai.siap_db")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Full asyncpg DSN. On the VPS, ai-engine reaches Postgres over the Docker
# network at host `postgres`, port 5432, database `dbsiapjateng`.
_SIAP_DB_URL: str = os.getenv("SIAP_DB_URL", "").strip()

# Conservative pool — the SIAP tool layer is low-volume (one citizen question
# at a time) and we must not exhaust SIAP's connection slots. Mirrors the
# tight pool admin-api uses for its SIAP engine (size 2, small overflow).
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 3
_CONNECT_TIMEOUT_SECONDS = 8.0
# Per-statement ceiling so a pathological query can't hang a citizen reply.
_COMMAND_TIMEOUT_SECONDS = 12.0

_pool: Optional[asyncpg.Pool] = None


def is_siap_db_configured() -> bool:
    """Cheap precheck without touching the pool. Callers should branch on
    this and return a friendly "integration disabled" result rather than
    letting a None pool crash."""
    return bool(_SIAP_DB_URL)


async def _on_connect(conn: asyncpg.Connection) -> None:
    """Pool `setup` hook — runs once per physical connection.

    Forces every connection BIMA opens against SIAP into read-only mode.
    Even if a future query is accidentally written as a mutation, Postgres
    rejects it ('cannot execute ... in a read-only transaction'). This is
    the interim guard until SIAP issues the `bima_readonly` role (B1)."""
    await conn.execute("SET default_transaction_read_only = on;")


async def get_siap_pool() -> asyncpg.Pool:
    """
    Return the lazily-constructed SIAP read-only connection pool.

    Raises RuntimeError if SIAP_DB_URL is empty — callers must guard with
    `is_siap_db_configured()` first. The pool is process-wide; asyncpg
    pools are safe to share across coroutines.
    """
    global _pool
    if not _SIAP_DB_URL:
        raise RuntimeError(
            "SIAP_DB_URL is not configured; cannot open the SIAP DB pool."
        )
    if _pool is None:
        logger.info(
            "Opening SIAP read-only pool | min=%d max=%d",
            _POOL_MIN_SIZE, _POOL_MAX_SIZE,
        )
        _pool = await asyncpg.create_pool(
            dsn=_SIAP_DB_URL,
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            timeout=_CONNECT_TIMEOUT_SECONDS,
            command_timeout=_COMMAND_TIMEOUT_SECONDS,
            setup=_on_connect,
        )
    return _pool


async def close_siap_pool() -> None:
    """Gracefully close the pool — wire into the FastAPI shutdown event if
    ai-engine starts managing lifespans for its connections. Safe to call
    when the pool was never opened."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("SIAP read-only pool closed.")
