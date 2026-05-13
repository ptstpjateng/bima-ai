"""
SQLAlchemy mapped classes for every table admin-api touches.

Two flavours coexist here:

* **Reflected/legacy** (users, ai_interactions, kbli, kbli_scrape_targets,
  magic_link_tokens) — these tables are managed by Laravel migrations. The
  classes describe the schema for SQLAlchemy/Pydantic, but Alembic must NOT
  try to recreate them. The `alembic/env.py` include_object hook + the
  baseline migration enforce that.

* **Owned** (ingestion_sources) — net-new in admin-api. Alembic creates and
  evolves it.

Importing this module forces every class to be registered against `Base`, so
`Base.metadata.tables` contains all of them — even though only the owned ones
are creatable. Always import models from `app.models` (not the submodules) so
this side effect runs.
"""

from app.models.ai_interaction import AiInteraction
from app.models.ingestion_source import IngestionSource
from app.models.kbli import Kbli
from app.models.kbli_scrape_target import KbliScrapeTarget
from app.models.magic_link_token import MagicLinkToken
from app.models.user import User

__all__ = [
    "AiInteraction",
    "IngestionSource",
    "Kbli",
    "KbliScrapeTarget",
    "MagicLinkToken",
    "User",
]
