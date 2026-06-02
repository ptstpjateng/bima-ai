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

from routers import aptana, copilot, meta_whatsapp, notify, validator, vectorize, webhooks
from services.transparency_poller import run_transparency_poller_loop

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
# Grace period for the transparency poller to wind down on shutdown — must
# comfortably exceed one poll interval so an in-flight tick can finish.
_POLLER_SHUTDOWN_TIMEOUT_SECONDS = 15.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("BIMA-AI engine starting up.")

    # Transparency-notification poller: a background task that polls SIAP's
    # license-request changes feed and dispatches proactive citizen WhatsApp
    # updates. The task is always created; it no-ops every tick until
    # BIMA_TRANSPARENCY_POLLER_ENABLED is set, so this is safe to ship before
    # the SIAP events token is provisioned. See services/transparency_poller.py.
    poller_stop = asyncio.Event()
    poller_task = asyncio.create_task(
        run_transparency_poller_loop(poller_stop), name="transparency_poller"
    )

    try:
        yield
    finally:
        logger.info("BIMA-AI engine shutting down.")
        poller_stop.set()
        try:
            await asyncio.wait_for(
                poller_task, timeout=_POLLER_SHUTDOWN_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning("transparency poller did not stop in time — cancelling")
            poller_task.cancel()


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
app.include_router(aptana.router, tags=["APTANA WhatsApp"])
app.include_router(meta_whatsapp.router, tags=["Meta WhatsApp (Direct)"])
app.include_router(validator.router)
app.include_router(copilot.router)
app.include_router(notify.router)


# ---------------------------------------------------------------------------
# Health check — A4: expose ChromaDB chunk count + model info
# ---------------------------------------------------------------------------
@app.get("/health", include_in_schema=False)
async def health():
    import asyncio
    from services.ai_handler import _GEMINI_MODEL, _GEMINI_INTENT_MODEL

    # Query ChromaDB in a thread so we don't block the event loop
    def _chroma_count() -> tuple[int, str]:
        try:
            import chromadb
            import os
            path = os.getenv("CHROMA_DB_PATH", "/app/chroma_db")
            client = chromadb.PersistentClient(path=path)
            col = client.get_collection("oss_regulations")
            return col.count(), "ok"
        except Exception as exc:
            return 0, str(exc)[:120]

    chunk_count, chroma_status = await asyncio.get_event_loop().run_in_executor(None, _chroma_count)

    return {
        "status": "ok",
        "model": _GEMINI_MODEL,
        "intent_model": _GEMINI_INTENT_MODEL,
        "chroma_chunks": chunk_count,
        "chroma_status": chroma_status,
    }
