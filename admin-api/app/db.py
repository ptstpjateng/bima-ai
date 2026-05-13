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
        except Exception:
            await session.rollback()
            raise
