"""
BIMA-AI – FastAPI Engine
Entry point: configures security middleware, structured logging,
global exception handlers, and mounts all routers.
"""

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

from routers import vectorize, webhooks
from services.telegram_polling import run_polling

# ---------------------------------------------------------------------------
# Logging – structured, to stdout so it flows into any log aggregator.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bima_ai")


# ---------------------------------------------------------------------------
# Lifespan: startup / shutdown hooks (replaces deprecated @app.on_event).
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("BIMA-AI engine starting up.")
    polling_task = asyncio.create_task(run_polling())
    yield
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass
    logger.info("BIMA-AI engine shutting down.")


# ---------------------------------------------------------------------------
# App instance
# ---------------------------------------------------------------------------
app = FastAPI(
    title="BIMA-AI Engine",
    description="Omnichannel AI orchestrator for DPMPTSP licensing assistant.",
    version="0.1.0",
    # Disable automatic /docs and /redoc in production by checking env; kept
    # enabled here for development convenience – override via env if needed.
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Security middleware
# ---------------------------------------------------------------------------

# Only accept requests from known hosts (extend list via env in production).
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*"],  # Tighten to specific domains before going to prod.
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict to frontend origin(s) in production.
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request ID + timing middleware – attaches a trace ID to every request.
# ---------------------------------------------------------------------------
@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    request_id = str(uuid.uuid4())
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
# Global exception handlers
# ---------------------------------------------------------------------------

def _error_body(status_code: int, message: str, request_id: str | None = None) -> dict:
    payload: dict = {"status": "error", "code": status_code, "message": message}
    if request_id:
        payload["request_id"] = request_id
    return payload


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
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
async def pydantic_exception_handler(request: Request, exc: ValidationError):
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
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", None)
    # Log with full traceback but never expose internals to the caller.
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
app.include_router(webhooks.router, tags=["Webhooks"])
app.include_router(vectorize.router, tags=["Vectorize"])


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}
