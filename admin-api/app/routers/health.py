"""
Liveness + readiness probes.

`/health` is a cheap liveness check: the process is up and middleware works.
`/health/deep` is the readiness probe — pings Postgres + data-pipeline so a
pre-demo `curl` can fail loudly when a subsystem is wedged. We deliberately
DO NOT replace `/health` with the deep variant: Caddy + the docker-compose
healthcheck rely on the cheap one staying cheap (no fan-out, no DB hit).
"""

from __future__ import annotations

import logging
import os
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db

logger = logging.getLogger("bima_admin_api.health")

router = APIRouter()

# Match the reconciler's pipeline URL convention (env override → docker DNS
# default) so a single env var change moves both probes together.
_DATA_PIPELINE_URL = os.environ.get("DATA_PIPELINE_URL", "http://data-pipeline:9000")
# 2s budget — same as the dashboard's ai-engine probe. Anything slower means
# the upstream is in trouble and we should fail-fast rather than hang the probe.
_DEEP_PROBE_TIMEOUT_SECONDS = 2.0


@router.get("/health", include_in_schema=False, tags=["Health"])
async def health() -> dict[str, str]:
    """Liveness: returns immediately if the FastAPI process is responsive."""
    return {"status": "ok", "service": "admin-api"}


async def _probe_db(db: AsyncSession) -> bool:
    """Cheap `SELECT 1` — confirms the asyncpg pool can reach Postgres."""
    try:
        await db.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("deep health: db ping failed | err=%s", exc)
        return False


async def _probe_pipeline() -> bool:
    """Hit data-pipeline /pipeline/etl-excel/status — same path the reconciler uses."""
    url = _DATA_PIPELINE_URL.rstrip("/") + "/pipeline/etl-excel/status"
    try:
        async with httpx.AsyncClient(timeout=_DEEP_PROBE_TIMEOUT_SECONDS) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning(
                "deep health: pipeline non-200 | url=%s | status=%s", url, resp.status_code
            )
            return False
        return True
    except (httpx.TimeoutException, httpx.RequestError, ValueError) as exc:
        logger.warning("deep health: pipeline unreachable | url=%s | err=%s", url, exc)
        return False


@router.get("/health/deep", include_in_schema=False, tags=["Health"])
async def health_deep(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """
    Readiness: pings PostgreSQL + data-pipeline. 200 if both OK, 503 if any down.

    Used by pre-demo smoke checks. Never gated by auth — operators need to call
    it from a terminal without a token.
    """
    db_ok = await _probe_db(db)
    pipeline_ok = await _probe_pipeline()
    overall_ok = db_ok and pipeline_ok

    body = {
        "status": "ok" if overall_ok else "degraded",
        "service": "admin-api",
        "db": "ok" if db_ok else "fail",
        "pipeline": "ok" if pipeline_ok else "fail",
    }
    return JSONResponse(
        status_code=status.HTTP_200_OK if overall_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=body,
    )
