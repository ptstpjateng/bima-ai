"""
SIAP Jateng tool layer — Tier 1 (BIMA's "SIAP-as-MCP" capability).

Per [[Decisions]] §22, BIMA goes DEEP on SIAP (DPMPTSP's own licensing
system) via a registry of tools the LLM picks from, "like an MCP server".
This module implements the five Tier-1 tools from [[SIAP API Inventory]]
Half 2 §6 — all read-only, all buildable now:

  1. siap_get_requirements      — documents/conditions a SIAP licence needs
                                  (the fix for the 2026-05-19 screenshot bug:
                                  BIMA could not answer "apa syarat Izin
                                  Pemakaian Tanah")
  2. siap_get_status            — live application status for one ticket
  3. siap_get_status_timeline   — chronological step history of a ticket
  4. siap_lookup_license        — search SIAP's licence catalogue by keyword
  5. siap_list_licenses_by_sektor — every licence DPMPTSP runs under a sektor

Tier 2 (officer-facing depth: person permits, full case dossier, SLA check,
regulations) and Tier 3 (blocked on SIAP-team data/endpoints, and the WRITE
tools) are deliberately NOT in this module — see the inventory.

Design contract:
  * Each tool is an async function with a typed signature and docstring.
  * Each returns a STRUCTURED dict (never raw asyncpg rows, never an
    exception). On any failure — SIAP DB not configured, miss, query
    error — the dict still has a stable shape so the Gemini agent can
    phrase the answer correctly. Tools degrade, they never raise.
  * Every SQL statement is a parameterised SELECT ($1, $2, …). There is
    no string interpolation into SQL anywhere in this file — read-only by
    construction, on top of the read-only-transaction guard in siap_db.py.
  * `siap_get_requirements` parses the HTML in `properties.requirements`
    into clean text with a stdlib `html.parser` subclass — no BeautifulSoup,
    no lxml. ai-engine stays dependency-light.

Backing data (all in `dbsiapjateng`, schema `ptsp`, verified 2026-05-21):
  * ptsp.license               — 472-row licence catalogue. `stereotype`
    in {ROOT, SECTOR, LICENSE, NON-LICENSE}. `properties JSONB` carries
    `requirements` (HTML), `legal_basis` (HTML), `time_period` (SLA days),
    `retribution_fee`.
  * ptsp.license_request       — one row per permohonan; `properties->>'ticket'`.
  * ptsp.get_license_request_log(request_id) — stored fn, per-ticket step log.
  * ptsp.license_approval_step + public.roles — desk owner per step.
  * ptsp.vi_monitoring_berkas_v3 — denormalised live status tracker.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from html.parser import HTMLParser
from typing import Any, Optional

from services.siap_db import get_siap_pool, is_siap_db_configured

logger = logging.getLogger("bima_ai.siap_tools")


# ===========================================================================
# HTML-requirements parsing
#
# SIAP stores `properties.requirements` as Word-pasted HTML — a mix of
# <ol><li>, <p>, <br>, and stray inline styles. We want a clean list of
# requirement lines. A tiny stdlib HTMLParser subclass beats a regex strip:
# it correctly turns <li> / <br> / block tags into line breaks and drops
# everything else, with zero extra dependencies.
# ===========================================================================

# Tags that should force a line break when opened or closed.
_BLOCK_TAGS = {"li", "p", "div", "ol", "ul", "tr", "h1", "h2", "h3", "h4"}


class _RequirementsHTMLStripper(HTMLParser):
    """Collapse SIAP requirement HTML into newline-separated plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "br" or tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def _html_to_lines(raw_html: Optional[str]) -> list[str]:
    """
    Parse a SIAP `properties.requirements` / `legal_basis` HTML blob into a
    clean list of non-empty lines.

    Steps: HTML → newline-separated text (block tags become breaks) → unescape
    entities → split on newlines → strip a leading enumerator ("1.", "a)", "-")
    so the caller can re-number consistently → drop empties and noise lines.
    """
    if not raw_html or not raw_html.strip():
        return []

    parser = _RequirementsHTMLStripper()
    try:
        parser.feed(raw_html)
    except Exception:  # pragma: no cover — defensive against malformed HTML
        # Fall back to a blunt tag strip so we still return *something*.
        text = re.sub(r"<[^>]+>", "\n", raw_html)
    else:
        text = parser.get_text()

    text = html.unescape(text)

    lines: list[str] = []
    for chunk in text.split("\n"):
        # Normalise internal whitespace and non-breaking spaces.
        line = re.sub(r"\s+", " ", chunk.replace("\xa0", " ")).strip()
        if not line:
            continue
        # Strip a leading list enumerator: "1.", "1)", "a.", "- ", "• ".
        line = re.sub(r"^\s*(?:\d+|[a-zA-Z])\s*[.)]\s*", "", line)
        line = re.sub(r"^\s*[-•*]\s*", "", line).strip()
        if not line:
            continue
        # Drop pure-punctuation / decorative residue.
        if not re.search(r"[0-9A-Za-z]", line):
            continue
        lines.append(line)
    return lines


def _clean_scalar(value: Optional[str]) -> Optional[str]:
    """Trim a JSONB-extracted scalar; map empty/placeholder strings to None."""
    if value is None:
        return None
    v = str(value).strip()
    if not v or v.lower() in {"-", "null", "none"}:
        return None
    return v


# ===========================================================================
# Tool 1 — siap_get_requirements  (THE PRIORITY)
#
# Fixes the 2026-05-19 screenshot bug: BIMA could not answer
# "apa syarat Izin Pemakaian Tanah". One ptsp.license row answers
# requirements + legal basis + SLA + fee at once.
# ===========================================================================

_SQL_REQUIREMENTS_BY_ID = """
    SELECT license_id,
           name,
           properties ->> 'requirements'    AS requirements_html,
           properties ->> 'legal_basis'     AS legal_basis_html,
           properties ->> 'time_period'     AS time_period,
           properties ->> 'retribution_fee' AS retribution_fee
      FROM ptsp.license
     WHERE license_id = $1
       AND stereotype = 'LICENSE'
     LIMIT 1
"""

# Name match: case-insensitive substring. ORDER BY length picks the most
# specific (shortest) name when several licences contain the phrase, so
# "Izin Pemakaian Tanah" prefers the base licence over a longer variant.
_SQL_REQUIREMENTS_BY_NAME = """
    SELECT license_id,
           name,
           properties ->> 'requirements'    AS requirements_html,
           properties ->> 'legal_basis'     AS legal_basis_html,
           properties ->> 'time_period'     AS time_period,
           properties ->> 'retribution_fee' AS retribution_fee
      FROM ptsp.license
     WHERE stereotype = 'LICENSE'
       AND name ILIKE '%' || $1 || '%'
     ORDER BY length(name) ASC
     LIMIT 1
"""

# Tool 1 enrichment — the normalised join table. Sparse (only ~195 rows
# across 384 LICENSE rows) so it is an *enrichment*, never the primary.
_SQL_REQUIREMENTS_JOIN = """
    SELECT r.name AS requirement_name
      FROM ptsp.license_requirements lr
      JOIN ptsp.requirements r ON r.requirements_id = lr.requirements_id
     WHERE lr.license_id = $1
     ORDER BY r.requirements_id
"""


async def siap_get_requirements(
    license_id: Optional[int] = None,
    license_name: Optional[str] = None,
) -> dict:
    """
    Return the documents/conditions a SIAP licence requires, plus its legal
    basis, SLA (working days) and retribution fee — all from one DB row.

    Resolve by `license_id` (exact) OR `license_name` (case-insensitive
    substring; most-specific match wins). Pass at least one. `license_id`
    takes precedence when both are given.

    The primary requirements source is `ptsp.license.properties.requirements`
    (HTML, always present on LICENSE rows). We parse the HTML to clean lines.
    When `ptsp.license_requirements` has structured rows for the licence we
    surface them too as `requirements_structured` — but the HTML list stays
    authoritative because the join table is sparse.

    Returns:
      {
        "found": bool,
        "license_id": int | None,
        "license_name": str | None,
        "requirements": list[str],            # clean lines, no HTML, no numbering
        "requirements_structured": list[str], # enrichment (may be empty)
        "legal_basis": list[str],
        "sla_working_days": int | None,
        "retribution_fee": str | None,
        "source": "ptsp.license.properties",
      }
    On miss / not configured / error: found=False with a `note`.
    """
    if not is_siap_db_configured():
        return {
            "found": False,
            "note": "Integrasi basis data SIAP belum dikonfigurasi pada lingkungan ini.",
        }
    if license_id is None and not (license_name and license_name.strip()):
        return {
            "found": False,
            "note": "Wajib mengisi license_id atau license_name.",
        }

    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            if license_id is not None:
                row = await conn.fetchrow(_SQL_REQUIREMENTS_BY_ID, int(license_id))
            else:
                row = await conn.fetchrow(
                    _SQL_REQUIREMENTS_BY_NAME, license_name.strip()
                )

            if row is None:
                return {
                    "found": False,
                    "license_id": license_id,
                    "license_name": license_name,
                    "note": "Tidak ada izin SIAP yang cocok dengan kriteria itu.",
                }

            resolved_id = row["license_id"]
            structured_rows = await conn.fetch(_SQL_REQUIREMENTS_JOIN, resolved_id)

        requirements = _html_to_lines(row["requirements_html"])
        legal_basis = _html_to_lines(row["legal_basis_html"])
        structured = [
            r["requirement_name"].strip()
            for r in structured_rows
            if r["requirement_name"] and r["requirement_name"].strip()
        ]

        sla_raw = _clean_scalar(row["time_period"])
        sla_days: Optional[int] = None
        if sla_raw is not None:
            digits = re.sub(r"\D", "", sla_raw)
            sla_days = int(digits) if digits else None

        result = {
            "found": True,
            "license_id": resolved_id,
            "license_name": row["name"],
            "requirements": requirements,
            "requirements_structured": structured,
            "legal_basis": legal_basis,
            "sla_working_days": sla_days,
            "retribution_fee": _clean_scalar(row["retribution_fee"]),
            "source": "ptsp.license.properties",
        }
        logger.info(
            "siap_get_requirements | id=%s name=%r | reqs=%d legal=%d",
            resolved_id, row["name"], len(requirements), len(legal_basis),
        )
        return result
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("siap_get_requirements failed | id=%s name=%r", license_id, license_name)
        return {
            "found": False,
            "note": f"Gagal membaca persyaratan dari basis data SIAP: {exc}",
        }


# ===========================================================================
# Tool 1b — siap_get_templates  (official downloadable sign-doc templates)
#
# SIAP stores the citizen-facing document templates (Surat Permohonan, Pakta
# Integritas, Surat Pesanan, plus reference PDFs) as files in `ptsp.files`,
# keyed to a licence by `owner_id`. `stereotype='PUBLIC-DOWNLOAD'` marks the
# ones meant for applicants. `internal_filename` is the storage key
# (`master/{ULID}.{ext}`), served publicly at
# `{SIAP_STORAGE_BASE}/storage/{internal_filename}`.
#
# BIMA's doc-prep co-pilot uses this to fill + deliver the REAL SIAP template
# instead of an invented layout. Read-only; non-PII (these are blank forms).
# ===========================================================================

_SQL_TEMPLATES_BY_LICENSE = """
    SELECT file_id,
           file_name,
           file_type,
           internal_filename
      FROM ptsp.files
     WHERE stereotype = 'PUBLIC-DOWNLOAD'
       AND owner_id = $1
     ORDER BY file_id
"""

# Map an SIAP template file_name to the BIMA doc_type it satisfies. Keyword
# match on the lowercased name; first hit wins. Names that match nothing are
# returned as doc_type=None (reference attachments, e.g. the spec PDFs).
_TEMPLATE_NAME_TO_DOCTYPE = (
    ("pakta", "pakta_integritas"),
    ("permohonan", "surat_permohonan"),
    ("pesanan", "surat_pesanan"),
)


def _doctype_for_template(file_name: str) -> Optional[str]:
    low = (file_name or "").lower()
    for needle, doc_type in _TEMPLATE_NAME_TO_DOCTYPE:
        if needle in low:
            return doc_type
    return None


async def siap_get_templates(license_id: int) -> dict:
    """Return the official PUBLIC-DOWNLOAD template files SIAP hosts for a licence.

    Args:
      license_id: the resolved SIAP licence id (e.g. 459 for PPKP).

    Returns:
      {
        "found": bool,
        "license_id": int,
        "templates": [
          {"file_id": int, "file_name": str, "file_type": str,
           "internal_filename": str, "doc_type": str | None}
        ],
        "by_doctype": {doc_type: internal_filename, ...},  # only mapped ones
      }
    On miss / not configured / error: found=False with a `note`. Never raises.
    """
    if not is_siap_db_configured():
        return {"found": False, "note": "Integrasi basis data SIAP belum dikonfigurasi."}
    if license_id is None:
        return {"found": False, "note": "Wajib mengisi license_id."}

    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SQL_TEMPLATES_BY_LICENSE, int(license_id))

        templates = []
        by_doctype: dict[str, str] = {}
        for r in rows:
            doc_type = _doctype_for_template(r["file_name"])
            templates.append({
                "file_id": r["file_id"],
                "file_name": r["file_name"],
                "file_type": r["file_type"],
                "internal_filename": r["internal_filename"],
                "doc_type": doc_type,
            })
            # First mapped hit per doc_type wins (ORDER BY file_id above).
            if doc_type and doc_type not in by_doctype:
                by_doctype[doc_type] = r["internal_filename"]

        logger.info(
            "siap_get_templates | license_id=%s | files=%d mapped=%d",
            license_id, len(templates), len(by_doctype),
        )
        return {
            "found": bool(templates),
            "license_id": int(license_id),
            "templates": templates,
            "by_doctype": by_doctype,
        }
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("siap_get_templates failed | license_id=%s", license_id)
        return {"found": False, "license_id": license_id,
                "note": f"Gagal membaca template dari basis data SIAP: {exc}"}


# ===========================================================================
# Tool 1c — siap_get_output_template  (the OFFICER-facing SK/Persetujuan template)
#
# SIAP hosts the DECISION-OUTPUT template (the Surat PKPP / approval letter the
# officer drafts, distinct from the citizen-facing PUBLIC-DOWNLOAD forms) as a
# file in `ptsp.files` marked `stereotype='LICENSE-RECOMMEND'` and keyed to the
# licence by `owner_id`. For PPKP (license 459) it is file_id=1533,
# "Surat PKPP V2.docx". It fills via literal `[data.KEY]` placeholder tokens
# (see services/siap_templates.fill_docx_placeholders), NOT the citizen forms'
# "Label : <blank>" layout. Read-only; the blank template is non-PII.
# ===========================================================================

_SQL_OUTPUT_TEMPLATE_BY_LICENSE = """
    SELECT file_id,
           file_name,
           file_type,
           internal_filename
      FROM ptsp.files
     WHERE stereotype = 'LICENSE-RECOMMEND'
       AND owner_id = $1
     ORDER BY file_id
     LIMIT 1
"""


async def siap_get_output_template(license_id: int) -> dict:
    """Return the SIAP LICENSE-RECOMMEND output template for a licence — the
    Surat Keputusan/Persetujuan (SK) draft the officer issues, filled via
    `[data.KEY]` placeholders.

    Mirrors `siap_get_templates` but targets the officer-facing DECISION output
    (stereotype 'LICENSE-RECOMMEND') rather than the citizen download forms
    (stereotype 'PUBLIC-DOWNLOAD'). One licence has one such template; we take
    the first (lowest file_id) if SIAP ever holds more than one.

    Args:
      license_id: the resolved SIAP licence id (e.g. 459 for PPKP).

    Returns:
      {
        "found": bool,
        "license_id": int,
        "file_id": int | None,
        "file_name": str | None,
        "file_type": str | None,
        "internal_filename": str | None,   # storage key, master/{ULID}.docx
      }
    On miss / not configured / error: found=False with a `note`. Never raises.
    """
    if not is_siap_db_configured():
        return {"found": False, "note": "Integrasi basis data SIAP belum dikonfigurasi."}
    if license_id is None:
        return {"found": False, "note": "Wajib mengisi license_id."}

    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SQL_OUTPUT_TEMPLATE_BY_LICENSE, int(license_id))
        if row is None:
            logger.info(
                "siap_get_output_template | license_id=%s | no LICENSE-RECOMMEND file",
                license_id,
            )
            return {
                "found": False,
                "license_id": int(license_id),
                "note": (
                    "Tidak ada template output (LICENSE-RECOMMEND) untuk izin ini "
                    "di SIAP."
                ),
            }
        logger.info(
            "siap_get_output_template | license_id=%s | file_id=%s",
            license_id, row["file_id"],
        )
        return {
            "found": True,
            "license_id": int(license_id),
            "file_id": row["file_id"],
            "file_name": row["file_name"],
            "file_type": row["file_type"],
            "internal_filename": row["internal_filename"],
        }
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("siap_get_output_template failed | license_id=%s", license_id)
        return {"found": False, "license_id": license_id,
                "note": f"Gagal membaca template output dari basis data SIAP: {exc}"}


# ===========================================================================
# Tool 1d — siap_get_step_output_template  (PER-STEP output selection)
#
# ([[Generalize copilot SK/output-doc from real data]] — task #80.)
#
# Which output template applies depends on the DESK the case is currently on.
# From the SIAP step→doc investigation:
#   * The TECHNICAL / Balai review desk produces the Rekomtek
#     (stereotype 'LICENSE-RECOMMEND').
#   * The final Kepala-Dinas SIGNING desk produces the Surat Keputusan
#     (stereotype 'LICENSE-SK').
# The binding in SIAP is by the two signing RESOURCES (Tanda Tangan
# Rekomendasi vs Tanda Tangan Berkas), scoped by the step's owning role, NOT a
# per-step template pointer. BIMA has no per-step pointer either, so we key the
# choice on the case-meta signals BIMA already computes in
# `get_request_case_meta`: `is_final_step` (the desk's role is a configured
# SK-signing role) and the step `stereotype` (a strong secondary hint —
# 'TTE'/'PENANDATANGANAN BERKAS'/'PENYERAHAN SK' ⇒ SK; 'SURVEY-LAPANGAN'/
# 'REVIEW-PERMOHONAN' ⇒ Rekomtek).
#
# IMPORTANT (no-guess fallback): BIMA's current `[data.KEY]` filler matches the
# LICENSE-RECOMMEND syntax; LICENSE-SK templates use PhpWord `${key}` which BIMA
# does NOT fill. So even when the step points at the SK desk, if only a
# LICENSE-SK template exists, drafting it would leave it almost entirely blank.
# We therefore return WHICH stereotype the step calls for AND which templates
# actually exist, letting the caller choose the fillable one and SAY so when it
# had to fall back — rather than silently drafting the wrong/empty doc.
# ===========================================================================

# Step stereotypes that indicate the FINAL SK-signing desk (produces LICENSE-SK).
# Case-insensitive substring match on the step's own `stereotype` column.
# Broadened (M1) so PENANDATANGANAN SURAT KEPUTUSAN / TANDA TANGAN SK / PENETAPAN
# SK all read as SK-leaning — the signing/SK nouns, not just the exact three
# earlier phrases. `is_final_step` (role-based) remains the AUTHORITATIVE primary
# signal in _preferred_stereotype_for_step; this only sharpens the secondary hint.
_SK_STEP_STEREOTYPES = (
    "tte", "penandatanganan", "tanda tangan", "keputusan", "penetapan",
    "penyerahan sk", " sk", "sk ", "-sk", "_sk",
)
# Step stereotypes that indicate a technical/recommendation desk (LICENSE-RECOMMEND).
_RECOMMEND_STEP_STEREOTYPES = (
    "survey", "review-permohonan", "review permohonan",
    "penyerahan surat persetujuan", "rekom",
)


async def _fetch_template_row(conn, license_id: int, stereotype: str):
    """Return the first (lowest file_id) ptsp.files row of `stereotype` for the
    licence, as a plain dict, or None. Parameterised — `stereotype` is passed
    as a bind value, never interpolated."""
    row = await conn.fetchrow(
        """
        SELECT file_id, file_name, file_type, internal_filename
          FROM ptsp.files
         WHERE stereotype = $2
           AND owner_id = $1
         ORDER BY file_id
         LIMIT 1
        """,
        int(license_id), stereotype,
    )
    if row is None:
        return None
    return {
        "file_id": row["file_id"],
        "file_name": row["file_name"],
        "file_type": row["file_type"],
        "internal_filename": row["internal_filename"],
    }


def _preferred_stereotype_for_step(
    *, is_final_step: bool, step_stereotype: Optional[str]
) -> str:
    """Decide which output stereotype the CURRENT step calls for.

    Returns 'LICENSE-SK' for the final signing desk, else 'LICENSE-RECOMMEND'.
    Primary signal: `is_final_step` (BIMA's role-based SK-signing detection).
    Secondary: the step's own `stereotype` column (TTE / PENANDATANGANAN BERKAS
    ⇒ SK; SURVEY-LAPANGAN / REVIEW-PERMOHONAN ⇒ Rekomtek). Defaults to the
    Rekomtek when nothing is decisive — it is the earlier, more common desk and
    the one BIMA can actually fill."""
    stereo = (step_stereotype or "").strip().lower()
    if is_final_step:
        return "LICENSE-SK"
    if stereo:
        if any(s in stereo for s in _SK_STEP_STEREOTYPES):
            return "LICENSE-SK"
        if any(s in stereo for s in _RECOMMEND_STEP_STEREOTYPES):
            return "LICENSE-RECOMMEND"
    return "LICENSE-RECOMMEND"


async def siap_get_step_output_template(
    license_id: int,
    *,
    is_final_step: bool = False,
    step_stereotype: Optional[str] = None,
) -> dict:
    """Return the output template for the case at its CURRENT step, choosing
    between the Rekomtek (LICENSE-RECOMMEND) and the SK (LICENSE-SK) by the
    desk the case sits on.

    The step signals come from `get_request_case_meta` (siap_db): pass
    `is_final_step` (the desk is a configured SK-signing role) and the step
    `stereotype`. We resolve the PREFERRED stereotype for that step, then return
    the actual template — but if the preferred one is missing OR is not a
    BIMA-fillable `.docx`, we fall back to whichever fillable output template
    the licence DOES have, and REPORT the fallback so the caller can tell the
    officer honestly which document was drafted.

    Returns:
      {
        "found": bool,
        "license_id": int,
        "stereotype": str | None,           # the stereotype we RESOLVED to
        "preferred_stereotype": str,        # what the step called for
        "fell_back": bool,                  # preferred missing/unfillable → other
        "file_id": int | None,
        "file_name": str | None,
        "file_type": str | None,
        "internal_filename": str | None,
        "doc_kind": "rekomendasi" | "sk" | None,   # human doc family
      }
    On miss / not configured / error: found=False with a `note`. Never raises.
    """
    if not is_siap_db_configured():
        return {"found": False, "note": "Integrasi basis data SIAP belum dikonfigurasi."}
    if license_id is None:
        return {"found": False, "note": "Wajib mengisi license_id."}

    preferred = _preferred_stereotype_for_step(
        is_final_step=is_final_step, step_stereotype=step_stereotype
    )
    # The two stereotypes, preferred first, then the other as the fallback.
    order = (
        ["LICENSE-SK", "LICENSE-RECOMMEND"]
        if preferred == "LICENSE-SK"
        else ["LICENSE-RECOMMEND", "LICENSE-SK"]
    )

    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            rows: dict[str, Optional[dict]] = {}
            for stereo in ("LICENSE-RECOMMEND", "LICENSE-SK"):
                rows[stereo] = await _fetch_template_row(conn, int(license_id), stereo)

        # Prefer the step's stereotype, but only if it is a fillable .docx. BIMA
        # fills `[data.KEY]` (LICENSE-RECOMMEND) — a legacy .doc/.rtf or the
        # `${key}` SK docx cannot be filled here, so we treat a non-.docx as
        # "not fillable" and let the fallback pick the other one.
        def _is_fillable_docx(r: Optional[dict]) -> bool:
            fn = (r or {}).get("internal_filename") or ""
            return bool(fn) and str(fn).lower().endswith(".docx")

        chosen_stereo: Optional[str] = None
        for stereo in order:
            if _is_fillable_docx(rows.get(stereo)):
                chosen_stereo = stereo
                break
        # If neither is a fillable .docx, still surface the preferred row (if
        # any exists) so the caller can report "template ada tapi belum bisa
        # diisi otomatis" rather than a bare not-found.
        if chosen_stereo is None:
            for stereo in order:
                if rows.get(stereo) is not None:
                    chosen_stereo = stereo
                    break

        if chosen_stereo is None:
            logger.info(
                "siap_get_step_output_template | license_id=%s | no template "
                "(preferred=%s)", license_id, preferred,
            )
            return {
                "found": False,
                "license_id": int(license_id),
                "preferred_stereotype": preferred,
                "note": (
                    "Tidak ada template output (Rekomendasi/SK) untuk izin ini "
                    "di SIAP."
                ),
            }

        row = rows[chosen_stereo] or {}
        doc_kind = "sk" if chosen_stereo == "LICENSE-SK" else "rekomendasi"
        fell_back = chosen_stereo != preferred
        logger.info(
            "siap_get_step_output_template | license_id=%s | preferred=%s "
            "chosen=%s fell_back=%s file_id=%s",
            license_id, preferred, chosen_stereo, fell_back, row.get("file_id"),
        )
        return {
            "found": True,
            "license_id": int(license_id),
            "stereotype": chosen_stereo,
            "preferred_stereotype": preferred,
            "fell_back": fell_back,
            "file_id": row.get("file_id"),
            "file_name": row.get("file_name"),
            "file_type": row.get("file_type"),
            "internal_filename": row.get("internal_filename"),
            "doc_kind": doc_kind,
        }
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception(
            "siap_get_step_output_template failed | license_id=%s", license_id
        )
        return {"found": False, "license_id": license_id,
                "note": f"Gagal membaca template output dari basis data SIAP: {exc}"}


# ===========================================================================
# Tool 2 — siap_get_status
#
# Per-ticket live status. The inventory backs this with the REST endpoint;
# we read the same view (ptsp.vi_monitoring_berkas_v3) directly so the whole
# tool layer needs only the one SIAP DB connection — no Sanctum token, no
# second integration surface. Column names mirror MonitoringBerkasResource.
# ===========================================================================

_SQL_STATUS_BY_TICKET = """
    SELECT ticket             AS no_tiket,
           nama_izin          AS nama_perizinan,
           nama_bidang,
           nama_pemohon,
           bloksistem_nama    AS posisi_berkas,
           tanggal_daftar     AS tanggal_permohonan,
           status_permohonan
      FROM ptsp.vi_monitoring_berkas_v3
     WHERE ticket = $1
     LIMIT 1
"""


async def siap_get_status(ticket: str) -> dict:
    """
    Return the live status of one SIAP permit application by ticket number.

    `ticket` is the 9-digit zero-padded SIAP ticket (e.g. "000077591"); a
    shorter run of digits is zero-padded automatically.

    Returns:
      {
        "found": bool,
        "no_tiket": str,
        "nama_perizinan": str,
        "nama_bidang": str,
        "nama_pemohon": str,
        "posisi_berkas": str,        # which desk currently holds the file
        "tanggal_permohonan": str,
        "status_permohonan": str,
      }
    On miss / not configured / error: found=False with a `note`.
    """
    if not is_siap_db_configured():
        return {
            "found": False,
            "note": "Integrasi basis data SIAP belum dikonfigurasi pada lingkungan ini.",
        }
    digits = re.sub(r"\D", "", ticket or "")
    if not digits:
        return {"found": False, "note": "Nomor tiket tidak valid."}
    norm_ticket = digits.zfill(9)

    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SQL_STATUS_BY_TICKET, norm_ticket)
        if row is None:
            return {
                "found": False,
                "no_tiket": norm_ticket,
                "note": f"Tidak ada permohonan dengan tiket {norm_ticket}.",
            }
        result: dict[str, Any] = {"found": True}
        for key in (
            "no_tiket", "nama_perizinan", "nama_bidang", "nama_pemohon",
            "posisi_berkas", "tanggal_permohonan", "status_permohonan",
        ):
            value = row[key]
            if value is None:
                result[key] = None
            elif hasattr(value, "isoformat"):  # datetime → ISO string
                result[key] = value.isoformat()
            else:
                result[key] = str(value)
        logger.info("siap_get_status | ticket=%s | status=%s",
                    norm_ticket, result.get("status_permohonan"))
        return result
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("siap_get_status failed | ticket=%s", norm_ticket)
        return {
            "found": False,
            "no_tiket": norm_ticket,
            "note": f"Gagal membaca status dari basis data SIAP: {exc}",
        }


# ===========================================================================
# Tool 3 — siap_get_status_timeline
#
# Chronological step history of a ticket incl. delay-state flags. Reuses
# SIAP's own stored function ptsp.get_license_request_log(request_id) — we
# resolve request_id from the ticket first, then enrich each step with the
# owning desk name from public.roles via license_approval_step.group_id.
# ===========================================================================

_SQL_REQUEST_ID_BY_TICKET = """
    SELECT request_id
      FROM ptsp.license_request
     WHERE properties ->> 'ticket' = $1
     LIMIT 1
"""


async def siap_resolve_request_id(ticket: str) -> Optional[int]:
    """Resolve a license_request's `request_id` from its SIAP ticket.

    A thin, side-effect-free lookup over `_SQL_REQUEST_ID_BY_TICKET` (the same
    statement `siap_get_status_timeline` uses). Returns the integer request_id,
    or None on a miss / unconfigured SIAP DB / any error. NEVER raises — callers
    on the citizen submit path must degrade, not crash.

    Used by the officer-notify hand-off: SIAP's create endpoint can return a
    ticket without an `id`/`request_id`, but flow-based officer resolution needs
    the request_id, so we recover it from the freshly-allocated ticket.
    """
    if not is_siap_db_configured():
        return None
    digits = re.sub(r"\D", "", ticket or "")
    if not digits:
        return None
    norm_ticket = digits.zfill(9)
    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SQL_REQUEST_ID_BY_TICKET, norm_ticket)
        if row is None or row["request_id"] is None:
            return None
        return int(row["request_id"])
    except Exception:  # pragma: no cover — defensive; never break the submit path
        logger.exception("siap_resolve_request_id failed | ticket=%s", norm_ticket)
        return None

# The stored function does the heavy lifting; we LEFT JOIN the desk name so
# the citizen sees "Front Office PTSP" rather than a bare step id. public.roles
# is a non-credential reference table — safe to read.
_SQL_TIMELINE = """
    SELECT log.status,
           log.description,
           log.step_duration,
           log.duration,
           log.duration_status,
           log.created_on,
           log.approval_step_id,
           rol.name AS owner_desk
      FROM ptsp.get_license_request_log($1) AS log
      LEFT JOIN ptsp.license_approval_step las
             ON las.approval_step_id = log.approval_step_id
      LEFT JOIN public.roles rol
             ON rol.id = las.group_id
     ORDER BY log.created_on ASC, log.log_id ASC
"""


async def siap_get_status_timeline(ticket: str) -> dict:
    """
    Return the chronological step-by-step history of one SIAP ticket,
    including per-step delay flags — answers "where did my permit get stuck".

    `ticket` is the 9-digit SIAP ticket; shorter digit runs are zero-padded.

    Each step carries `duration_status` — SIAP's own delay flag:
      GRAY = not started/idle · GREEN = on time · YELLOW = nearing SLA ·
      RED = over SLA.

    Returns:
      {
        "found": bool,
        "no_tiket": str,
        "request_id": int | None,
        "steps": [
          {
            "step": str,            # human-readable step description
            "status": str,          # SUBMITTED / APPROVED / REJECTED / ...
            "owner_desk": str|None, # which desk handled this step
            "entered_on": str|None,
            "step_duration": str|None,
            "duration": str|None,
            "duration_status": str|None,
          }, ...
        ],
      }
    On miss / not configured / error: found=False with a `note`.
    """
    if not is_siap_db_configured():
        return {
            "found": False,
            "note": "Integrasi basis data SIAP belum dikonfigurasi pada lingkungan ini.",
        }
    digits = re.sub(r"\D", "", ticket or "")
    if not digits:
        return {"found": False, "note": "Nomor tiket tidak valid."}
    norm_ticket = digits.zfill(9)

    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            id_row = await conn.fetchrow(_SQL_REQUEST_ID_BY_TICKET, norm_ticket)
            if id_row is None:
                return {
                    "found": False,
                    "no_tiket": norm_ticket,
                    "note": f"Tidak ada permohonan dengan tiket {norm_ticket}.",
                }
            request_id = id_row["request_id"]
            rows = await conn.fetch(_SQL_TIMELINE, request_id)

        steps: list[dict[str, Any]] = []
        for r in rows:
            created = r["created_on"]
            steps.append({
                "step": (r["description"] or "Tahapan").strip(),
                "status": r["status"],
                "owner_desk": r["owner_desk"],
                "entered_on": created.isoformat() if created is not None else None,
                "step_duration": r["step_duration"],
                "duration": str(r["duration"]) if r["duration"] is not None else None,
                "duration_status": r["duration_status"],
            })
        logger.info(
            "siap_get_status_timeline | ticket=%s request_id=%s | steps=%d",
            norm_ticket, request_id, len(steps),
        )
        return {
            "found": True,
            "no_tiket": norm_ticket,
            "request_id": request_id,
            "steps": steps,
        }
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("siap_get_status_timeline failed | ticket=%s", norm_ticket)
        return {
            "found": False,
            "no_tiket": norm_ticket,
            "note": f"Gagal membaca riwayat dari basis data SIAP: {exc}",
        }


# ===========================================================================
# Tool 4 — siap_lookup_license
#
# Search the licence catalogue by keyword. Resolves a human phrase
# ("izin pemakaian tanah") into license_id candidates that feed tools 1 & 5.
# ===========================================================================

# parent name = the SECTOR row this licence hangs under.
_SQL_LOOKUP_LICENSE = """
    SELECT c.license_id,
           c.name,
           p.name AS sektor
      FROM ptsp.license c
      LEFT JOIN ptsp.license p ON p.license_id = c.parent_id
     WHERE c.stereotype = 'LICENSE'
       AND c.name ILIKE '%' || $1 || '%'
     ORDER BY length(c.name) ASC, c.name ASC
     LIMIT $2
"""

_LOOKUP_DEFAULT_LIMIT = 10
_LOOKUP_MAX_LIMIT = 25


async def siap_lookup_license(query: str, limit: int = _LOOKUP_DEFAULT_LIMIT) -> dict:
    """
    Search SIAP's licence catalogue by name keyword.

    Use this to turn a citizen's free-text licence phrase into concrete
    `license_id` values — then call `siap_get_requirements` with the best
    match. `query` is a case-insensitive substring; `limit` is capped at 25.

    Returns:
      {
        "found": bool,
        "query": str,
        "count": int,
        "matches": [{"license_id": int, "name": str, "sektor": str|None}, ...],
      }
    On no match / not configured / error: found=False with a `note`.
    """
    if not is_siap_db_configured():
        return {
            "found": False,
            "note": "Integrasi basis data SIAP belum dikonfigurasi pada lingkungan ini.",
        }
    q = (query or "").strip()
    if not q:
        return {"found": False, "query": query, "note": "Kata kunci pencarian kosong."}

    capped = max(1, min(int(limit) if limit else _LOOKUP_DEFAULT_LIMIT, _LOOKUP_MAX_LIMIT))
    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SQL_LOOKUP_LICENSE, q, capped)
        matches = [
            {
                "license_id": r["license_id"],
                "name": r["name"],
                "sektor": r["sektor"],
            }
            for r in rows
        ]
        logger.info("siap_lookup_license | query=%r | matches=%d", q, len(matches))
        return {
            "found": bool(matches),
            "query": q,
            "count": len(matches),
            "matches": matches,
            **({} if matches else {"note": f"Tidak ada izin SIAP yang cocok dengan '{q}'."}),
        }
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("siap_lookup_license failed | query=%r", q)
        return {
            "found": False,
            "query": q,
            "note": f"Gagal mencari izin di basis data SIAP: {exc}",
        }


# ===========================================================================
# Tool 5 — siap_list_licenses_by_sektor
#
# Given a sektor name, list every licence DPMPTSP administers under it.
# This is the KBLI → sektor → licences narrowing path ([[SIAP API Inventory]]
# §5): KBLI codes have no structural join to SIAP licences, only the free-text
# sektor string overlaps. The match is on a normalised (trim+lower) sektor.
# ===========================================================================

_SQL_LICENSES_BY_SEKTOR = """
    SELECT c.license_id,
           c.name
      FROM ptsp.license p
      JOIN ptsp.license c ON c.parent_id = p.license_id
     WHERE p.stereotype = 'SECTOR'
       AND c.stereotype = 'LICENSE'
       AND lower(trim(p.name)) = lower(trim($1))
     ORDER BY c.name ASC
"""

# Fallback when the sektor name is not an exact match — substring match on the
# SECTOR name so "Perikanan" still resolves "Kelautan dan Perikanan".
_SQL_LICENSES_BY_SEKTOR_FUZZY = """
    SELECT c.license_id,
           c.name,
           p.name AS matched_sektor
      FROM ptsp.license p
      JOIN ptsp.license c ON c.parent_id = p.license_id
     WHERE p.stereotype = 'SECTOR'
       AND c.stereotype = 'LICENSE'
       AND p.name ILIKE '%' || $1 || '%'
     ORDER BY p.name ASC, c.name ASC
     LIMIT 60
"""


async def siap_list_licenses_by_sektor(sektor: str) -> dict:
    """
    List every SIAP licence type DPMPTSP administers under a given sektor.

    `sektor` is a free-text sektor name (e.g. "Kelautan dan Perikanan",
    "Pariwisata"). First tries an exact normalised (trim+lower) match against
    SIAP's 31 SECTOR rows; if none, falls back to a substring match.

    This is the KBLI → sektor → licences narrowing path: a KBLI code maps to
    a sektor, and a sektor maps to *several* licences — never one. Treat the
    result as a shortlist, not a deterministic resolution.

    Returns:
      {
        "found": bool,
        "sektor": str,
        "match_type": "exact" | "fuzzy" | None,
        "count": int,
        "licenses": [{"license_id": int, "name": str}, ...],
      }
    On no match / not configured / error: found=False with a `note`.
    """
    if not is_siap_db_configured():
        return {
            "found": False,
            "note": "Integrasi basis data SIAP belum dikonfigurasi pada lingkungan ini.",
        }
    s = (sektor or "").strip()
    if not s:
        return {"found": False, "sektor": sektor, "note": "Nama sektor kosong."}

    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SQL_LICENSES_BY_SEKTOR, s)
            match_type = "exact"
            if not rows:
                rows = await conn.fetch(_SQL_LICENSES_BY_SEKTOR_FUZZY, s)
                match_type = "fuzzy" if rows else None

        licenses = [
            {"license_id": r["license_id"], "name": r["name"]}
            for r in rows
        ]
        logger.info(
            "siap_list_licenses_by_sektor | sektor=%r match=%s | count=%d",
            s, match_type, len(licenses),
        )
        return {
            "found": bool(licenses),
            "sektor": s,
            "match_type": match_type,
            "count": len(licenses),
            "licenses": licenses,
            **({} if licenses else {
                "note": f"Tidak ada sektor SIAP yang cocok dengan '{s}'."
            }),
        }
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("siap_list_licenses_by_sektor failed | sektor=%r", s)
        return {
            "found": False,
            "sektor": s,
            "note": f"Gagal membaca daftar izin per sektor dari basis data SIAP: {exc}",
        }


# ===========================================================================
# Licence catalogue — the WHOLE SIAP licence list, cached in memory.
#
# This powers the LLM-driven resolver (services/license_resolver.py): instead
# of a brittle `name ILIKE '%query%'` over a regex-cleaned message, BIMA hands
# the COMPLETE catalogue (id | name | sektor, ~384 LICENSE rows) to Gemini and
# lets the model map the citizen's free-text intent to the right licence.
#
# The catalogue is near-static (DPMPTSP's licence list changes rarely), so we
# cache it with a TTL — exactly the in-memory-set-with-TTL + non-blocking
# refresh pattern officer_bridge.py uses for the officer directory. The cache
# is shared across every resolve_license_intent call so each citizen turn only
# costs one (cached) read, and the per-call Gemini grounding block is stable
# (helps implicit prompt caching).
#
# NEVER raises: on a SIAP-DB miss/outage it returns [] so the resolver can fall
# straight through to the fuzzy SQL fallback. License names are NOT PII — only
# the count is logged either way.
# ===========================================================================

# Same parent-join shape as _SQL_LOOKUP_LICENSE but UNFILTERED — the whole
# LICENSE catalogue ordered by name. parent name = the SECTOR row the licence
# hangs under.
_SQL_LICENSE_CATALOGUE = """
    SELECT c.license_id,
           c.name,
           p.name AS sektor
      FROM ptsp.license c
      LEFT JOIN ptsp.license p ON p.license_id = c.parent_id
     WHERE c.stereotype = 'LICENSE'
     ORDER BY c.name ASC
"""

# Catalogue TTL — the licence roster is near-static, mirror the officer-cache
# 5-min default; override with SIAP_LICENSE_CATALOGUE_TTL_SECONDS.
_CATALOGUE_TTL_SECONDS: float = float(
    os.getenv("SIAP_LICENSE_CATALOGUE_TTL_SECONDS", "600") or "600"
)

# Last-known catalogue rows (list of {license_id, name, sektor}).
_catalogue_cache: list[dict] = []
_catalogue_cache_at: float = 0.0       # monotonic timestamp of last successful load
_catalogue_cache_loaded: bool = False  # has a load ever succeeded?


def _catalogue_is_stale() -> bool:
    return (time.monotonic() - _catalogue_cache_at) > _CATALOGUE_TTL_SECONDS


async def _load_license_catalogue() -> list[dict]:
    """Read the whole LICENSE catalogue from SIAP. Never raises — [] on error.

    Returns a fresh list of {license_id:int, name:str, sektor:str|None}. The
    caller owns caching; this is the raw fetch.
    """
    if not is_siap_db_configured():
        logger.info("get_license_catalogue: SIAP DB not configured")
        return []
    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SQL_LICENSE_CATALOGUE)
        catalogue = [
            {
                "license_id": int(r["license_id"]),
                "name": (r["name"] or "").strip(),
                "sektor": (r["sektor"].strip() if r["sektor"] else None),
            }
            for r in rows
            if r["license_id"] is not None and (r["name"] or "").strip()
        ]
        logger.info("license catalogue loaded | rows=%d", len(catalogue))
        return catalogue
    except Exception:  # pragma: no cover — defensive; never break the resolver
        logger.exception("_load_license_catalogue failed (returning empty)")
        return []


async def get_license_catalogue(*, force_refresh: bool = False) -> list[dict]:
    """
    Return the WHOLE SIAP licence catalogue, cached in memory with a TTL.

    Each entry is {"license_id": int, "name": str, "sektor": str|None}
    (~384 LICENSE rows). The list is the LLM resolver's grounding corpus —
    Gemini picks the best licence id from it, and we validate the model's
    answer against it so a hallucinated id is dropped.

    Caching: a successful fetch is held for `_CATALOGUE_TTL_SECONDS`. While the
    cache is warm we return it directly. When it is stale (or never loaded) we
    re-fetch synchronously — unlike the officer cache we await the refresh,
    because the resolver NEEDS the catalogue to do its job this turn; a stale
    fetch returning [] simply routes the resolver to its fuzzy fallback.

    NEVER raises. On a SIAP outage returns the last-known catalogue if we have
    one (better stale than empty), else [].
    """
    global _catalogue_cache, _catalogue_cache_at, _catalogue_cache_loaded

    if not force_refresh and _catalogue_cache_loaded and not _catalogue_is_stale():
        return _catalogue_cache

    fresh = await _load_license_catalogue()
    if fresh:
        _catalogue_cache = fresh
        _catalogue_cache_at = time.monotonic()
        _catalogue_cache_loaded = True
        return _catalogue_cache

    # Fetch failed/empty. Keep the last-known set if we have one (a SIAP blip
    # shouldn't wipe a good catalogue); otherwise return [].
    if _catalogue_cache_loaded and _catalogue_cache:
        logger.info(
            "get_license_catalogue: refresh empty — serving last-known | rows=%d",
            len(_catalogue_cache),
        )
        return _catalogue_cache
    return []


def _reset_license_catalogue_cache() -> None:
    """Test helper — clear the catalogue cache so each test starts cold."""
    global _catalogue_cache, _catalogue_cache_at, _catalogue_cache_loaded
    _catalogue_cache = []
    _catalogue_cache_at = 0.0
    _catalogue_cache_loaded = False
