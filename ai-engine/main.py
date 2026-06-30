"""
BIMA-AI – FastAPI Engine
Entry point: configures security middleware, structured logging,
global exception handlers, and mounts all routers.
"""

import asyncio
import logging
import os
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

from logging_setup import configure_logging, mask_url_secrets
from routers import aptana, copilot, downloads, notify, telegram, validator, vectorize, webhooks
from services.transparency_poller import run_transparency_poller_loop

# ---------------------------------------------------------------------------
# Logging – structured, to stdout so it flows into any log aggregator.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
# Install secret-redacting filters on outbound-HTTP loggers (httpx etc.) BEFORE
# the first httpx call. Otherwise the Telegram bot token / Meta access token
# would already be in stdout by the time we got around to redacting.
configure_logging()
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

    # Durable chat sessions (Redis): probe connectivity once at startup so the
    # logs say up-front whether guided-submission + officer sessions will
    # survive a restart. No-op (logs "disabled") when the flag is off; never
    # fatal — session_store degrades to in-memory on any failure.
    try:
        from services import session_store
        await session_store.ping()
    except Exception:
        logger.exception("session_store startup ping raised (non-fatal)")

    # Officer-directory cache (flow-based notify): warm the in-memory set of
    # registered officer WhatsApp numbers so the first inbound officer message
    # hits a warm cache instead of a cold sync miss. is_officer_channel_id
    # refreshes it on a TTL via a non-blocking background task thereafter.
    # Never fatal — degrades to the BIMA_OFFICER_WA_PHONE env fallback.
    try:
        from services import officer_bridge
        await officer_bridge.warm_officer_cache()
    except Exception:
        logger.exception("officer cache warm raised (non-fatal)")

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
# /docs, /redoc and the OpenAPI schema are a free recon map of every route +
# auth surface; expose them only when BIMA_ENABLE_DOCS is explicitly truthy
# (dev), never by default in production.
_DOCS_ON = os.getenv("BIMA_ENABLE_DOCS", "").strip().lower() in {"1", "true", "yes", "on"}
app = FastAPI(
    title="BIMA-AI Engine",
    description="Omnichannel AI orchestrator for DPMPTSP licensing assistant.",
    version="0.1.0",
    docs_url="/docs" if _DOCS_ON else None,
    redoc_url="/redoc" if _DOCS_ON else None,
    openapi_url="/openapi.json" if _DOCS_ON else None,
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Security middleware
#
# Both allow-lists are env-driven so DevOps can rotate domains (e.g.
# nolongin.com → bimaptsp.com cutover) without a code change. We deliberately
# default to a *safe* list — never "*" — so a fresh deploy with no env vars
# set still rejects unknown Host headers and CORS origins instead of silently
# accepting attackers' DNS rebinding / cross-site requests.
#
# TRUSTED_HOSTS:        comma-separated hostnames (no scheme), Starlette
#                       supports wildcard subdomain entries like "*.foo.com".
# CORS_ALLOW_ORIGINS:   comma-separated full origins (scheme + host).
# ---------------------------------------------------------------------------

# Hard-coded fallbacks. These mirror today's production reality (the two
# domains BIMA serves under) plus localhost for dev. Changing this list is a
# code change on purpose — env override is the supported runtime knob.
_DEFAULT_TRUSTED_HOSTS: list[str] = [
    "nolongin.com",
    "*.nolongin.com",
    "bimaptsp.com",
    "*.bimaptsp.com",
    "localhost",
]
_DEFAULT_CORS_ORIGINS: list[str] = [
    "https://portal.nolongin.com",
    "https://admin.nolongin.com",
    "https://portal.bimaptsp.com",
    "https://admin.bimaptsp.com",
]


def _parse_csv_env(name: str, default: list[str]) -> list[str]:
    """
    Parse a comma-separated env var into a clean list.

    Returns the supplied default when the env var is unset or — after stripping
    whitespace and empties — yields nothing. This is the "no env = safe
    default" contract: an operator who forgets the var still gets a locked-down
    allow-list, never a wildcard.
    """
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or list(default)


_TRUSTED_HOSTS = _parse_csv_env("TRUSTED_HOSTS", _DEFAULT_TRUSTED_HOSTS)
_CORS_ALLOW_ORIGINS = _parse_csv_env("CORS_ALLOW_ORIGINS", _DEFAULT_CORS_ORIGINS)

logger.info(
    "security middleware configured | trusted_hosts=%s | cors_origins=%s",
    _TRUSTED_HOSTS,
    _CORS_ALLOW_ORIGINS,
)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=_TRUSTED_HOSTS,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ALLOW_ORIGINS,
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

    # Mask path-secret segments in webhook URLs before logging — otherwise
    # /webhook/telegram/<secret> and /webhook/aptana/inbound/<secret> get
    # written to stdout on every hit and any operator with log access can
    # forge inbound traffic. See logging_setup.mask_url_secrets for the
    # specific patterns.
    logger.info(
        "path=%s method=%s status=%s duration_ms=%s request_id=%s",
        mask_url_secrets(request.url.path),
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
app.include_router(telegram.router, tags=["Telegram"])
app.include_router(validator.router)
app.include_router(copilot.router)
app.include_router(notify.router)
app.include_router(downloads.router, tags=["Downloads"])


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
