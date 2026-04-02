"""
BIMA-AI – Data Pipeline Server

Persistent FastAPI microservice that wraps the Playwright scraper.
Exposes HTTP endpoints so the Filament admin panel can trigger pipeline runs
and query progress without needing direct Docker access.

Runs on port 9000 inside the bima-internal Docker network.
"""
import asyncio
import logging
import sys
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, status
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("pipeline_server")

app = FastAPI(title="BIMA-AI Pipeline Server", version="0.1.0")

# ── State ────────────────────────────────────────────────────────────────────

_state = {
    "running":    False,
    "status":     "idle",          # idle | running | done | error
    "started_at": None,
    "finished_at": None,
    "last_message": "",
}


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "pipeline": _state["status"], "running": _state["running"]}


@app.get("/pipeline/status")
def pipeline_status() -> dict:
    return dict(_state)


@app.post("/pipeline/trigger", status_code=status.HTTP_202_ACCEPTED)
async def trigger_pipeline(
    background_tasks: BackgroundTasks,
    limit: int = 35,
) -> JSONResponse:
    """
    Trigger a dynamic pipeline run for up to `limit` pending KBLI targets.
    Non-blocking — the scraper runs in a background task.
    Returns 409 if already running.
    """
    if _state["running"]:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"status": "already_running", "message": "Pipeline is already running."},
        )

    background_tasks.add_task(_run_pipeline, limit)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status":  "started",
            "message": f"Pipeline triggered for up to {limit} pending KBLI targets.",
        },
    )


# ── Background pipeline runner ────────────────────────────────────────────────

async def _run_pipeline(limit: int) -> None:
    _state["running"]    = True
    _state["status"]     = "running"
    _state["started_at"] = datetime.now(timezone.utc).isoformat()
    _state["finished_at"] = None
    logger.info("Pipeline started | limit=%d", limit)

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "run_pipeline_dynamic.py",
            f"--limit={limit}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd="/app",
        )
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").rstrip()
            logger.info("[scraper] %s", line)
            _state["last_message"] = line

        rc = await proc.wait()
        _state["status"] = "done" if rc == 0 else "error"
        logger.info("Pipeline finished | returncode=%d", rc)

    except Exception as exc:
        _state["status"]       = "error"
        _state["last_message"] = str(exc)
        logger.exception("Pipeline subprocess raised an exception")

    finally:
        _state["running"]     = False
        _state["finished_at"] = datetime.now(timezone.utc).isoformat()
