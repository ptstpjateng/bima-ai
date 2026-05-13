"""
KBLI router — Sprint B.2.

Two read-only endpoints:

    GET /kbli              — paginated search by q / sektor / tingkat_risiko
    GET /kbli/{kode}       — full 14-column detail + ChromaDB chunk_count

The list view returns a slim subset of columns (no long Text fields). Detail
returns everything plus a chunk count sourced from ChromaDB via ai-engine.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.deps import get_current_admin_user, get_db
from app.models import Kbli
from app.schemas.kbli import KbliDetail, KbliItem, KbliListResponse

logger = logging.getLogger("bima_admin_api.kbli")

router = APIRouter()

# Same budget / soft-fail policy as the dashboard endpoint — a flapping
# ai-engine should never break a KBLI detail page.
_AI_ENGINE_TIMEOUT_SECONDS = 2.0


async def _fetch_chunk_count(ai_engine_url: str, kode_kbli: str) -> int:
    """
    Best-effort ChromaDB chunk count for a KBLI code.

    ai-engine doesn't currently expose a per-KBLI chunk-count endpoint
    (only the global `chroma_chunks` on `/health`). Until that's added in
    Sprint C, return 0 on every call — but keep the seam wired so the
    upgrade is a one-line change inside this function.

    TODO(sprint-c): swap to `GET ai-engine/chunks?kode_kbli={kode_kbli}` and
    parse the returned count once `vectorize.py` ships that route.
    """
    # Touch the args so linters don't complain about unused params during the
    # stub period; ai_engine_url is what the future call will use.
    _ = (ai_engine_url, kode_kbli)
    return 0


@router.get(
    "",
    response_model=KbliListResponse,
    summary="Search KBLI codes with q / sektor / tingkat_risiko filters.",
)
async def list_kbli(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user=Depends(get_current_admin_user),
    q: str | None = Query(
        default=None,
        description="Case-insensitive ILIKE on kode_kbli OR judul_kbli OR sektor.",
        min_length=1,
        max_length=200,
    ),
    sektor: str | None = Query(default=None, description="Exact sektor match."),
    tingkat_risiko: str | None = Query(
        default=None, description="Exact tingkat_risiko match."
    ),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> KbliListResponse:
    """
    Paginated KBLI search.

    The Excel ingest stores one row per (kode_kbli, skala_usaha) so a single
    code can appear multiple times. The list view returns rows as-stored —
    deduplication is the frontend's call (e.g. group by kode_kbli, expand on
    click). Same convention as the legacy Filament resource.
    """
    filters = []
    if q is not None:
        # Escape ILIKE meta-characters so a `%` in user input doesn't widen
        # the search to "everything".
        safe = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pattern = f"%{safe}%"
        filters.append(
            or_(
                Kbli.kode_kbli.ilike(like_pattern),
                Kbli.judul_kbli.ilike(like_pattern),
                Kbli.sektor.ilike(like_pattern),
            )
        )
    if sektor is not None:
        filters.append(Kbli.sektor == sektor)
    if tingkat_risiko is not None:
        filters.append(Kbli.tingkat_risiko == tingkat_risiko)

    count_stmt = select(func.count(Kbli.id))
    if filters:
        count_stmt = count_stmt.where(*filters)
    total = int((await db.execute(count_stmt)).scalar_one() or 0)

    offset = (page - 1) * limit
    rows_stmt = (
        select(Kbli).order_by(Kbli.kode_kbli.asc()).offset(offset).limit(limit)
    )
    if filters:
        rows_stmt = rows_stmt.where(*filters)

    rows = (await db.execute(rows_stmt)).scalars().all()

    items = [KbliItem.model_validate(r) for r in rows]

    return KbliListResponse(items=items, total=total, page=page, limit=limit)


@router.get(
    "/{kode}",
    response_model=KbliDetail,
    summary="Full KBLI detail (all 14 columns) + ChromaDB chunk count.",
)
async def get_kbli_detail(
    kode: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _current_user=Depends(get_current_admin_user),
) -> KbliDetail:
    """
    Look up by kode_kbli. If multiple rows exist (one per skala_usaha) the
    first one ordered by id is returned — the frontend's drill-down view
    treats the code as the primary key and surfaces skala variants
    separately via the list endpoint.

    404 if the code isn't in the kblis table at all.
    """
    stmt = select(Kbli).where(Kbli.kode_kbli == kode).order_by(Kbli.id.asc()).limit(1)
    row = (await db.execute(stmt)).scalar_one_or_none()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"KBLI code {kode!r} not found.",
        )

    chunk_count = await _fetch_chunk_count(settings.AI_ENGINE_URL, kode)

    return KbliDetail(
        id=row.id,
        kode_kbli=row.kode_kbli,
        judul_kbli=row.judul_kbli,
        ruang_lingkup=row.ruang_lingkup,
        skala_usaha=row.skala_usaha,
        tingkat_risiko=row.tingkat_risiko,
        perizinan_berusaha=row.perizinan_berusaha,
        persyaratan=row.persyaratan,
        jangka_waktu_penerbitan=row.jangka_waktu_penerbitan,
        kewajiban=row.kewajiban,
        pb_umku_names=row.pb_umku_names,
        parameter=row.parameter,
        kewenangan=row.kewenangan,
        sektor=row.sektor,
        chunk_count=chunk_count,
    )
