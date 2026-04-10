"""
BIMA-AI – Data Pipeline Server

Persistent FastAPI microservice that wraps the Playwright scraper and the
multi-agent PDF parsing pipeline.

Endpoints
---------
  GET  /health                  — liveness check
  GET  /pipeline/status         — KBLI scraper run state
  POST /pipeline/trigger        — trigger KBLI scraper (Playwright)
  POST /pipeline/pdf/{job_id}   — trigger PDF multi-agent pipeline for one job
  GET  /pipeline/pdf/{job_id}   — status of a running PDF pipeline job

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

app = FastAPI(title="BIMA-AI Pipeline Server", version="0.2.0")

# ── Shared run state (KBLI scraper) ──────────────────────────────────────────

_state = {
    "running":      False,
    "status":       "idle",   # idle | running | done | error
    "started_at":   None,
    "finished_at":  None,
    "last_message": "",
}

# ── PDF job state: job_id → { running, status, started_at, finished_at, pid } ─

_pdf_jobs: dict[int, dict] = {}


# ── KBLI Scraper routes ───────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    return {
        "status":       "ok",
        "pipeline":     _state["status"],
        "running":      _state["running"],
        "pdf_jobs_active": sum(1 for j in _pdf_jobs.values() if j["running"]),
    }


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
    Non-blocking — returns 409 if already running.
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


# ── PDF Multi-Agent Pipeline routes ──────────────────────────────────────────

@app.post("/pipeline/pdf/{job_id}", status_code=status.HTTP_202_ACCEPTED)
async def trigger_pdf_pipeline(
    job_id: int,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """
    Trigger the multi-agent PDF parsing pipeline for a specific job.

    The Filament admin panel calls this after uploading PDFs. The pipeline runs
    asynchronously — poll GET /pipeline/pdf/{job_id} or the Filament view page
    (which polls the PostgreSQL status via the backend) for progress.

    Returns 409 if this job is already running.
    """
    existing = _pdf_jobs.get(job_id)
    if existing and existing.get("running"):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"status": "already_running", "job_id": job_id,
                     "message": f"PDF job {job_id} is already running."},
        )

    _pdf_jobs[job_id] = {
        "running":     True,
        "status":      "running",
        "started_at":  datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "exit_code":   None,
    }

    background_tasks.add_task(_run_pdf_pipeline, job_id)
    logger.info("PDF pipeline triggered | job_id=%d", job_id)

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status":  "started",
            "job_id":  job_id,
            "message": f"PDF pipeline started for job #{job_id}. Check Filament for progress.",
        },
    )


@app.get("/pipeline/pdf/{job_id}")
def pdf_pipeline_status(job_id: int) -> JSONResponse:
    """Return local run state for a PDF pipeline job."""
    if job_id not in _pdf_jobs:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"status": "not_found", "job_id": job_id},
        )
    return JSONResponse(content={"job_id": job_id, **_pdf_jobs[job_id]})


# ── Background runners ────────────────────────────────────────────────────────

async def _run_pipeline(limit: int) -> None:
    """Run the Playwright KBLI scraper as a subprocess."""
    _state["running"]     = True
    _state["status"]      = "running"
    _state["started_at"]  = datetime.now(timezone.utc).isoformat()
    _state["finished_at"] = None
    logger.info("KBLI scraper started | limit=%d", limit)

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
        logger.info("KBLI scraper finished | returncode=%d", rc)

    except Exception as exc:
        _state["status"]       = "error"
        _state["last_message"] = str(exc)
        logger.exception("KBLI scraper raised an exception")

    finally:
        _state["running"]     = False
        _state["finished_at"] = datetime.now(timezone.utc).isoformat()


async def _run_pdf_pipeline(job_id: int) -> None:
    """Run pdf_agent_pipeline.py as a subprocess for one job."""
    logger.info("PDF pipeline subprocess started | job_id=%d", job_id)

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "pdf_agent_pipeline.py",
            f"--job-id={job_id}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd="/app",
        )

        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").rstrip()
            logger.info("[pdf_agent job=%d] %s", job_id, line)

        rc = await proc.wait()
        _pdf_jobs[job_id].update({
            "running":     False,
            "status":      "done" if rc == 0 else "error",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "exit_code":   rc,
        })
        logger.info("PDF pipeline finished | job_id=%d | returncode=%d", job_id, rc)

    except Exception as exc:
        logger.exception("PDF pipeline raised an exception | job_id=%d", job_id)
        _pdf_jobs[job_id].update({
            "running":     False,
            "status":      "error",
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })
