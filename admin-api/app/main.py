"""
BIMA admin-api — FastAPI entrypoint.

Mirrors the structural patterns of `ai-engine/main.py` (request_id middleware,
structured logging, central exception handlers) so a developer moving between
the two services finds familiar conventions.

Routers wired so far:
- health, auth (Sprint B.1)
- dashboard, ai_interactions, kbli (Sprint B.2 — read-only resource endpoints
  for the admin frontend)

Sprint C will add ingestion_sources CRUD, users CRUD, and the unified ingest
worker hooks.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import get_settings
from app.routers import (
    ai_interactions,
    auth,
    case,
    dashboard,
    health,
    ingestion,
    kbli,
    tracking,
)
from app.services.status_reconciler import (
    POLL_INTERVAL_SECONDS as _RECONCILER_INTERVAL,
    run_reconciler_loop,
)

# ---------------------------------------------------------------------------
# Structured logging — same shape as ai-engine so log aggregators can parse
# both services with one regex.
# ---------------------------------------------------------------------------
_settings = get_settings()

logging.basicConfig(
    level=_settings.LOG_LEVEL.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bima_admin_api")


# ---------------------------------------------------------------------------
# Lifespan: startup + shutdown hooks.
#
# Today this only logs; Phase 2 will add (a) a DB ping to fail fast on bad
# DATABASE_URL, (b) an Arq pool init for background jobs.
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "admin-api starting up | cors_origins=%s | trusted_hosts=%s",
        _settings.cors_origins,
        _settings.trusted_hosts,
    )

    # Status reconciler: polls data-pipeline /etl-excel/status and flips
    # ingestion_sources rows out of `processing` once a run finishes. Without
    # this, uploads stay stuck on `processing` forever — there's no callback
    # channel from the pipeline back to admin-api.
    reconciler_stop = asyncio.Event()
    reconciler_task = asyncio.create_task(
        run_reconciler_loop(reconciler_stop), name="ingestion_status_reconciler"
    )

    try:
        yield
    finally:
        logger.info("admin-api shutting down.")
        reconciler_stop.set()
        try:
            await asyncio.wait_for(reconciler_task, timeout=_RECONCILER_INTERVAL + 1)
        except asyncio.TimeoutError:
            reconciler_task.cancel()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="BIMA admin-api",
    description=(
        "Admin API replacing the Laravel/Filament panel. Sprint B.1: auth + "
        "health. Sprint B.2: dashboard, AI interactions, KBLI (read-only). "
        "Sprint C: ingestion-sources + users CRUD."
    ),
    version="0.2.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Security middleware.
#
# ai-engine still has wildcard CORS (its README flags this as a known TODO);
# admin-api ships locked-down from day one. Origins come from env so DevOps
# can swap localhost ↔ admin.nolongin.com without a code change.
# ---------------------------------------------------------------------------
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=_settings.trusted_hosts,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_credentials=False,  # we use bearer tokens, not cookies
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Internal-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID", "X-Response-Time-Ms"],
)


# ---------------------------------------------------------------------------
# Request ID + timing middleware.
#
# Honours an upstream X-Request-ID if Caddy/nginx already set one (so the
# whole chain logs the same id), otherwise generates a uuid4.
# ---------------------------------------------------------------------------
@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    start = time.monotonic()

    response = await call_next(request)

    elapsed_ms = round((time.monotonic() - start) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-Ms"] = str(elapsed_ms)

    logger.info(
        "path=%s method=%s status=%s duration_ms=%s request_id=%s",
        request.url.path,
        request.method,
        response.status_code,
        elapsed_ms,
        request_id,
    )
    return response


# ---------------------------------------------------------------------------
# Global exception handlers.
#
# Keep response bodies opaque on 5xx; full traceback only in logs.
# ---------------------------------------------------------------------------
def _error_body(code: int, message: str, request_id: str | None) -> dict:
    payload: dict = {"status": "error", "code": code, "message": message}
    if request_id:
        payload["request_id"] = request_id
    return payload


@app.exception_handler(RequestValidationError)
async def _on_request_validation(request: Request, exc: RequestValidationError):
    request_id = getattr(request.state, "request_id", None)
    logger.warning(
        "Validation error on %s | request_id=%s | detail=%s",
        request.url.path,
        request_id,
        exc.errors(),
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_error_body(422, "Invalid request payload.", request_id),
    )


@app.exception_handler(ValidationError)
async def _on_pydantic_validation(request: Request, exc: ValidationError):
    request_id = getattr(request.state, "request_id", None)
    logger.warning(
        "Pydantic validation error on %s | request_id=%s | detail=%s",
        request.url.path,
        request_id,
        exc.errors(),
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_error_body(422, "Payload schema mismatch.", request_id),
    )


@app.exception_handler(Exception)
async def _on_unhandled(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", None)
    logger.exception(
        "Unhandled exception on %s | request_id=%s",
        request.url.path,
        request_id,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_error_body(500, "Internal server error.", request_id),
    )


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(dashboard.router, prefix="/dashboard", tags=["Dashboard"])
app.include_router(
    ai_interactions.router,
    prefix="/ai-interactions",
    tags=["AI Interactions"],
)
app.include_router(kbli.router, prefix="/kbli", tags=["KBLI"])
app.include_router(ingestion.router, prefix="/ingestion", tags=["Ingestion"])
app.include_router(tracking.router, prefix="/tracking", tags=["Tracking"])
app.include_router(case.router, prefix="/case", tags=["Case"])
