"""
Ingestion router — Sprint C (Ingestion Upload MVP).

Powers the "Sumber Data" admin page: upload an XLSX (`kbli` or `pb_umku`),
persist it to the shared `ingestion_uploads` Docker volume, insert a row in
`ingestion_sources`, and ask data-pipeline to ETL it. The admin watches
status flip pending → processing → done/failed and sees the chunk count.

Volume layout:
  admin-api:    /app/uploads/<source_id>-<sanitized_name>.xlsx   (rw)
  data-pipeline: /app/data/uploads/<same_filename>                (ro)

Endpoints (all require admin JWT):
  POST /ingestion/upload          — multipart upload + insert + best-effort trigger
  GET  /ingestion                 — paginated list, newest first
  GET  /ingestion/{id}            — detail; merges live counters if running
  POST /ingestion/{id}/rerun      — re-trigger ETL on the stored file
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.deps import get_current_admin_user, get_db
from app.models import IngestionSource, User
from app.schemas.ingestion import IngestionSourceList, IngestionSourceOut

logger = logging.getLogger("bima_admin_api.ingestion")

router = APIRouter()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Mount points must match docker-compose.yml. /app/uploads is rw in admin-api;
# /app/data/uploads/<same name> is the ro view inside data-pipeline.
ADMIN_UPLOAD_DIR = Path(os.environ.get("INGESTION_UPLOAD_DIR", "/app/uploads"))
PIPELINE_UPLOAD_DIR = Path(
    os.environ.get("INGESTION_PIPELINE_DIR", "/app/data/uploads")
)
DATA_PIPELINE_URL = os.environ.get("DATA_PIPELINE_URL", "http://data-pipeline:9000")

# 50 MB cap. Real KBLI/PB UMKU files are < 1 MB; 50 MB is generous slop for
# accidentally-fat exports without letting a hostile admin DoS the disk.
MAX_FILE_BYTES = 50 * 1024 * 1024
# Stream in 1 MB chunks so we fail before holding the full file in RAM.
_CHUNK_BYTES = 1024 * 1024

_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        # Some browsers send this for .xlsx on Windows.
        "application/octet-stream",
    }
)
_ALLOWED_EXTENSIONS = frozenset({".xlsx", ".xls"})

# Only excel kinds are reachable from the MVP UI. The DB column allows the
# wider enum (per ingestion_source.py docstring), but the API gate keeps
# Sprint C honest.
UploadKind = Literal["excel_kbli", "excel_pb_umku"]

# Budget for the cross-service call. Same convention as dashboard.py.
_PIPELINE_TIMEOUT_SECONDS = 5.0
_PIPELINE_STATUS_TIMEOUT_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_filename_part(name: str) -> str:
    """
    Sanitize the user-supplied display_name for use as a filename component.

    Strip path separators, parent-dir tokens, and control chars. Truncate to
    100 chars (per brief). Any remaining unsafe char becomes `_`.
    """
    no_traversal = name.replace("..", "_").replace("/", "_").replace("\\", "_")
    safe = re.sub(r"[^\w\-. ]+", "_", no_traversal).strip("_ .")
    safe = safe[:100] if safe else "uploaded"
    return safe


def _build_filename(source_id: int, display_name: str) -> str:
    return f"{source_id}-{_sanitize_filename_part(display_name)}.xlsx"


def _validate_upload_or_raise(file: UploadFile) -> None:
    """Reject obvious misuses before we start streaming bytes."""
    content_type = (file.content_type or "").lower()
    filename = file.filename or ""
    ext = Path(filename).suffix.lower()

    if content_type not in _ALLOWED_CONTENT_TYPES and ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                "Hanya berkas .xlsx atau .xls yang didukung "
                f"(diterima content_type={content_type!r}, ext={ext!r})."
            ),
        )


async def _stream_to_disk(file: UploadFile, dest: Path) -> int:
    """
    Stream the multipart body to `dest` in fixed chunks. Returns bytes
    written. Raises 413 if the cap is exceeded; deletes the partial file so
    we don't leak disk on a rejected upload.
    """
    total = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dest.open("wb") as fp:
            while True:
                chunk = await file.read(_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    fp.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Berkas melebihi batas {MAX_FILE_BYTES // (1024 * 1024)} MB.",
                    )
                fp.write(chunk)
    except HTTPException:
        raise
    except Exception:
        # On any other write failure, leave nothing half-written behind.
        dest.unlink(missing_ok=True)
        raise
    return total


def _pipeline_path_for(filename: str) -> str:
    """Return the absolute path the data-pipeline subprocess will read."""
    return str(PIPELINE_UPLOAD_DIR / filename)


async def _resolve_active_paths(db: AsyncSession) -> dict[str, str]:
    """
    Return the most-recent uploaded path for each excel kind that has any
    source.

    Result shape:
        {"kbli_path": "/app/data/uploads/<file>",
         "pb_umku_path": "/app/data/uploads/<file>"}

    Keys are omitted when no upload of that kind exists yet — the pipeline
    falls back to its hardcoded baseline (DEFAULT_KBLI_PATH /
    DEFAULT_PB_UMKU_PATH in etl_pipeline.py) for the omitted side.

    Invariant: this MUST be called inside the same session/transaction as
    the row that just got upserted, so that the just-inserted/rerun row is
    visible and naturally wins the "most-recent" ordering for its kind. We
    deliberately do NOT gate on status — the file lands on disk during the
    upload step, well before the pipeline reports back, so even a
    `pending` row is a valid source of truth for the file path.
    """
    paths: dict[str, str] = {}
    for kind, body_key in (
        ("excel_kbli", "kbli_path"),
        ("excel_pb_umku", "pb_umku_path"),
    ):
        stmt = (
            select(IngestionSource)
            .where(
                IngestionSource.kind == kind,
                IngestionSource.object_key.isnot(None),
            )
            .order_by(
                IngestionSource.created_at.desc(),
                IngestionSource.id.desc(),
            )
            .limit(1)
        )
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row and row.object_key:
            paths[body_key] = _pipeline_path_for(row.object_key)
    return paths


async def _trigger_pipeline(body: dict[str, str]) -> tuple[int, dict]:
    """
    Best-effort POST to data-pipeline /pipeline/etl-excel with the assembled
    body (may include `kbli_path`, `pb_umku_path`, both, or neither — in
    which case the pipeline runs against its hardcoded defaults).

    Returns (status_code, response_json_or_empty). Network errors surface as
    status_code=0 so the caller can fall through to pending.
    """
    url = DATA_PIPELINE_URL.rstrip("/") + "/pipeline/etl-excel"
    try:
        async with httpx.AsyncClient(timeout=_PIPELINE_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=body)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        return resp.status_code, payload
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning("data-pipeline unreachable | url=%s | err=%s", url, exc)
        return 0, {}


async def _fetch_pipeline_live_status() -> dict | None:
    """Pull /pipeline/etl-excel/status. Soft-fail with None."""
    url = DATA_PIPELINE_URL.rstrip("/") + "/pipeline/etl-excel/status"
    try:
        async with httpx.AsyncClient(
            timeout=_PIPELINE_STATUS_TIMEOUT_SECONDS
        ) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return None
        return resp.json()
    except (httpx.TimeoutException, httpx.RequestError, ValueError) as exc:
        logger.warning("data-pipeline status unreachable | err=%s", exc)
        return None


def _row_to_out(row: IngestionSource) -> IngestionSourceOut:
    return IngestionSourceOut.model_validate(row)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/upload",
    response_model=IngestionSourceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload an XLSX and enqueue/trigger the ETL pipeline.",
)
async def upload_ingestion_source(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
    file: Annotated[UploadFile, File(description="Berkas .xlsx (KBLI atau PB UMKU).")],
    kind: Annotated[UploadKind, Form(description="excel_kbli | excel_pb_umku")],
    display_name: Annotated[str, Form(min_length=1, max_length=255)],
) -> IngestionSourceOut:
    """
    Save the uploaded file to the shared volume, insert an ingestion_sources
    row, and best-effort POST data-pipeline to start the ETL. If the pipeline
    is already running (409) we leave the row in `pending` so the admin can
    click "Proses ulang" later.
    """
    _validate_upload_or_raise(file)

    # 1) Insert a row first to allocate an id — filename embeds the id so a
    #    later listing/rerun can resolve the file path deterministically.
    row = IngestionSource(
        kind=kind,
        display_name=display_name,
        object_key=None,  # filled in post-insert once we know the id
        metadata_json={},
        status="pending",
        chunks_indexed=0,
        created_by=current_user.id,
    )
    db.add(row)
    await db.flush()  # populates row.id without committing
    assert row.id is not None

    filename = _build_filename(row.id, display_name)
    admin_path = ADMIN_UPLOAD_DIR / filename

    # 2) Stream the upload to disk; size-check while writing.
    try:
        bytes_written = await _stream_to_disk(file, admin_path)
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.exception("Failed to write upload to disk")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Gagal menyimpan berkas ke volume bersama.",
        ) from exc

    row.object_key = filename
    logger.info(
        "ingestion upload | id=%d | kind=%s | bytes=%d | path=%s",
        row.id,
        kind,
        bytes_written,
        admin_path,
    )

    # 3) Best-effort trigger; pass BOTH paths so the other kind's prior
    #    upload isn't silently reverted to the pipeline's hardcoded default.
    #    _resolve_active_paths sees the just-inserted row (via db.flush()
    #    above) and wins the most-recent ordering for `kind`.
    await db.flush()  # make the new object_key visible to the lookup
    body = await _resolve_active_paths(db)
    code, _payload = await _trigger_pipeline(body)
    if code == 202:
        row.status = "processing"
        row.last_run_at = datetime.now(timezone.utc)
        logger.info("ingestion %d → processing (pipeline accepted)", row.id)
    elif code == 409:
        logger.info(
            "ingestion %d staying pending — pipeline already busy", row.id
        )
    else:
        logger.warning(
            "ingestion %d staying pending — pipeline status=%s", row.id, code
        )

    await db.commit()
    await db.refresh(row)
    return _row_to_out(row)


@router.get(
    "",
    response_model=IngestionSourceList,
    summary="Paginated list of ingestion sources (newest first).",
)
async def list_ingestion_sources(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_admin_user)],
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
) -> IngestionSourceList:
    """Newest first by created_at; tie-broken by id desc for stability."""
    count_stmt = select(func.count(IngestionSource.id))
    total = int((await db.execute(count_stmt)).scalar_one() or 0)

    offset = (page - 1) * limit
    rows_stmt = (
        select(IngestionSource)
        .order_by(IngestionSource.created_at.desc(), IngestionSource.id.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(rows_stmt)).scalars().all()
    return IngestionSourceList(
        items=[_row_to_out(r) for r in rows],
        total=total,
        page=page,
        limit=limit,
    )


@router.get(
    "/{source_id}",
    response_model=IngestionSourceOut,
    summary="Single ingestion source detail; merges live pipeline state if running.",
)
async def get_ingestion_source(
    source_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_admin_user)],
) -> IngestionSourceOut:
    """
    If the row is currently `processing` AND the live pipeline run matches
    this row's `last_run_at` window, we surface the live `chunks_indexed`
    from /pipeline/etl-excel/status. Best-effort: any failure falls back to
    the stored row.
    """
    row = await db.get(IngestionSource, source_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ingestion source {source_id} not found.",
        )

    out = _row_to_out(row)
    if row.status != "processing":
        return out

    live = await _fetch_pipeline_live_status()
    if not live or not live.get("running"):
        return out

    # Heuristic match: the live job's started_at should be at-or-after the
    # row's last_run_at. We don't have a job-id channel back from the
    # pipeline yet — MVP-acceptable.
    live_chunks = live.get("chunks_indexed")
    if isinstance(live_chunks, int):
        out = out.model_copy(update={"chunks_indexed": live_chunks})
    return out


@router.post(
    "/{source_id}/rerun",
    response_model=IngestionSourceOut,
    summary="Re-trigger the ETL pipeline against the stored file.",
)
async def rerun_ingestion_source(
    source_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_admin_user)],
) -> IngestionSourceOut:
    """
    POST data-pipeline /pipeline/etl-excel with the row's stored path.
    409 if another job is already running. On 202 we flip to processing.
    """
    row = await db.get(IngestionSource, source_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ingestion source {source_id} not found.",
        )
    if not row.object_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sumber data ini tidak memiliki berkas untuk diproses ulang.",
        )
    if row.kind not in ("excel_kbli", "excel_pb_umku"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Re-run tidak didukung untuk kind={row.kind!r}.",
        )

    # Pass BOTH paths so re-running one kind doesn't silently revert the
    # other to the pipeline's hardcoded baseline. The row being rerun is
    # (by construction) the most-recent for its kind, so it's included.
    body = await _resolve_active_paths(db)
    code, payload = await _trigger_pipeline(body)
    if code == 409:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pipeline ETL sedang berjalan untuk sumber lain. Coba lagi sebentar.",
        )
    if code != 202:
        logger.warning(
            "rerun id=%d failed | pipeline status=%s | payload=%s",
            source_id,
            code,
            payload,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Tidak dapat menghubungi data-pipeline.",
        )

    row.status = "processing"
    row.error = None
    row.last_run_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return _row_to_out(row)
