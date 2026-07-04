"""
SIAP Jateng read-only Postgres connection (ai-engine).

The BIMA SIAP tool layer ([[Decisions]] §22, [[SIAP API Inventory]] Half 2)
reads SIAP's `dbsiapjateng` database directly: SIAP exposes no REST endpoint
for its licence catalogue, requirements, legal basis, fee, SLA, or per-ticket
step history — that "deep" knowledge is DB-only. This module owns the single
asyncpg connection pool the Tier-1 tools share.

Why asyncpg directly (not SQLAlchemy)?
  ai-engine has no SQLAlchemy dependency — it is a thin FastAPI service.
  admin-api's `siap_user_client.py` uses SQLAlchemy because admin-api already
  carries it for its own ORM. Here a bare asyncpg pool is lighter and the
  query surface is five read-only SELECTs. The connection *pattern* (a second,
  independently-pooled, lazily-built, read-only engine pointed at SIAP) is
  copied verbatim from admin-api `app/db.py`.

READ-ONLY by contract:
  * Every query in `siap_tools.py` is a parameterised SELECT — no string
    interpolation into SQL, ever.
  * `default_transaction_read_only` is forced on every connection via the
    pool `setup` hook, so even a buggy query physically cannot write.
  * Ideally the DSN points at a SELECT-only Postgres role. Today only the
    `bima` superuser exists on the VPS — see B1 in [[SIAP API Inventory]]
    §8: a `bima_readonly` role is a SIAP-team ask. The read-only transaction
    guard below is the interim safety net.

Configuration (discrete env vars — NOT a DSN string):
  SIAP_DB_HOST     — e.g. `postgres` (Docker service name on the VPS)
  SIAP_DB_PORT     — default 5432
  SIAP_DB_NAME     — default `dbsiapjateng`
  SIAP_DB_USER     — e.g. `bima`
  SIAP_DB_PASSWORD — the DB password, raw (NOT url-encoded)

  Why discrete vars, not a `postgresql://user:pw@host/db` DSN: a DSN
  string breaks when the password contains URL-special characters
  (`@ / : ? #`) — asyncpg misparses the userinfo/host boundary and the
  host resolves to garbage ("Name or service not known"). The BIMA DB
  password contains such characters. Passing host/user/password as
  separate asyncpg kwargs sidesteps URL parsing entirely.

  Leave SIAP_DB_HOST (or SIAP_DB_USER) blank to disable the SIAP tool
  layer entirely (tools then return a structured "not configured"
  result and the agent degrades gracefully).

Failure model:
  Lazy init — the pool is built on first use and only if the SIAP DB is
  configured. Callers guard with `is_siap_db_configured()`. A SIAP
  outage never raises out of a tool: tools catch and return a
  structured error dict.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import asyncpg
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("bima_ai.siap_db")

# ---------------------------------------------------------------------------
# Configuration — discrete connection components (see module docstring for
# why we do NOT use a DSN string here).
# ---------------------------------------------------------------------------

# On the VPS, ai-engine reaches Postgres over the Docker network at host
# `postgres`, port 5432, database `dbsiapjateng`.
_SIAP_DB_HOST: str = os.getenv("SIAP_DB_HOST", "").strip()
_SIAP_DB_PORT: int = int(os.getenv("SIAP_DB_PORT", "5432") or "5432")
_SIAP_DB_NAME: str = os.getenv("SIAP_DB_NAME", "dbsiapjateng").strip()
_SIAP_DB_USER: str = os.getenv("SIAP_DB_USER", "").strip()
# Password is read raw — never url-encoded, never logged.
_SIAP_DB_PASSWORD: str = os.getenv("SIAP_DB_PASSWORD", "")

# Conservative pool — the SIAP tool layer is low-volume (one citizen question
# at a time) and we must not exhaust SIAP's connection slots. Mirrors the
# tight pool admin-api uses for its SIAP engine (size 2, small overflow).
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 3
_CONNECT_TIMEOUT_SECONDS = 8.0
# Per-statement ceiling so a pathological query can't hang a citizen reply.
_COMMAND_TIMEOUT_SECONDS = 12.0

_pool: Optional[asyncpg.Pool] = None


def is_siap_db_configured() -> bool:
    """Cheap precheck without touching the pool. Callers should branch on
    this and return a friendly "integration disabled" result rather than
    letting a None pool crash."""
    return bool(_SIAP_DB_HOST and _SIAP_DB_USER)


async def _on_connect(conn: asyncpg.Connection) -> None:
    """Pool `setup` hook — runs once per physical connection.

    Forces every connection BIMA opens against SIAP into read-only mode.
    Even if a future query is accidentally written as a mutation, Postgres
    rejects it ('cannot execute ... in a read-only transaction'). This is
    the interim guard until SIAP issues the `bima_readonly` role (B1)."""
    await conn.execute("SET default_transaction_read_only = on;")


async def get_siap_pool() -> asyncpg.Pool:
    """
    Return the lazily-constructed SIAP read-only connection pool.

    Raises RuntimeError if the SIAP DB isn't configured — callers must
    guard with `is_siap_db_configured()` first. The pool is process-wide;
    asyncpg pools are safe to share across coroutines.
    """
    global _pool
    if not (_SIAP_DB_HOST and _SIAP_DB_USER):
        raise RuntimeError(
            "SIAP DB is not configured (SIAP_DB_HOST / SIAP_DB_USER unset); "
            "cannot open the SIAP DB pool."
        )
    if _pool is None:
        logger.info(
            "Opening SIAP read-only pool | host=%s db=%s min=%d max=%d",
            _SIAP_DB_HOST, _SIAP_DB_NAME, _POOL_MIN_SIZE, _POOL_MAX_SIZE,
        )
        # Discrete kwargs — no DSN parsing, so a password with URL-special
        # characters is passed through verbatim.
        _pool = await asyncpg.create_pool(
            host=_SIAP_DB_HOST,
            port=_SIAP_DB_PORT,
            user=_SIAP_DB_USER,
            password=_SIAP_DB_PASSWORD,
            database=_SIAP_DB_NAME,
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            timeout=_CONNECT_TIMEOUT_SECONDS,
            command_timeout=_COMMAND_TIMEOUT_SECONDS,
            setup=_on_connect,
        )
    return _pool


async def close_siap_pool() -> None:
    """Gracefully close the pool — wire into the FastAPI shutdown event if
    ai-engine starts managing lifespans for its connections. Safe to call
    when the pool was never opened."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("SIAP read-only pool closed.")


# ===========================================================================
# Flow-based officer resolution ([[BIMA Officer Notify Model]])
#
# BIMA decides WHO to notify from SIAP's "Alur Izin" (license_approval_step),
# not a single static env phone. For a license_request at its CURRENT
# approval_step_id we read that step's owning role (group_id → public.roles)
# and its `properties.wa_notification` flag, then resolve the officer(s)
# assigned to that step on this specific izin.
#
# These are the SAME pool + SELECT-only pattern suitability_judge uses
# (get_siap_pool → pool.acquire → conn.fetch/fetchrow). Read-only by contract;
# the pool `setup` hook forces every connection read-only.
#
# Failure model: NEVER raise. resolve_step_officers / get_officer_directory
# return a safe empty result on any error so a SIAP hiccup degrades the
# notify path to the BIMA_OFFICER_WA_PHONE fallback rather than crashing the
# citizen's submission. PII (whatsapp / names) is never logged.
# ===========================================================================

# The applicant ("Pemohon Online") role anchors sort_order 0 / the terminal
# step. It must never be treated as an officer (it's the CITIZEN). Matched
# case-insensitively against the role NAME (the id is environment-specific;
# the spec cites 99 for Beta-SIAP but the name is the stable contract).
APPLICANT_ROLE_NAME = "pemohon online"

# request_id → (approval_step_id, license_id)
_SQL_REQUEST_STEP = """
    SELECT approval_step_id, license_id
      FROM ptsp.license_request
     WHERE request_id = $1
     LIMIT 1
"""

# approval_step_id → (group_id, sort_order, wa_notification flag)
_SQL_APPROVAL_STEP = """
    SELECT group_id,
           sort_order,
           properties ->> 'wa_notification' AS wa_notification
      FROM ptsp.license_approval_step
     WHERE approval_step_id = $1
     LIMIT 1
"""

# role id → role name (to detect the applicant role by name)
_SQL_ROLE_NAME = """
    SELECT name
      FROM public.roles
     WHERE id = $1
     LIMIT 1
"""

# Officer(s) who BOTH hold the step's role (Spatie model_has_roles) AND are
# assigned to this specific izin (Privilege Izin → scmcoresystem.tblroleizin).
# Mirrors how SIAP itself scopes the officer worklist (DataPerizinanController).
_SQL_STEP_OFFICER_WHATSAPPS = """
    SELECT DISTINCT u.whatsapp
      FROM scmcoresystem.tblroleizin t
      JOIN public.users u ON u.id = t.users_id
      JOIN public.model_has_roles m ON m.model_id = u.id
     WHERE t.tblizin_id = $1
       AND m.role_id = $2
       AND u.whatsapp IS NOT NULL
       AND btrim(u.whatsapp) <> ''
"""

# Every officer WhatsApp across all real officer roles — i.e. roles that own
# at least one license_approval_step, excluding the applicant role. Powers the
# inbound officer-identification cache (any registered petugas may chat as
# themselves). PII never logged; only the COUNT is.
_SQL_OFFICER_DIRECTORY = """
    SELECT DISTINCT u.whatsapp
      FROM public.users u
      JOIN public.model_has_roles m ON m.model_id = u.id
      JOIN public.roles r ON r.id = m.role_id
     WHERE r.id IN (SELECT DISTINCT group_id FROM ptsp.license_approval_step)
       AND lower(r.name) <> $1
       AND u.whatsapp IS NOT NULL
       AND btrim(u.whatsapp) <> ''
"""


def _empty_step_result() -> dict:
    """Safe default — no officer, not the applicant step, wa not active."""
    return {
        "wa_active": False,
        "is_applicant_step": False,
        "officer_whatsapps": [],
        "group_id": None,
        "sort_order": None,
    }


async def resolve_step_officers(request_id: int) -> dict:
    """
    Resolve the officer(s) BIMA should notify for a license_request at its
    CURRENT approval step.

    Returns a dict:
      {
        "wa_active":         bool,        # step's wa_notification == 'ACTIVE'
        "is_applicant_step": bool,        # owning role is 'Pemohon Online'
        "officer_whatsapps": list[str],   # NORMALIZED (62…) officer numbers
        "group_id":          int | None,  # the step's role id
        "sort_order":        int | None,  # 0 = applicant anchor
      }

    NEVER raises. On any DB error / missing row / unconfigured SIAP DB it
    returns a safe empty result (callers fall back to BIMA_OFFICER_WA_PHONE).
    Officer numbers are normalized to APTANA's inbound form so they match the
    inbound channel_id directly. Numbers/names are never logged.
    """
    result = _empty_step_result()

    if not is_siap_db_configured():
        logger.info(
            "resolve_step_officers: SIAP DB not configured | request_id=%s",
            request_id,
        )
        return result

    try:
        rid = int(request_id)
    except (TypeError, ValueError):
        logger.warning("resolve_step_officers: bad request_id=%r", request_id)
        return result

    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            req_row = await conn.fetchrow(_SQL_REQUEST_STEP, rid)
            if req_row is None:
                logger.info(
                    "resolve_step_officers: request not found | request_id=%s", rid
                )
                return result

            approval_step_id = req_row["approval_step_id"]
            license_id = req_row["license_id"]
            if approval_step_id is None:
                logger.info(
                    "resolve_step_officers: no approval_step_id | request_id=%s", rid
                )
                return result

            step_row = await conn.fetchrow(_SQL_APPROVAL_STEP, approval_step_id)
            if step_row is None:
                logger.info(
                    "resolve_step_officers: step not found | request_id=%s step=%s",
                    rid, approval_step_id,
                )
                return result

            group_id = step_row["group_id"]
            sort_order = step_row["sort_order"]
            wa_flag = (step_row["wa_notification"] or "").strip().upper()
            wa_active = wa_flag == "ACTIVE"
            result["group_id"] = group_id
            result["sort_order"] = sort_order
            result["wa_active"] = wa_active

            # Resolve the owning role NAME to decide applicant-vs-officer.
            is_applicant_step = False
            if group_id is not None:
                role_row = await conn.fetchrow(_SQL_ROLE_NAME, group_id)
                role_name = (role_row["name"] if role_row else "") or ""
                is_applicant_step = (
                    role_name.strip().lower() == APPLICANT_ROLE_NAME
                )
            result["is_applicant_step"] = is_applicant_step

            # Only resolve officer numbers for a wa-active OFFICER step. An
            # applicant step (citizen notify) and a NON-ACTIVE step both yield
            # no officer numbers here.
            if wa_active and not is_applicant_step and group_id is not None:
                rows = await conn.fetch(
                    _SQL_STEP_OFFICER_WHATSAPPS, license_id, group_id
                )
                seen: set[str] = set()
                numbers: list[str] = []
                for r in rows:
                    norm = _normalize_msisdn(r["whatsapp"])
                    if norm and norm not in seen:
                        seen.add(norm)
                        numbers.append(norm)
                result["officer_whatsapps"] = numbers

        logger.info(
            "resolve_step_officers | request_id=%s group_id=%s sort_order=%s "
            "wa_active=%s applicant=%s officers=%d",
            rid, result["group_id"], result["sort_order"],
            result["wa_active"], result["is_applicant_step"],
            len(result["officer_whatsapps"]),
        )
        return result
    except Exception:  # pragma: no cover — defensive; never break the citizen path
        logger.exception(
            "resolve_step_officers failed (returning empty) | request_id=%s", rid
        )
        return _empty_step_result()


async def get_officer_directory() -> list[str]:
    """
    Return the set of ALL officer WhatsApp numbers (normalized, deduped) —
    every `public.users.whatsapp` whose role owns at least one
    `license_approval_step`, excluding the applicant role.

    Powers the inbound officer-identification cache in officer_bridge. NEVER
    raises — returns [] on any error / unconfigured SIAP DB. Numbers are never
    logged (only the count).
    """
    if not is_siap_db_configured():
        logger.info("get_officer_directory: SIAP DB not configured")
        return []
    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SQL_OFFICER_DIRECTORY, APPLICANT_ROLE_NAME)
        seen: set[str] = set()
        numbers: list[str] = []
        for r in rows:
            norm = _normalize_msisdn(r["whatsapp"])
            if norm and norm not in seen:
                seen.add(norm)
                numbers.append(norm)
        logger.info("get_officer_directory | officers=%d", len(numbers))
        return numbers
    except Exception:  # pragma: no cover — defensive
        logger.exception("get_officer_directory failed (returning empty)")
        return []


def _normalize_msisdn(raw: Optional[str]) -> str:
    """Canonicalize a stored WhatsApp number to APTANA's inbound form (62…).

    Strip everything but digits; a leading 0 → 62; otherwise ensure a 62
    prefix. Mirrors services.whatsapp_sender.normalize_phone but is kept local
    so this read-only module carries no outbound-sender import. Returns "" for
    empty/garbage input.
    """
    if not raw:
        return ""
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if not digits:
        return ""
    if digits.startswith("0"):
        return "62" + digits[1:]
    if digits.startswith("62"):
        return digits
    return "62" + digits


# ===========================================================================
# SIAP-grounded case loading ([[BIMA Multi-step Officer Copilot]])
#
# The multi-step officer copilot must let ANY next-step officer read ALL the
# submission documents and ALL the prior-step notes for a request STRAIGHT FROM
# SIAP — not from the citizen's in-memory session (which only the FIRST officer
# ever saw). These reads assemble that case context.
#
# Submission-doc join (confirmed against [[SIAP API Inventory]] §Half-2):
#   ptsp.license_request (request_id PK) --profile_id--> ptsp.person_profile
#   ptsp.profile_requirements (keyed by profile_id) holds the applicant's
#     per-requirement uploads: `profile_requirements_files` is the storage key
#     (served at {SIAP_STORAGE_BASE}/storage/<file>), `requirements_id` links to
#     ptsp.requirements.name (the human requirement label, e.g. "Fotokopi KTP").
# So a request's uploaded docs are: request → profile_id → profile_requirements
#   JOIN requirements. The bytes are fetched on demand from Beta storage by
#   services.siap_templates.fetch_submission_file_bytes.
#
# Step / last-step detection:
#   The request's CURRENT step is license_request.approval_step_id. Its owning
#   ROLE name decides whether this is the LAST (SK-signing / Kepala Dinas) desk:
#   the signing desk is a specific role (BIMA_SK_SIGNING_ROLES, default
#   `Kepala Dinas SKPD` + `Kepala DPMPTSP`), NOT the highest sort_order — SIAP
#   chains bookend with SYSTEM/APPLICANT roles (super_admin, Pemohon Online) at
#   the top sort_order, so a positional test mis-fires. sort_order +
#   MAX(sort_order) are still captured for context (and as a fallback when no
#   signing role is configured); `stereotype` is a secondary hint.
#
# Failure model: NEVER raise. Every function returns a safe empty/None result
# on any error / unconfigured SIAP DB so a SIAP hiccup degrades the officer
# surface to an honest "tidak tersedia di SIAP" rather than crashing a reply.
# PII (file names / applicant fields) is never logged.
# ===========================================================================

# request_id → (profile_id, license_id, approval_step_id, ticket)
_SQL_REQUEST_CASE = """
    SELECT profile_id,
           license_id,
           approval_step_id,
           properties ->> 'ticket' AS ticket
      FROM ptsp.license_request
     WHERE request_id = $1
     LIMIT 1
"""

# All requirement uploads for an applicant profile. `profile_requirements_files`
# is the storage key (may be a bare file name or a relative path); we return it
# raw and let the fetch layer sanitise. `requirements.name` is the human label
# the officer recognises ("Fotokopi KTP", "NIB"). Rows with no file are dropped
# by the caller (a requirement can exist unfulfilled).
_SQL_PROFILE_REQUIREMENT_FILES = """
    SELECT pr.requirements_id,
           pr.profile_requirements_files AS file_ref,
           pr.status AS req_status,
           r.name AS requirement_name
      FROM ptsp.profile_requirements pr
      LEFT JOIN ptsp.requirements r
             ON r.requirements_id = pr.requirements_id
     WHERE pr.profile_id = $1
       AND pr.profile_requirements_files IS NOT NULL
       AND btrim(pr.profile_requirements_files) <> ''
     ORDER BY pr.requirements_id
"""

# The request's current step (sort_order/group_id/stereotype) PLUS the maximum
# sort_order across the whole license's workflow, so the caller can decide if
# the current step is the LAST one. Two params: approval_step_id, license_id.
_SQL_STEP_WITH_MAX = """
    SELECT s.sort_order        AS current_sort_order,
           s.group_id          AS group_id,
           s.stereotype        AS stereotype,
           mx.max_sort_order   AS max_sort_order
      FROM ptsp.license_approval_step s
      CROSS JOIN LATERAL (
            SELECT MAX(sort_order) AS max_sort_order
              FROM ptsp.license_approval_step
             WHERE license_id = $2
      ) mx
     WHERE s.approval_step_id = $1
     LIMIT 1
"""

# The role name(s) that own the FINAL SK-signing desk (the Kepala Dinas who
# signs the Surat Keputusan ber-TTE). SIAP approval chains are bookended by
# SYSTEM/APPLICANT roles (super_admin, Pemohon Online) at the highest
# sort_order, so "current sort_order == max sort_order" mis-identifies the
# signer — verified on Beta: request 8's chain ends `super_admin`, and across
# licenses the terminal role is usually super_admin/Pemohon Online. The signing
# desk is a SPECIFIC ROLE, not the last position. Comma-separated, matched
# case-insensitively; confirmed with the SIAP owner as `Kepala Dinas SKPD` +
# `Kepala DPMPTSP`. Set BIMA_SK_SIGNING_ROLES empty to fall back to the old
# sort_order heuristic (the signal then never silently vanishes).
_SK_SIGNING_ROLES: frozenset = frozenset(
    r.strip().lower()
    for r in os.getenv(
        "BIMA_SK_SIGNING_ROLES", "Kepala Dinas SKPD,Kepala DPMPTSP"
    ).split(",")
    if r.strip()
)


def _is_signing_desk(role_name, current_sort_order, max_sort_order) -> bool:
    """True when THIS desk is the FINAL SK-signing (Kepala Dinas) step.

    Primary signal: the desk's ROLE name is one of `_SK_SIGNING_ROLES`
    (BIMA_SK_SIGNING_ROLES). Fallback (only when no signing roles are
    configured): the old `current >= max sort_order` heuristic, so the last-step
    signal is never lost even if the env is blanked.
    """
    if _SK_SIGNING_ROLES:
        name = (role_name or "").strip().lower()
        return bool(name) and name in _SK_SIGNING_ROLES
    return (
        current_sort_order is not None
        and max_sort_order is not None
        and current_sort_order >= max_sort_order
    )


def _empty_case_meta() -> dict:
    """Safe default when the request/step cannot be read from SIAP."""
    return {
        "found": False,
        "request_id": None,
        "profile_id": None,
        "license_id": None,
        "approval_step_id": None,
        "ticket": None,
        "current_sort_order": None,
        "max_sort_order": None,
        "is_final_step": False,
        "group_id": None,
        "stereotype": None,
        "owner_desk": None,
    }


async def get_request_case_meta(request_id: int) -> dict:
    """Resolve a request's identity + step position from SIAP.

    Returns a dict (see `_empty_case_meta`) with `found`, the profile/license/
    step ids, the SIAP `ticket`, the current step's `sort_order`/`stereotype`/
    owning `group_id`/`owner_desk` role, the license's `max_sort_order`, and a
    derived `is_final_step` (the desk's role is a configured SK-signing role —
    see `_is_signing_desk` / BIMA_SK_SIGNING_ROLES). NEVER raises — a
    miss / DB error returns the safe empty meta so the caller degrades to an
    honest "not in SIAP" reply. PII is never logged.
    """
    result = _empty_case_meta()
    if not is_siap_db_configured():
        logger.info("get_request_case_meta: SIAP DB not configured | request_id=%s", request_id)
        return result
    try:
        rid = int(request_id)
    except (TypeError, ValueError):
        logger.warning("get_request_case_meta: bad request_id=%r", request_id)
        return result

    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            req = await conn.fetchrow(_SQL_REQUEST_CASE, rid)
            if req is None:
                logger.info("get_request_case_meta: request not found | request_id=%s", rid)
                return result
            result["request_id"] = rid
            result["profile_id"] = req["profile_id"]
            result["license_id"] = req["license_id"]
            result["approval_step_id"] = req["approval_step_id"]
            result["ticket"] = req["ticket"]

            step_id = req["approval_step_id"]
            license_id = req["license_id"]
            if step_id is not None and license_id is not None:
                step = await conn.fetchrow(_SQL_STEP_WITH_MAX, step_id, license_id)
                if step is not None:
                    cur = step["current_sort_order"]
                    mx = step["max_sort_order"]
                    result["current_sort_order"] = cur
                    result["max_sort_order"] = mx
                    result["group_id"] = step["group_id"]
                    result["stereotype"] = step["stereotype"]
                    # Resolve the current desk's ROLE name first — it is the
                    # authoritative last-step signal (see _is_signing_desk).
                    if step["group_id"] is not None:
                        role = await conn.fetchrow(_SQL_ROLE_NAME, step["group_id"])
                        result["owner_desk"] = (role["name"] if role else None)
                    # Last-step (SK-signing) signal keys on the desk's ROLE
                    # (Kepala Dinas), NOT max sort_order — SIAP chains bookend
                    # with system/applicant roles at the top sort_order, so the
                    # positional heuristic mis-fires. See _SK_SIGNING_ROLES.
                    result["is_final_step"] = _is_signing_desk(
                        result["owner_desk"], cur, mx
                    )
            result["found"] = True

        logger.info(
            "get_request_case_meta | request_id=%s license_id=%s step=%s "
            "sort=%s/%s final=%s stereotype=%s",
            rid, result["license_id"], result["approval_step_id"],
            result["current_sort_order"], result["max_sort_order"],
            result["is_final_step"], result["stereotype"],
        )
        return result
    except Exception:  # pragma: no cover — defensive; never break the officer path
        logger.exception("get_request_case_meta failed (returning empty) | request_id=%s", rid)
        return _empty_case_meta()


async def get_submission_doc_refs(profile_id: int) -> list[dict]:
    """Return the applicant's uploaded requirement files from SIAP.

    Each entry: {"requirements_id", "requirement_name", "file_ref", "status"}
    where `file_ref` is the storage key (fetched by
    services.siap_templates.fetch_submission_file_bytes). NEVER raises — returns
    [] on any error / unconfigured DB. File names are NOT logged (they may echo
    the applicant's name); only the count is.

    SCOPING LIMITATION (known): ptsp.profile_requirements is keyed by
    profile_id + requirements_id ONLY — it has NO request_id/approval_step_id
    column — so these files are pooled per APPLICANT, not per request. For a
    citizen who filed more than one license request, this returns every
    requirement file on the profile, which may include a different request's
    attachments (this mirrors SIAP's own per-profile requirement model). It is
    NOT an issue for BIMA-created requests: those carry their docs behind the
    per-request document API (siap_document_client), and profile_requirements is
    empty for them — the officer loader's source (b) is request-scoped and
    authoritative there. Tightening source (a) to a request would require
    joining the license's applicable requirements (license_requirements by
    license_id); tracked as a follow-up, not done here.
    """
    if not is_siap_db_configured():
        logger.info("get_submission_doc_refs: SIAP DB not configured | profile_id=%s", profile_id)
        return []
    try:
        pid = int(profile_id)
    except (TypeError, ValueError):
        logger.warning("get_submission_doc_refs: bad profile_id=%r", profile_id)
        return []
    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SQL_PROFILE_REQUIREMENT_FILES, pid)
        docs: list[dict] = []
        for r in rows:
            file_ref = (r["file_ref"] or "").strip()
            if not file_ref:
                continue
            docs.append({
                "requirements_id": r["requirements_id"],
                "requirement_name": (r["requirement_name"] or "").strip() or None,
                "file_ref": file_ref,
                "status": r["req_status"],
            })
        logger.info("get_submission_doc_refs | profile_id=%s docs=%d", pid, len(docs))
        return docs
    except Exception:  # pragma: no cover — defensive
        logger.exception("get_submission_doc_refs failed (returning empty) | profile_id=%s", pid)
        return []


# ===========================================================================
# Applicant profile read ([[Generalize copilot SK/output-doc from real data]])
#
# The generic output-template fill engine (services/siap_templates.py) resolves
# an APPLICANT-identity placeholder key (nama_pemohon, alamat, nik, no_hp, …)
# from the request's REAL ptsp.person_profile.properties JSONB — the same
# structured source SIAP itself uses on the Rekomtek preview surface, and the
# same key set SIAP's person-profile upsert accepts (see
# siap_profile_client._OPTIONAL_FIELDS). This read replaces the earlier PKPP
# path where identity was carried in the copilot session; grounding directly on
# person_profile makes ANY licence's applicant fields fill deterministically
# with no hallucination — every value is a literal column read.
#
# Read-only by contract (parameterised SELECT + the read-only-transaction pool
# guard). NEVER raises — a miss / DB error / unconfigured DB returns {} so the
# fill engine simply leaves those placeholders blank. PII (the property VALUES)
# is NEVER logged — only the profile_id and the count of keys.
# ===========================================================================

# profile_id → the raw person_profile.properties JSONB (as text, parsed by the
# caller). The whole JSONB is returned so the fill engine can read ANY key the
# template needs (full_name, address, identity_number, phone, email, …) without
# this module knowing the template's key set in advance.
_SQL_PERSON_PROFILE = """
    SELECT properties
      FROM ptsp.person_profile
     WHERE profile_id = $1
     LIMIT 1
"""


async def get_person_profile_properties(profile_id: int) -> dict:
    """Return the applicant's `ptsp.person_profile.properties` as a dict.

    This is the authoritative APPLICANT-identity source for the generic
    output-template fill engine (full_name, address, identity_number, phone,
    email, gender, birth_place/birth_date, nationality, regency/sub_district/
    village, identity_type, nip, position_name — the SIAP profile key set).

    NEVER raises — returns {} on any error / miss / unconfigured DB, so a
    template placeholder that would have drawn from the profile simply renders
    blank. The property VALUES are PII and are NEVER logged (only the count of
    keys); the profile_id is not itself PII.
    """
    if not is_siap_db_configured():
        logger.info(
            "get_person_profile_properties: SIAP DB not configured | profile_id=%s",
            profile_id,
        )
        return {}
    try:
        pid = int(profile_id)
    except (TypeError, ValueError):
        logger.warning("get_person_profile_properties: bad profile_id=%r", profile_id)
        return {}
    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SQL_PERSON_PROFILE, pid)
        if row is None or row["properties"] is None:
            logger.info(
                "get_person_profile_properties: profile not found | profile_id=%s", pid
            )
            return {}
        raw = row["properties"]
        # asyncpg returns a JSONB column as a str (no codec registered) or,
        # with a codec, as a dict. Handle both; a str is parsed with json.
        if isinstance(raw, str):
            import json
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                logger.warning(
                    "get_person_profile_properties: properties not valid JSON | "
                    "profile_id=%s", pid,
                )
                return {}
        elif isinstance(raw, dict):
            parsed = raw
        else:
            return {}
        if not isinstance(parsed, dict):
            return {}
        # Never log the values — only how many keys were read.
        logger.info(
            "get_person_profile_properties | profile_id=%s keys=%d", pid, len(parsed)
        )
        return parsed
    except Exception:  # pragma: no cover — defensive; never break the officer path
        logger.exception(
            "get_person_profile_properties failed (returning empty) | profile_id=%s",
            profile_id,
        )
        return {}


# license_id → the licence NAME (for placeholder keys like nama_izin, and to
# parse jenis_pengadaan). Read-only; the licence name is non-PII.
_SQL_LICENSE_NAME = """
    SELECT name
      FROM ptsp.license
     WHERE license_id = $1
     LIMIT 1
"""


async def get_license_name(license_id: int) -> Optional[str]:
    """Return `ptsp.license.name` for a licence id, or None on any miss/error.

    The authoritative licence-name source for case-derived placeholders
    (`nama_izin`, and `jenis_pengadaan` parsing). NEVER raises."""
    if not is_siap_db_configured():
        return None
    try:
        lid = int(license_id)
    except (TypeError, ValueError):
        return None
    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SQL_LICENSE_NAME, lid)
        if row is None or row["name"] is None:
            return None
        return str(row["name"]).strip() or None
    except Exception:  # pragma: no cover — defensive
        logger.exception("get_license_name failed (returning None) | license_id=%s", license_id)
        return None


# ===========================================================================
# get_applicant_form_id — resolve a licence's APPLICANT "Formulir Isian" form.
#
# A licence's forms live behind
#   license -> license_approval_step -> license_approval_step_form -> forms
# (the SAME join SIAP's License::GetLicenseRequestForm walks). A licence has
# SEVERAL forms across its steps — the applicant's Formulir Isian, and separate
# officer/system forms such as the "Penomoran" numbering form. We must fill ONLY
# the APPLICANT one; stamping the Penomoran form would be wrong and, per the
# form-value endpoint's form_id-belongs-to-licence guard, a different write
# entirely.
#
# SIAP discriminates the applicant form by the human title "Form Pemohon".
# NOTE: on live Beta both PKPP forms share code='ppkp' — the discriminator is in
# forms.NAME, not forms.code (form 560 name="Form Pemohon Persetujuan Pengadaan
# Kapal Perikanan" vs form 768 name="Form Penomoran PPKP"). SIAP's own
# License::GetLicenseRequestFormPemohon filters `forms.code LIKE '%Form Pemohon%'`,
# which matches NOTHING against this data — so we filter `forms.name ILIKE
# '%Form Pemohon%'` (the Penomoran form's name does NOT match: "Penomoran" ≠
# "Pemohon"), then pick the lowest form_id deterministically so a licence with
# more than one applicant form is stable. Data-driven — no hardcoded per-licence
# form_id (PKPP 459 → 560 is discovered here, not baked in). Read-only; the form
# code/name are non-PII.
# ===========================================================================

# license_id → the APPLICANT "Formulir Isian" form_id. Walks the same join as
# License::GetLicenseRequestForm and filters to the applicant form by its title
# (forms.name ILIKE '%Form Pemohon%') — NOT forms.code, which is 'ppkp' for both
# the applicant and the Penomoran form on live data. DISTINCT + lowest form_id so
# a multi-step licence that references the same applicant form on several steps
# still yields one deterministic id.
_SQL_APPLICANT_FORM_ID = """
    SELECT f.form_id
      FROM ptsp.license_approval_step s
      JOIN ptsp.license_approval_step_form sf
            ON sf.approval_step_id = s.approval_step_id
      JOIN ptsp.forms f
            ON f.form_id = sf.form_id
     WHERE s.license_id = $1
       AND f.name ILIKE '%Form Pemohon%'
     ORDER BY f.form_id
     LIMIT 1
"""


async def get_applicant_form_id(license_id: int) -> Optional[int]:
    """Return the APPLICANT Formulir-Isian `form_id` for a licence, or None.

    Resolves the applicant input form via
    license_approval_step -> license_approval_step_form -> forms, filtered to
    `forms.name ILIKE '%Form Pemohon%'` — the applicant form's human title,
    which excludes the officer/system "Penomoran" numbering form. (forms.code is
    'ppkp' for BOTH on live Beta, so we key on the name, not the code SIAP's own
    GetLicenseRequestFormPemohon filters on.) Picks the lowest form_id so the
    result is deterministic for a licence that references the form on several
    steps.

    Generic across licences — for PKPP (license 459) this returns 560 by
    DISCOVERY, not a hardcode. NEVER raises: a miss / DB error / unconfigured DB
    returns None so the caller (fill_siap_form) degrades to an honest "form not
    resolvable" message rather than filling the wrong form. Non-PII; the
    license_id + resolved form_id are the only things logged.
    """
    if not is_siap_db_configured():
        logger.info(
            "get_applicant_form_id: SIAP DB not configured | license_id=%s",
            license_id,
        )
        return None
    try:
        lid = int(license_id)
    except (TypeError, ValueError):
        logger.warning("get_applicant_form_id: bad license_id=%r", license_id)
        return None
    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SQL_APPLICANT_FORM_ID, lid)
        if row is None or row["form_id"] is None:
            logger.info(
                "get_applicant_form_id: no applicant form found | license_id=%s",
                lid,
            )
            return None
        form_id = int(row["form_id"])
        logger.info(
            "get_applicant_form_id | license_id=%s | form_id=%s", lid, form_id
        )
        return form_id
    except Exception:  # pragma: no cover — defensive; never break the officer path
        logger.exception(
            "get_applicant_form_id failed (returning None) | license_id=%s",
            license_id,
        )
        return None


# ===========================================================================
# get_form_value_properties — read the request's filled Formulir-Isian row.
#
# The whole applicant Formulir Isian (text fields + FLE file slots) is ONE
# ptsp.form_value.properties JSONB row keyed by (form_id, request_id); its keys
# ARE the SK template keys. SIAP generates the SK FROM that form_value. The
# officer copilot's draft_sk reads it back so a draft reflects EXACTLY what the
# citizen/officer entered in SIAP (no divergent re-read), and the DRAFT GATE
# refuses to draft while the vessel-subject fields are still blank.
#
# Read-only by contract (parameterised SELECT + the read-only-transaction pool
# guard). NEVER raises — a miss / DB error / unconfigured DB returns {} so the
# caller degrades. The property VALUES are PII / applicant data and are NEVER
# logged — only the form_id, request_id, and the COUNT of keys.
# ===========================================================================

# (form_id, request_id) → the filled form's properties JSONB (as text, parsed
# by the caller). The whole JSONB is returned so the caller can read ANY field
# key the form holds without this module knowing the form's key set in advance.
_SQL_FORM_VALUE = """
    SELECT properties
      FROM ptsp.form_value
     WHERE form_id = $1
       AND request_id = $2
     LIMIT 1
"""


async def get_form_value_properties(form_id: int, request_id: int) -> Optional[dict]:
    """Return the request's filled `ptsp.form_value.properties` as a dict.

    Keyed by (form_id, request_id) — the ONE JSONB row that holds the whole
    applicant Formulir Isian (text fields + file slots). Its keys are the SK
    template keys; SIAP generates the SK from this row. draft_sk reads it as the
    source of truth for a draft, and the DRAFT GATE keys on whether its
    vessel-subject fields are filled.

    NEVER raises. Return contract distinguishes "definitely not filled" from
    "could not read":
      * `{}` — the row is genuinely absent / properties NULL / unconfigured DB /
        bad ids / corrupt JSON. The form is treated as EMPTY (the gate applies).
      * `None` — a transient DB read error (pool/query failure). The caller
        CANNOT tell whether the form is filled, so it must NOT gate on this (a
        DB hiccup should degrade to the ungated draft, not a false "form empty"
        refusal on an already-filled form).
    The property VALUES are applicant PII and are NEVER logged (only the ids +
    the key COUNT). Mirrors get_request_case_meta / get_person_profile_properties.
    """
    if not is_siap_db_configured():
        logger.info(
            "get_form_value_properties: SIAP DB not configured | form_id=%s "
            "request_id=%s",
            form_id, request_id,
        )
        return {}
    try:
        fid = int(form_id)
        rid = int(request_id)
    except (TypeError, ValueError):
        logger.warning(
            "get_form_value_properties: bad ids | form_id=%r request_id=%r",
            form_id, request_id,
        )
        return {}
    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SQL_FORM_VALUE, fid, rid)
        if row is None or row["properties"] is None:
            logger.info(
                "get_form_value_properties: form_value not found | form_id=%s "
                "request_id=%s",
                fid, rid,
            )
            return {}
        raw = row["properties"]
        # asyncpg returns a JSONB column as a str (no codec registered) or, with
        # a codec, as a dict. Handle both; a str is parsed with json.
        if isinstance(raw, str):
            import json
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                logger.warning(
                    "get_form_value_properties: properties not valid JSON | "
                    "form_id=%s request_id=%s", fid, rid,
                )
                return {}
        elif isinstance(raw, dict):
            parsed = raw
        else:
            return {}
        if not isinstance(parsed, dict):
            return {}
        # Never log the values — only how many keys were read.
        logger.info(
            "get_form_value_properties | form_id=%s request_id=%s keys=%d",
            fid, rid, len(parsed),
        )
        return parsed
    except Exception:  # pragma: no cover — defensive; never break the officer path
        # A transient READ failure (pool/query error) — NOT a genuine empty form.
        # Return None so the caller degrades to the ungated draft instead of
        # falsely refusing "Formulir Isian masih kosong" on a filled form.
        logger.exception(
            "get_form_value_properties read failed (returning None) | form_id=%s "
            "request_id=%s",
            form_id, request_id,
        )
        return None
