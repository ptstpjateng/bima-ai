"""
SIAP Jateng citizen lookup — direct DB path.

Used by `routers/sso.py` to resolve a citizen identity number against the
SIAP `ptsp.person_profile` table. SIAP stores BOTH NIK (KTP, 16 digits) and
NPWP (15 digits) in the same JSONB pair: `properties->>'identity_type'` says
which type, `properties->>'identity_number'` carries the value. We pair-match
on both columns for safety (a NIK and NPWP could theoretically collide as
numeric strings; the type guard prevents that ambiguity).

There is no functional index on either path today, so this is an O(n) scan
over ~40k rows. Acceptable for hackathon scale; the post-hackathon plan is
either a functional index or moving the lookup behind a SIAP-provided REST
endpoint.

Why not use SIAP's REST API?
  SIAP does not expose a public citizen-lookup endpoint as of 2026-05-18 (the
  /monitoring-berkas endpoint we already integrate with is ticket-based, not
  identity-based). When that lands we'll add an HTTP path here that takes
  precedence and the DB path becomes the fallback.

This client is deliberately read-only and stateless — no caching, no model,
no transaction management. Just one `SELECT ... LIMIT 1` per call.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from sqlalchemy import text

from app.db import get_siap_engine, is_siap_db_configured

logger = logging.getLogger("bima_admin_api.siap_user_client")


# Allowed identity types. Matches the values present in `ptsp.person_profile`
# as of 2026-05-19 (KTP 31k+ rows, NPWP 8k+ rows; SIM/PASPOR are out of scope
# for citizen SSO because they don't map cleanly to a UMKM applicant).
IdentityType = Literal["KTP", "NPWP"]


# Query is intentionally narrow — we only need the fields the JWT will carry
# plus what the /me endpoint surfaces. Don't widen it without bumping the
# JWT claims contract; PII leakage in token payloads is a real risk.
_LOOKUP_SQL = text(
    """
    SELECT profile_id,
           properties ->> 'full_name'      AS full_name,
           properties ->> 'mobile_phone'   AS mobile,
           properties ->> 'kabupaten'      AS kab
      FROM ptsp.person_profile
     WHERE properties ->> 'identity_type'   = :itype
       AND properties ->> 'identity_number' = :id_num
     LIMIT 1
    """
)


async def lookup_pemohon_by_identity(
    identity_number: str,
    identity_type: IdentityType,
) -> Optional[dict]:
    """
    Resolve a citizen by (identity_type, identity_number) to a SIAP profile.

    Returns {profile_id, full_name, mobile, kab} on success, or None on any
    of: SIAP DB not configured, identity not found, query error.

    The caller (`routers/sso.py`) translates None into either 503 (if the
    integration is disabled) or 404 (if the identity is genuinely unknown).
    We keep that distinction at the router so this function stays pure.
    """
    if not is_siap_db_configured():
        logger.warning(
            "lookup_pemohon_by_identity called but SIAP_DATABASE_URL is empty"
        )
        return None

    try:
        engine = get_siap_engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                _LOOKUP_SQL,
                {"itype": identity_type, "id_num": identity_number},
            )
            row = result.mappings().first()
    except Exception as exc:
        # Don't crash the whole router on a SIAP outage — log and return None
        # so the caller can render a friendly 503. The exception type set is
        # broad on purpose (asyncpg, OperationalError, DNS, TLS, etc.).
        logger.exception(
            "SIAP lookup failed for type=%s number=***%s: %s",
            identity_type,
            identity_number[-4:],
            exc,
        )
        return None

    if row is None:
        return None

    return {
        "profile_id": row["profile_id"],
        "full_name": row["full_name"],
        "mobile": row["mobile"],
        "kab": row["kab"],
    }
