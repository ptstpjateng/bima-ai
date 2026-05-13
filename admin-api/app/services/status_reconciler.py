"""
Status reconciler — flips `ingestion_sources` rows out of `processing`.

The ingestion router triggers data-pipeline ETL via best-effort POST and marks
the row `processing` when the pipeline accepts (202). But the pipeline has no
callback channel — admin-api must poll `/pipeline/etl-excel/status` to learn
when the run finished and update the row to `done` / `failed`.

This module runs a single background asyncio task during app lifespan that
ticks every POLL_INTERVAL_SECONDS, fetches the pipeline state, and reconciles
any `processing` rows whose `last_run_at` falls inside the run's window.

Match heuristic (MVP):
  - pipeline reports running=False
  - pipeline reports a terminal status (`done` | `error`) with finished_at
  - row.last_run_at is within [started_at - 60s, finished_at]
The 60s pre-start tolerance accounts for the gap between admin-api stamping
`last_run_at` (T0) and pipeline-side `started_at` (T0 + ~100ms). Rows whose
last_run_at is much earlier than started_at are considered orphans from a
prior crashed admin-api and are NOT flipped here — admin clicks "Proses ulang"
to recover. Avoids the failure mode where a fresh successful run silently
"completes" an unrelated, hours-old, never-actually-ran row.

Status mapping: pipeline `done` → row `done`; pipeline `error` → row `failed`.
chunks_indexed is copied through on success only.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import update

from app.db import SessionFactory
from app.models import IngestionSource

logger = logging.getLogger("bima_admin_api.reconciler")

DATA_PIPELINE_URL = os.environ.get("DATA_PIPELINE_URL", "http://data-pipeline:9000")
POLL_INTERVAL_SECONDS = 5.0
_FETCH_TIMEOUT_SECONDS = 2.0
# Tolerance for the gap between row.last_run_at (admin-api side) and
# pipeline.started_at (data-pipeline side). The two clocks are different
# processes; small skew is fine. 60s is generous.
_PRE_START_TOLERANCE = timedelta(seconds=60)


async def _fetch_pipeline_status() -> dict | None:
    """Pull the ETL state from data-pipeline. Soft-fail with None on any error."""
    url = DATA_PIPELINE_URL.rstrip("/") + "/pipeline/etl-excel/status"
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_SECONDS) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return None
        return resp.json()
    except (httpx.TimeoutException, httpx.RequestError, ValueError):
        return None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _reconcile_once() -> None:
    """One reconciliation tick. Idempotent; safe to call repeatedly."""
    live = await _fetch_pipeline_status()
    if not live:
        return
    if live.get("running"):
        return  # in-flight; nothing to flip

    raw_status = live.get("status")
    if raw_status not in ("done", "error"):
        return

    finished_at = _parse_iso(live.get("finished_at"))
    started_at = _parse_iso(live.get("started_at"))
    if not finished_at or not started_at:
        return

    new_status = "done" if raw_status == "done" else "failed"
    chunks = live.get("chunks_indexed")
    last_message = live.get("last_message") or None
    error_text = None if new_status == "done" else (last_message or "ETL failed")

    window_start = started_at - _PRE_START_TOLERANCE

    async with SessionFactory() as db:
        values: dict = {"status": new_status, "error": error_text}
        if new_status == "done" and isinstance(chunks, int):
            values["chunks_indexed"] = chunks

        stmt = (
            update(IngestionSource)
            .where(
                IngestionSource.status == "processing",
                IngestionSource.last_run_at.isnot(None),
                IngestionSource.last_run_at >= window_start,
                IngestionSource.last_run_at <= finished_at,
            )
            .values(**values)
        )
        result = await db.execute(stmt)
        await db.commit()
        if result.rowcount:
            logger.info(
                "reconciler flipped %d row(s) → %s (chunks=%s)",
                result.rowcount,
                new_status,
                chunks if new_status == "done" else "n/a",
            )


async def run_reconciler_loop(stop_event: asyncio.Event) -> None:
    """Background loop: tick every POLL_INTERVAL_SECONDS until stop_event is set."""
    logger.info(
        "status reconciler started | interval=%.1fs | pipeline=%s",
        POLL_INTERVAL_SECONDS,
        DATA_PIPELINE_URL,
    )
    while not stop_event.is_set():
        try:
            await _reconcile_once()
        except Exception:
            logger.exception("reconciler tick crashed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass  # normal tick interval
    logger.info("status reconciler stopped")
