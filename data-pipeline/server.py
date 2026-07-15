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
  POST /pipeline/siap-corpus    — rebuild SIAP corpus (licences B1/B3 + regulations B2)
  GET  /pipeline/siap-corpus/status

Runs on port 9000 inside the bima-internal Docker network.
"""
import asyncio
import logging
import re
import sys
from datetime import datetime, timezone

from fastapi import BackgroundTasks, Body, FastAPI, status
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


# ── Excel ETL → ChromaDB endpoint ─────────────────────────────────────────────

_etl_state: dict = {
    "running":        False,
    "status":         "idle",
    "started_at":     None,
    "finished_at":    None,
    "last_message":   "",
    # Populated by parsing `[etl] indexed <N> chunks` from the subprocess
    # stdout. None until the subprocess emits that line. Sprint C ingestion
    # UI surfaces this back to the admin.
    "chunks_indexed": None,
}

# Subprocess emits this exact line at the end of a successful run, e.g.
# `[etl] indexed 306 chunks`. Anchor on `[etl]` so log noise doesn't match.
_CHUNKS_INDEXED_RE = re.compile(r"^\[etl\]\s+indexed\s+(\d+)\s+chunks", re.IGNORECASE)

# Belt-and-suspenders: caller-supplied paths must live under this prefix.
# The volume mount already enforces this, but a defensive check guards
# against future misconfiguration.
_ALLOWED_ETL_PATH_PREFIX = "/app/data/"


@app.post("/pipeline/etl-excel", status_code=status.HTTP_202_ACCEPTED)
async def trigger_etl_excel(
    background_tasks: BackgroundTasks,
    body: dict | None = Body(default=None),
) -> JSONResponse:
    """
    Run etl_pipeline.py to rebuild ChromaDB oss_regulations from Excel data.

    Body (optional, JSON):
        kbli_path:   abs path to the KBLI .xlsx (under /app/data/)
        pb_umku_path: abs path to the PB UMKU .xlsx (under /app/data/)

    Either or both may be supplied. Omitted paths fall back to the hardcoded
    defaults so callers that don't set a body keep working unchanged.

    Non-blocking — returns 409 if already running.
    """
    if _etl_state["running"]:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"message": "ETL pipeline already running", **_etl_state},
        )

    kbli_path = (body or {}).get("kbli_path") if isinstance(body, dict) else None
    pb_umku_path = (body or {}).get("pb_umku_path") if isinstance(body, dict) else None

    for label, p in (("kbli_path", kbli_path), ("pb_umku_path", pb_umku_path)):
        if p is None:
            continue
        if not isinstance(p, str) or not p.startswith(_ALLOWED_ETL_PATH_PREFIX):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "message": f"{label} must be a string under {_ALLOWED_ETL_PATH_PREFIX}.",
                    "received": p,
                },
            )

    background_tasks.add_task(_run_etl_pipeline, kbli_path, pb_umku_path)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "message": "ETL pipeline started",
            "status": "accepted",
            "kbli_path": kbli_path,
            "pb_umku_path": pb_umku_path,
        },
    )


@app.get("/pipeline/etl-excel/status")
def etl_pipeline_status() -> dict:
    return dict(_etl_state)


async def _run_etl_pipeline(
    kbli_path: str | None = None,
    pb_umku_path: str | None = None,
) -> None:
    """Run etl_pipeline.py as a subprocess with optional per-file overrides."""
    _etl_state["running"]        = True
    _etl_state["status"]         = "running"
    _etl_state["started_at"]     = datetime.now(timezone.utc).isoformat()
    _etl_state["finished_at"]    = None
    _etl_state["chunks_indexed"] = None
    logger.info(
        "ETL Excel pipeline subprocess started | kbli=%s | pb_umku=%s",
        kbli_path,
        pb_umku_path,
    )

    argv: list[str] = [sys.executable, "etl_pipeline.py"]
    if kbli_path:
        argv.extend(["--kbli-path", kbli_path])
    if pb_umku_path:
        argv.extend(["--pb-umku-path", pb_umku_path])

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd="/app",
        )

        output_lines = []
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").rstrip()
            output_lines.append(line)
            logger.info("[etl_excel] %s", line)

            # Parse the chunk-count beacon as soon as it shows up.
            m = _CHUNKS_INDEXED_RE.search(line)
            if m:
                try:
                    _etl_state["chunks_indexed"] = int(m.group(1))
                except ValueError:
                    pass

        rc = await proc.wait()
        _etl_state["status"]       = "done" if rc == 0 else "error"
        _etl_state["last_message"] = output_lines[-1] if output_lines else f"exit={rc}"
        logger.info("ETL Excel pipeline finished | returncode=%d", rc)

        # Beacon-drift guard: if the ETL succeeded but our regex never matched,
        # the admin's live counter will silently render 0. Loud-warn so the
        # next deploy catches a beacon format change before demo day.
        if rc == 0 and _etl_state["chunks_indexed"] is None:
            logger.warning(
                "ETL completed but chunks_indexed beacon missing — "
                "admin live counter will show 0"
            )

    except Exception as exc:
        logger.exception("ETL Excel pipeline raised an exception")
        _etl_state["status"]       = "error"
        _etl_state["last_message"] = str(exc)

    finally:
        _etl_state["running"]     = False
        _etl_state["finished_at"] = datetime.now(timezone.utc).isoformat()


# ── SIAP corpus (licences B1/B3 + regulations B2) → ChromaDB ──────────────────

_siap_corpus_state: dict = {
    "running":      False,
    "status":       "idle",   # idle | running | done | error
    "started_at":   None,
    "finished_at":  None,
    "last_message": "",
    "scope":        None,
    "licences":     None,
    "regulations":  None,
}

# siap_corpus.py emits this exact beacon on success, e.g.
# `[siap-corpus] done scope=all licences=379 regulations=56`. Anchor on the
# bracketed tag so ordinary log noise never matches.
_SIAP_CORPUS_DONE_RE = re.compile(
    r"\[siap-corpus\]\s+done\s+scope=(\S+)\s+licences=(\d+)\s+regulations=(\d+)"
)


@app.post("/pipeline/siap-corpus", status_code=status.HTTP_202_ACCEPTED)
async def trigger_siap_corpus(
    background_tasks: BackgroundTasks,
    body: dict | None = Body(default=None),
) -> JSONResponse:
    """
    Rebuild the SIAP corpus (BIMA's RAG source-of-truth) from SIAP's own DB —
    persyaratan + workflow (B1/B3) and the regulation registry (B2). Additive,
    idempotent upsert on stable ids; the KBLI/PB-UMKU chunks are untouched.

    Body (optional, JSON):
        scope:            "all" (default) | "licenses" | "regulations"
        include_inactive: bool — also ingest de-activated regulations (default false)

    Non-blocking — returns 409 if a build is already running.
    """
    if _siap_corpus_state["running"]:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"message": "SIAP corpus build already running", **_siap_corpus_state},
        )

    scope = (body or {}).get("scope", "all") if isinstance(body, dict) else "all"
    if scope not in ("all", "licenses", "regulations"):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "message": 'scope must be "all", "licenses", or "regulations".',
                "received": scope,
            },
        )
    include_inactive = (
        bool((body or {}).get("include_inactive", False)) if isinstance(body, dict) else False
    )

    background_tasks.add_task(_run_siap_corpus, scope, include_inactive)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "message": "SIAP corpus build started",
            "status": "accepted",
            "scope": scope,
            "include_inactive": include_inactive,
        },
    )


@app.get("/pipeline/siap-corpus/status")
def siap_corpus_status() -> dict:
    return dict(_siap_corpus_state)


async def _run_siap_corpus(scope: str, include_inactive: bool) -> None:
    """Run siap_corpus.py as a subprocess and surface the upsert counts."""
    _siap_corpus_state.update({
        "running":     True,
        "status":      "running",
        "started_at":  datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "scope":       scope,
        "licences":    None,
        "regulations": None,
    })
    logger.info(
        "SIAP corpus build started | scope=%s | include_inactive=%s",
        scope, include_inactive,
    )

    argv: list[str] = [sys.executable, "siap_corpus.py", "--scope", scope]
    if include_inactive:
        argv.append("--include-inactive")

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd="/app",
        )
        output_lines = []
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").rstrip()
            output_lines.append(line)
            logger.info("[siap_corpus] %s", line)
            m = _SIAP_CORPUS_DONE_RE.search(line)
            if m:
                _siap_corpus_state["licences"]    = int(m.group(2))
                _siap_corpus_state["regulations"] = int(m.group(3))

        rc = await proc.wait()
        _siap_corpus_state["status"]       = "done" if rc == 0 else "error"
        _siap_corpus_state["last_message"] = output_lines[-1] if output_lines else f"exit={rc}"
        logger.info("SIAP corpus build finished | returncode=%d", rc)

    except Exception as exc:
        logger.exception("SIAP corpus build raised an exception")
        _siap_corpus_state["status"]       = "error"
        _siap_corpus_state["last_message"] = str(exc)

    finally:
        _siap_corpus_state["running"]     = False
        _siap_corpus_state["finished_at"] = datetime.now(timezone.utc).isoformat()


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
