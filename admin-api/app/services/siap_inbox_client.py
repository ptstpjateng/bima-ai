"""
SIAP Jateng officer-inbox query — direct DB path.

Backs the `GET /inbox` endpoint (Wave 4 — the multi-ticket officer inbox).
Where `siap_client.py` (HTTP) only resolves ONE ticket at a time, the inbox
needs the whole *list* of cases an officer should triage. SIAP exposes no REST
list endpoint, so we read the denormalised tracker view directly:

  * ptsp.vi_monitoring_berkas_v3 — one row per permohonan, carries
    `ticket`, `nama_izin`, `nama_pemohon`, `bloksistem_nama` (current desk),
    `tanggal_daftar`, `status_permohonan`. (Same view `ai-engine`'s
    `siap_get_status` reads — see `ai-engine/services/siap_tools.py`.)
  * ptsp.license_request — `properties->>'ticket'` → `license_id`.
  * ptsp.license          — `properties->>'time_period'` = the licence's
    statutory SLA / jangka waktu (the SOP days the inbox surfaces).

The two latter tables are LEFT-joined: a case whose SOP days cannot be
resolved still appears in the inbox (SOP shows as unknown) rather than being
silently dropped.

READ-ONLY by contract:
  * One parameterised SELECT — no string interpolation into SQL, ever.
  * Runs on the SIAP read-only engine in `app/db.py` (`get_siap_engine`),
    the same independently-pooled engine `siap_user_client.py` uses. A SIAP
    outage cannot starve the bima_ai pool.

Officer → cases scoping (documented decision — Wave 4):
  SIAP's `bloksistem_nama` records which desk currently holds a file, but the
  admin-api `User` model carries no desk / SIAP role-id, so there is no clean
  officer→desk join for Beta. The inbox therefore returns ALL *active*
  (in-progress) cases — anything not in a terminal state — and accepts an
  optional `desk` substring filter so an officer can narrow to their desk by
  hand. Tightening this to an automatic per-officer desk is a post-hackathon
  ask on the SIAP team (a `users.siap_role_id` column or a SIAP-provided
  "my queue" endpoint).
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text

from app.db import get_siap_engine, is_siap_db_configured

logger = logging.getLogger("bima_admin_api.siap_inbox_client")

# Terminal statuses — a case in one of these is finished and does NOT belong
# in an officer's working inbox. SIAP's `status_permohonan` is free-ish text,
# so we match on a normalised lowercase substring (mirrors the bucketing
# `case-status-badge.tsx` and `transparency_poller.py` already use).
_TERMINAL_STATUS_TOKENS: tuple[str, ...] = (
    "selesai",
    "terbit",
    "ditolak",
    "tolak",
    "dibatalkan",
    "batal",
)

# Hard ceiling on how many rows the inbox query returns. The officer inbox is
# a working surface, not a data export — a few hundred active cases is already
# a large desk. Caps memory + payload size; the router exposes `limit` capped
# at this value.
MAX_INBOX_ROWS = 200

# The inbox list query.
#
#   days_open  — whole days since `tanggal_daftar` (calendar days; SIAP's own
#                SLA is in *working* days but we have no SIAP holiday calendar
#                here, so calendar days is the honest, conservative choice —
#                it never under-states elapsed time).
#   sop_days   — digits parsed out of `ptsp.license.properties->>'time_period'`
#                (NULLIF + regexp_replace strips "14 hari kerja" -> 14). NULL
#                when the licence row is missing or the field is blank.
#
# Filtering: drop terminal-status rows in Python (SIAP `status_permohonan` is
# free-text, easier to bucket there). Ordering is done in Python too (the
# ranker is non-trivial), but we ORDER BY tanggal_daftar so the LIMIT keeps the
# *oldest* — and thus most likely most-urgent — cases when there are more rows
# than MAX_INBOX_ROWS.
#
# Bound params are SQLAlchemy `:name` markers — `::text` / `::int` are Postgres
# casts and SQLAlchemy's text() parser does not mistake them for binds.
_SQL_INBOX = text(
    """
    SELECT v.ticket                                   AS ticket,
           v.nama_izin                                AS license_name,
           v.nama_bidang                              AS sector_name,
           v.nama_pemohon                             AS applicant_name,
           v.bloksistem_nama                          AS current_desk,
           v.status_permohonan                        AS status,
           v.tanggal_daftar                           AS submitted_at,
           GREATEST(
               0,
               (CURRENT_DATE - v.tanggal_daftar::date)
           )                                          AS days_open,
           NULLIF(
               regexp_replace(
                   COALESCE(lic.properties ->> 'time_period', ''),
                   '\\D', '', 'g'
               ),
               ''
           )::int                                     AS sop_days
      FROM ptsp.vi_monitoring_berkas_v3 v
      LEFT JOIN ptsp.license_request lr
             ON lr.properties ->> 'ticket' = v.ticket
      LEFT JOIN ptsp.license lic
             ON lic.license_id = lr.license_id
     WHERE v.tanggal_daftar IS NOT NULL
       AND (CAST(:desk AS text) IS NULL OR v.bloksistem_nama ILIKE '%' || :desk || '%')
     ORDER BY v.tanggal_daftar ASC
     LIMIT :row_limit
    """
)


def _is_terminal(status: Optional[str]) -> bool:
    """True when a SIAP status string means the case is finished."""
    if not status:
        return False
    norm = status.strip().lower()
    return any(token in norm for token in _TERMINAL_STATUS_TOKENS)


async def fetch_officer_inbox(
    desk: Optional[str] = None,
    limit: int = MAX_INBOX_ROWS,
) -> Optional[list[dict]]:
    """
    Return the list of active SIAP cases for the officer inbox.

    Each dict has a stable shape:
      {
        "ticket": str,            # zero-padded 9-digit ticket
        "license_name": str|None,
        "sector_name": str|None,
        "applicant_name": str|None,
        "current_desk": str|None, # `bloksistem_nama`
        "status": str|None,       # raw SIAP `status_permohonan`
        "submitted_at": str|None, # ISO-8601
        "days_open": int,         # whole calendar days since submission
        "sop_days": int|None,     # statutory SLA days, None when unknown
      }

    Terminal-status cases are filtered out — the inbox is a working queue, not
    a history view. `desk` is an optional case-insensitive substring filter on
    `bloksistem_nama`.

    Returns None (not []) on a hard failure — SIAP DB not configured or a
    query error — so the caller can distinguish "integration disabled" (503)
    from a genuinely empty inbox ([]).
    """
    if not is_siap_db_configured():
        logger.warning("fetch_officer_inbox called but SIAP_DATABASE_URL is empty")
        return None

    capped = max(1, min(int(limit) if limit else MAX_INBOX_ROWS, MAX_INBOX_ROWS))
    desk_filter = desk.strip() if desk and desk.strip() else None

    try:
        engine = get_siap_engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                _SQL_INBOX,
                {"desk": desk_filter, "row_limit": capped},
            )
            rows = result.mappings().all()
    except Exception:
        # Never crash the router on a SIAP outage — log and return None so the
        # caller renders a clean 503. Broad on purpose (asyncpg, OperationalError,
        # DNS, TLS, view-missing, …).
        logger.exception("fetch_officer_inbox query failed | desk=%s", desk_filter)
        return None

    cases: list[dict] = []
    for row in rows:
        status = row["status"]
        if _is_terminal(status):
            continue
        submitted = row["submitted_at"]
        sop_days = row["sop_days"]
        cases.append(
            {
                "ticket": str(row["ticket"]).strip().zfill(9),
                "license_name": (row["license_name"] or None),
                "sector_name": (row["sector_name"] or None),
                "applicant_name": (
                    row["applicant_name"].title()
                    if row["applicant_name"]
                    else None
                ),
                "current_desk": (row["current_desk"] or None),
                "status": status or None,
                "submitted_at": (
                    submitted.isoformat()
                    if submitted is not None and hasattr(submitted, "isoformat")
                    else (str(submitted) if submitted is not None else None)
                ),
                "days_open": int(row["days_open"] or 0),
                "sop_days": int(sop_days) if sop_days is not None else None,
            }
        )

    logger.info(
        "fetch_officer_inbox | desk=%s | active_cases=%d (of %d fetched)",
        desk_filter,
        len(cases),
        len(rows),
    )
    return cases
