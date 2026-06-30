"""
SQLAlchemy 2.x async engine + session factory.

Convention: every router/service consumes a session via the `get_db` dependency
in `deps.py`. Don't construct sessions directly elsewhere — that bypasses the
per-request cleanup and the engine pool tuning.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """
    Single declarative base for every model in `app/models/*`.

    All models inherit from this so `Base.metadata` aggregates every table
    Alembic / autogenerate needs to see. Reflected (Laravel-managed) tables
    use `__table_args__ = {"keep_existing": True}` and Alembic skips them via
    the include_object hook in `alembic/env.py`.
    """


_settings = get_settings()

# pool_pre_ping survives Postgres restarts that drop the TCP connection.
# pool_size=5 + max_overflow=10 keeps memory tight on the 2 uvicorn workers
# without throttling normal admin traffic.
engine: AsyncEngine = create_async_engine(
    _settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_recycle=1800,  # recycle connections every 30 min
)

# expire_on_commit=False is the right default for async — accessing attrs after
# commit shouldn't trigger an implicit IO that's no longer awaitable.
SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """
    FastAPI dependency: yield a session, commit on clean return, rollback on
    error, always close. Routers should depend on `app.deps.get_db` which is a
    thin alias around this — keeps the dep import surface single.
    """
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()  # commit on clean return (matches the docstring)
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# SIAP Jateng read-only engine.
#
# This is a SECOND, independently-pooled engine pointed at SIAP's database
# (dbsiapjateng). It exists for the citizen-SSO router (`routers/sso.py`)
# which needs to look up `ptsp.person_profile` rows by NIK.
#
# Why a separate engine (not a second schema on the bima engine)?
#   - SIAP runs on a different host (and, in some envs, a different Postgres
#     instance). It must not share a connection pool with bima_ai.
#   - It is read-only from BIMA's perspective. No models, no Alembic, no
#     transactions worth speaking of — a single `await conn.fetchrow(...)`
#     per request.
#   - Keeping it isolated means a SIAP outage cannot starve the bima_ai pool.
#
# Lazy init: the engine is only constructed on first use AND only if
# SIAP_DATABASE_URL is set. Callers should check `is_siap_db_configured()`
# first and return 503 rather than letting NoneType crash.
# ---------------------------------------------------------------------------
_siap_engine: AsyncEngine | None = None


def is_siap_db_configured() -> bool:
    """Cheap precheck without instantiating the engine."""
    return bool(_settings.SIAP_DATABASE_URL)


def get_siap_engine() -> AsyncEngine:
    """
    Return the lazily-constructed SIAP read-only engine.

    Raises RuntimeError if SIAP_DATABASE_URL is empty — callers should guard
    with `is_siap_db_configured()` and surface a 503 to the user.
    """
    global _siap_engine
    if not _settings.SIAP_DATABASE_URL:
        raise RuntimeError(
            "SIAP_DATABASE_URL is not configured; cannot open SIAP engine."
        )
    if _siap_engine is None:
        # Tighter pool than the primary bima_ai engine — citizen SSO is low
        # volume and we don't want to exhaust SIAP's connection slots.
        _siap_engine = create_async_engine(
            _settings.SIAP_DATABASE_URL,
            echo=False,
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=3,
            pool_recycle=1800,
        )
    return _siap_engine
