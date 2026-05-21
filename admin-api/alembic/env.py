"""
Alembic environment.

Behaviour:
    1. Read DATABASE_URL from env (or fall back to Settings — same source).
    2. Set Alembic's sqlalchemy.url at runtime so the static value in
       alembic.ini stays a harmless placeholder.
    3. Run migrations against an AsyncEngine — Alembic supports this since
       1.6 via `connection.run_sync(...)`.
    4. `include_object` filters out tables marked as Laravel-owned, so
       `alembic revision --autogenerate` ignores them. Without this, an
       autogenerate run would emit destructive DROP statements for every
       Laravel table whose Python model uses `keep_existing=True`.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Ensure the `app` package is importable when alembic is invoked from the
# admin-api directory (prepend_sys_path = . in alembic.ini handles cwd).
from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402

# Importing app.models registers every model on Base.metadata.
import app.models  # noqa: E402,F401

config = context.config

# Inject the runtime DATABASE_URL.
#
# Alembic stores main options in a ConfigParser, whose BasicInterpolation
# treats `%` as a metacharacter (`%(name)s` = interpolation, `%%` = a
# literal `%`). Our DATABASE_URL carries a url-encoded password — the `%`
# of every `%XX` escape (e.g. `%40` for `@`) is read as broken
# interpolation and `set_main_option` raises ValueError at import time.
# Doubling `%` → `%%` makes the string interpolation-safe; ConfigParser
# collapses it back to a single `%` when the value is read out via
# `get_main_option` / `get_section`, so SQLAlchemy receives the correct URL.
_settings = get_settings()
config.set_main_option(
    "sqlalchemy.url", _settings.DATABASE_URL.replace("%", "%%")
)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Tables managed by Laravel migrations. Alembic must NOT autogenerate diffs
# against these — the baseline migration marks them as already-applied, and
# this hook keeps future `alembic revision --autogenerate` from including
# them.
#
# When admin-api eventually owns a Laravel table (post-decommission), drop
# it from this set in the same revision that ports its schema.
# ---------------------------------------------------------------------------
LARAVEL_OWNED_TABLES: frozenset[str] = frozenset(
    {
        "users",
        "ai_interactions",
        "kblis",
        "kbli_scrape_targets",
        "magic_link_tokens",
        # Tables admin-api doesn't model but Laravel maintains — listed so
        # any future reflection helper doesn't accidentally pick them up.
        "businesses",
        "permit_applications",
        "pb_umkus",
        "knowledge_base_articles",
        "pdf_parse_jobs",
        "personal_access_tokens",
        "password_reset_tokens",
        "sessions",
        "cache",
        "cache_locks",
        "jobs",
        "job_batches",
        "failed_jobs",
        "migrations",
    }
)


def include_object(object_, name, type_, reflected, compare_to):  # noqa: ANN001
    """Skip Laravel-owned tables during autogenerate diffing."""
    if type_ == "table" and name in LARAVEL_OWNED_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    """Render SQL without an active DB connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Connect to the DB and run migrations through an AsyncEngine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
