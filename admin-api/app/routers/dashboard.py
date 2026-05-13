"""
Dashboard router — Sprint B.2.

Single endpoint that backs every tile on the admin homepage. Folded into one
response on purpose — saves the frontend 4-5 separate fetches on every
dashboard mount.

Endpoint:
    GET /dashboard/stats — counts + ai-engine chunk count + last_updated
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.deps import get_current_admin_user, get_db
from app.models import AiInteraction, IngestionSource, Kbli, User
from app.schemas.dashboard import DashboardStats

logger = logging.getLogger("bima_admin_api.dashboard")

router = APIRouter()

# Asia/Jakarta is UTC+7, no DST. Hard-coded as a fixed offset so we don't need
# to add zoneinfo / tzdata as a dep — the offset has been stable since 1988.
_JAKARTA_OFFSET = timezone(timedelta(hours=7))

# 2-second budget for the ai-engine /health call. ai-engine's chroma probe
# does a synchronous .count() on the persistent collection so it can take a
# beat on cold start; 2s is generous without being user-visible.
_AI_ENGINE_TIMEOUT_SECONDS = 2.0


async def _fetch_chroma_chunks(ai_engine_url: str) -> int | None:
    """
    Hit ai-engine /health and pluck `chroma_chunks`.

    Returns None on any failure (timeout, non-200, JSON parse, missing key) so
    the dashboard renders a "—" rather than 500-ing on a transient ai-engine
    blip. Errors get logged at WARNING — not ERROR — because this is a
    soft-fail by design.
    """
    url = ai_engine_url.rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(timeout=_AI_ENGINE_TIMEOUT_SECONDS) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning(
                "ai-engine /health returned non-200 | url=%s | status=%s",
                url,
                resp.status_code,
            )
            return None
        body = resp.json()
        chunks = body.get("chroma_chunks")
        if isinstance(chunks, int):
            return chunks
        logger.warning("ai-engine /health missing/invalid chroma_chunks | body=%s", body)
        return None
    except (httpx.TimeoutException, httpx.RequestError, ValueError) as exc:
        # ValueError catches JSONDecodeError; httpx errors cover network +
        # protocol failures. We deliberately don't catch broad Exception so
        # programmer bugs still surface.
        logger.warning("ai-engine /health unreachable | url=%s | err=%s", url, exc)
        return None


@router.get(
    "/stats",
    response_model=DashboardStats,
    summary="Aggregated counts powering the admin dashboard tiles.",
)
async def dashboard_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _current_user=Depends(get_current_admin_user),
) -> DashboardStats:
    """
    Returns the four homepage tiles + ChromaDB chunk count + a UTC timestamp.

    `whatsapp_messages_today` counts from the start of the day in
    Asia/Jakarta (UTC+7) so the number matches what an Indonesian admin sees
    on their wall clock. Stored timestamps are UTC; we convert the wall-clock
    midnight back to UTC for the comparison.
    """
    # ---- UMKM users ----------------------------------------------------
    umkm_q = select(func.count(User.id)).where(
        User.role == "msme",
        User.deleted_at.is_(None),
    )
    umkm_result = await db.execute(umkm_q)
    umkm_users = int(umkm_result.scalar_one() or 0)

    # ---- WhatsApp messages today (Asia/Jakarta day boundary) -----------
    now_jakarta = datetime.now(_JAKARTA_OFFSET)
    today_start_jakarta = now_jakarta.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_jakarta.astimezone(timezone.utc)

    wa_q = select(func.count(AiInteraction.id)).where(
        AiInteraction.channel == "whatsapp",
        AiInteraction.created_at >= today_start_utc,
    )
    wa_result = await db.execute(wa_q)
    whatsapp_messages_today = int(wa_result.scalar_one() or 0)

    # ---- Active KBLI codes (distinct) ---------------------------------
    # COUNT(DISTINCT kode_kbli) — a single Kbli row exists per skala_usaha,
    # so the table has duplicate codes by design. The dashboard tile counts
    # *unique* codes the system knows about.
    kbli_q = select(func.count(distinct(Kbli.kode_kbli)))
    kbli_result = await db.execute(kbli_q)
    active_kbli_codes = int(kbli_result.scalar_one() or 0)

    # ---- Pending ingestions -------------------------------------------
    ing_q = select(func.count(IngestionSource.id)).where(
        IngestionSource.status == "pending"
    )
    ing_result = await db.execute(ing_q)
    pending_ingestions = int(ing_result.scalar_one() or 0)

    # ---- Chroma chunks (cross-service, soft-fail) ----------------------
    chroma_chunks = await _fetch_chroma_chunks(settings.AI_ENGINE_URL)

    return DashboardStats(
        umkm_users=umkm_users,
        whatsapp_messages_today=whatsapp_messages_today,
        active_kbli_codes=active_kbli_codes,
        pending_ingestions=pending_ingestions,
        chroma_chunks=chroma_chunks,
        last_updated=datetime.now(timezone.utc),
    )
