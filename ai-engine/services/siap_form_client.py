"""
Form client for SIAP Jateng — the BIMA→SIAP Formulir-Isian seam (Wave 3.6).

`siap_document_client.py` reads/attaches the applicant-upload documents on a
request; `siap_profile_client.py` resolves-or-creates the applicant profile.
This module is the third write seam: the only place in ai-engine that fills a
request's **Formulir Isian** — its text fields AND its file-upload slots — in
one call. SIAP then generates the SK FROM that filled form.

Where the officer copilot's `draft_sk` produces a BIMA-authored *aid* (a PDF +
.docx sent into the chat, never stored in SIAP), THIS seam writes the REAL
`ptsp.form_value` row the officer would otherwise fill by hand on the SIAP edit
page — so the officer only has to VERIFY and click Simpan, and SIAP's own SK
generator has the data it needs.

Endpoint (shipped on Beta-SIAP):

  POST /api/v1/license-request/{id}/form-value
    Upserts the request's `ptsp.form_value` row (form_id + request_id) — fills
    the named TEXT fields and COPIES each named APPLICANT-UPLOAD document into
    the named FLE file slot, exactly as the officer "simpan data form" action.
    Body (application/json):
      {
        "form_id": int,                       # the applicant Formulir Isian form
        "fields": {<field_name>: <string>},   # text fields (optional)
        "files":  {<slot_name>: <file_id>}    # applicant-upload file_id per slot
      }
    Returns: 200 (updated) / 201 (created):
      {"status":"success","data":{"form_value_id": int, "request_id": int,
        "form_id": int, "fields_set": [...], "files_attached": [...]}}
    Guards (all enforced SIAP-side, so a bad payload is a 422 not a silent
    mis-write): form_id must belong to THIS request's licence; every `fields`
    key must be a real NON-FLE field and every `files` key a real FLE slot of
    that form; every file_id must be an ACTIVE APPLICANT-UPLOAD of THIS request.
    Ability required on the token: `form:write`

Auth model:
  A SEPARATE Sanctum bearer from every other SIAP token BIMA holds:
    - SIAP_API_TOKEN          — read-only monitoring-berkas
    - SIAP_WRITE_API_TOKEN    — officer writes (workflow:advance + decision:draft)
    - SIAP_SUBMISSION_API_TOKEN — citizen submission (submission:create)
    - SIAP_DOCUMENT_API_TOKEN — per-request attachments (document:upload/read)
    - SIAP_PROFILE_API_TOKEN  — applicant profile (profile:upsert)
    - SIAP_EVENTS_API_TOKEN   — events:read changes feed
  This one — SIAP_FORM_API_TOKEN — must carry ONLY the `form:write` ability.
  Keeping it distinct is the whole point of SIAP's scoped-ability design: a
  leaked form token can only fill a request's Formulir Isian — it can never
  forward, decide, create a request, or read documents.

  Mint it on Beta-SIAP with:
    php artisan bima:issue-token --user=bima@dpmptsp.go.id \
        --abilities=form:write --name=bima-form

Network model:
  Reuses `SIAP_API_BASE` (same SIAP host as every other SIAP client).
    Local dev:  http://127.0.0.1:8500
    Production: Beta-SIAP during this wave, never production SIAP autonomously.

Failure model — degrade, never raise:
  * If `SIAP_FORM_API_TOKEN` (or `SIAP_API_BASE`) is blank the client is "not
    configured": `upsert_form` short-circuits to a structured
    `{"ok": False, "configured": False, ...}` result with a clear note. The
    officer copilot's `fill_siap_form` tool then tells the officer to fill the
    form by hand on SIAP — the officer turn is never blocked.
  * A timeout, network error, or non-2xx (401/403/404/422) from SIAP returns a
    structured `{"ok": False, ...}` dict. The caller always gets a stable shape.
    We NEVER raise out of a public method — a form-fill hiccup must never crash
    the officer's copilot turn.

PII hygiene:
  The `fields` values are applicant PII (nama, alamat, and document reads). The
  token is redacted in every log line, and the field VALUES are NEVER logged —
  only the field-NAME keys and counts. A SIAP 4xx `message`/`error` body can
  echo a value back, so the raw resp.text is never logged on the write path;
  only the parsed, scrubbed message reaches a log line.

This module performs NO validation or field-sourcing logic of its own — the
no-hallucination extraction lives one layer up (siap_templates.resolve_fill_data
via officer_copilot.fill_siap_form). The client only marshals the HTTP call.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("bima_ai.siap_form")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# SIAP host — shared with every other SIAP client (siap_client.py etc).
_SIAP_API_BASE: str = os.getenv("SIAP_API_BASE", "").rstrip("/")

# DISTINCT from SIAP_API_TOKEN / SIAP_WRITE_API_TOKEN / SIAP_SUBMISSION_API_TOKEN
# / SIAP_DOCUMENT_API_TOKEN / SIAP_PROFILE_API_TOKEN / SIAP_EVENTS_API_TOKEN:
# this token carries ONLY the `form:write` ability. Blank → client disabled.
_SIAP_FORM_API_TOKEN: str = os.getenv("SIAP_FORM_API_TOKEN", "")

# The upsert resolves + validates every slot, copies the slot bytes on the SIAP
# disk, and writes one form_value row inside a DB transaction — a write-side
# call with per-file storage copies. Give it the same headroom as the other
# write clients (document/write).
_SIAP_FORM_TIMEOUT_SECONDS: float = float(
    os.getenv("SIAP_FORM_TIMEOUT_SECONDS", "15")
)


# Any run of 12+ consecutive ASCII digits in a SIAP message is treated as an
# identity number (a 16-digit NIK echoed back in a validation error) and masked
# to its last 4. The `fields` values may carry a NIK, so a 4xx echo must not
# land raw in a log line.
_LONG_DIGIT_RUN = re.compile(r"\d{12,}")


def _mask_digits(s: str) -> str:
    """Mask a long digit run to at most its last 4 digits."""
    s = str(s or "")
    if len(s) <= 4:
        return "*" * len(s)
    return "*" * (len(s) - 4) + s[-4:]


def _scrub_message_for_log(message: str) -> str:
    """Scrub a SIAP error message before it is LOGGED.

    A Laravel 4xx `message`/`error` body can echo an offending field value back
    — and this seam's `fields` values are applicant PII (nama, alamat, a NIK
    read off a document). We can't know every value here, but a NIK is the
    highest-risk echo, so mask any long digit run before the message reaches a
    log line. The user-facing `note` keeps the original SIAP message; only the
    LOG is scrubbed.
    """
    scrubbed = str(message or "")
    if not scrubbed:
        return scrubbed
    return _LONG_DIGIT_RUN.sub(lambda m: _mask_digits(m.group(0)), scrubbed)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SiapFormClient:
    """Async wrapper around SIAP's `POST /api/v1/license-request/{id}/form-value`.

    One instance per process is fine — it holds no per-request state.
    """

    def __init__(
        self,
        base: str = _SIAP_API_BASE,
        token: str = _SIAP_FORM_API_TOKEN,
        timeout: float = _SIAP_FORM_TIMEOUT_SECONDS,
    ) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def is_configured(self) -> bool:
        """True only when both the SIAP host and the form token are set.

        When False, `upsert_form` short-circuits to a structured "not
        configured" result — the officer copilot's `fill_siap_form` tool then
        tells the officer to fill the form by hand instead of crashing.

        A plain method (not a property) for parity with every other SIAP client
        (siap_document_client / siap_profile_client / siap_write_client /
        siap_submission_client / siap_client) — callers invoke it WITH parens.
        """
        return bool(self.base and self.token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _not_configured() -> dict[str, Any]:
        return {
            "ok": False,
            "configured": False,
            "form_value_id": None,
            "fields_set": [],
            "files_attached": [],
            "note": (
                "Integrasi Formulir Isian SIAP belum dikonfigurasi pada "
                "lingkungan ini (SIAP_FORM_API_TOKEN / SIAP_API_BASE belum "
                "diisi)."
            ),
        }

    @staticmethod
    def _explain_http_error(http_status: int, siap_message: str) -> str:
        """Turn a SIAP error status into an Indonesian explanation."""
        suffix = f" Pesan SIAP: {siap_message}" if siap_message else ""
        if http_status == 401:
            return (
                "Token layanan BIMA tidak diterima SIAP — kemungkinan token "
                "formulir sudah kedaluwarsa." + suffix
            )
        if http_status == 403:
            return (
                "Token layanan BIMA tidak memiliki izin untuk mengisi formulir "
                "(ability form:write belum diberikan)." + suffix
            )
        if http_status == 404:
            return "SIAP tidak menemukan permohonan tersebut." + suffix
        if http_status == 422:
            return (
                "Data Formulir Isian tidak valid menurut SIAP (field/slot tidak "
                "dikenali atau berkas bukan unggahan permohonan ini)." + suffix
            )
        return f"SIAP merespon dengan HTTP {http_status}." + suffix

    async def upsert_form(
        self,
        request_id: int,
        form_id: int,
        fields: dict[str, str],
        files: dict[str, int],
    ) -> dict[str, Any]:
        """Fill request `request_id`'s Formulir Isian in SIAP.

        Calls `POST /api/v1/license-request/{id}/form-value` with a JSON body of
        `{form_id, fields, files}`. `fields` maps each TEXT field name to its
        string value; `files` maps each FLE slot name to the applicant-upload
        `file_id` to copy into it. Blank/empty maps are legal (the row is still
        upserted with whatever is supplied).

        Returns a structured dict — NEVER raises:
          done   → {"ok": True, "configured": True, "form_value_id": int|None,
                    "fields_set": [...], "files_attached": [...], "note": "",
                    "http_status": int}
          failed → {"ok": False, "configured": True|False,
                    "form_value_id": None, "fields_set": [], "files_attached": [],
                    "note": str, ...}

        The field VALUES are applicant PII and are NEVER logged — only the
        field-NAME keys, the slot names, and counts. The token is redacted.
        """
        if not self.is_configured():
            return self._not_configured()

        try:
            request_id_int = int(request_id)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "configured": True,
                "form_value_id": None,
                "fields_set": [],
                "files_attached": [],
                "note": "request_id harus berupa angka.",
            }

        try:
            form_id_int = int(form_id)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "configured": True,
                "form_value_id": None,
                "fields_set": [],
                "files_attached": [],
                "note": "form_id harus berupa angka.",
            }

        # Coerce to the shapes SIAP validates: fields → {str: str}, files →
        # {str: int}. Drop blank text values (SIAP trims to "" anyway; sending
        # them just noise) and non-integer file_ids (would 422). No fabrication:
        # the caller already sourced every value from a real source.
        clean_fields: dict[str, str] = {}
        for key, value in (fields or {}).items():
            name = str(key or "").strip()
            if not name:
                continue
            cleaned = "" if value is None else str(value).strip()
            if cleaned:
                clean_fields[name] = cleaned

        clean_files: dict[str, int] = {}
        for slot, file_id in (files or {}).items():
            slot_name = str(slot or "").strip()
            if not slot_name:
                continue
            try:
                clean_files[slot_name] = int(file_id)
            except (TypeError, ValueError):
                # A slot whose file_id isn't an int can't be a real APPLICANT-
                # UPLOAD reference; skip it rather than 422 the whole call.
                logger.warning(
                    "SIAP form upsert: dropping slot with non-int file_id | "
                    "slot=%s", slot_name,
                )

        body: dict[str, Any] = {
            "form_id": form_id_int,
            "fields": clean_fields,
            "files": clean_files,
        }

        url = f"{self.base}/api/v1/license-request/{request_id_int}/form-value"
        # NEVER log the field VALUES (PII). Log the field-NAME keys, slot names,
        # and counts only.
        logger.info(
            "SIAP form upsert | request_id=%s | form_id=%s | field_keys=%s | "
            "file_slots=%s",
            request_id_int, form_id_int,
            sorted(clean_fields.keys()), sorted(clean_files.keys()),
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=self._headers(), json=body)
        except httpx.TimeoutException:
            logger.warning(
                "SIAP form upsert timeout | request_id=%s", request_id_int
            )
            return {
                "ok": False,
                "configured": True,
                "form_value_id": None,
                "fields_set": [],
                "files_attached": [],
                "note": "SIAP tidak merespon tepat waktu saat mengisi Formulir Isian.",
            }
        except httpx.RequestError as exc:
            logger.warning(
                "SIAP form upsert network error | request_id=%s | err=%s",
                request_id_int, exc,
            )
            return {
                "ok": False,
                "configured": True,
                "form_value_id": None,
                "fields_set": [],
                "files_attached": [],
                "note": "Gangguan jaringan saat mengisi Formulir Isian di SIAP.",
            }

        try:
            payload = resp.json()
        except ValueError:
            payload = {}

        if 200 <= resp.status_code < 300:
            data = (
                payload.get("data", payload)
                if isinstance(payload, dict)
                else payload
            )
            form_value_id = None
            fields_set: list[str] = []
            files_attached: list[str] = []
            if isinstance(data, dict):
                form_value_id = data.get("form_value_id") or data.get("id")
                raw_fields = data.get("fields_set")
                raw_files = data.get("files_attached")
                if isinstance(raw_fields, list):
                    fields_set = [str(x) for x in raw_fields]
                if isinstance(raw_files, list):
                    files_attached = [str(x) for x in raw_files]
            logger.info(
                "SIAP form upsert ok | request_id=%s | http=%d | "
                "form_value_id=%s | fields_set=%d | files_attached=%d",
                request_id_int, resp.status_code, form_value_id,
                len(fields_set), len(files_attached),
            )
            return {
                "ok": True,
                "configured": True,
                "http_status": resp.status_code,
                "form_value_id": form_value_id,
                "fields_set": fields_set,
                "files_attached": files_attached,
                "note": "",
            }

        siap_message = ""
        if isinstance(payload, dict):
            siap_message = str(
                payload.get("message") or payload.get("error") or ""
            ).strip()
        # NEVER log the raw resp.text here: a Laravel 422/4xx body can echo an
        # offending field VALUE (applicant PII) back. Log ONLY the status code +
        # the parsed siap_message, and SCRUB even that (a NIK echoed in the
        # message is masked before the line reaches the log).
        logger.warning(
            "SIAP form upsert non-2xx | request_id=%s | http=%d | siap_message=%s",
            request_id_int, resp.status_code,
            _scrub_message_for_log(siap_message),
        )
        return {
            "ok": False,
            "configured": True,
            "http_status": resp.status_code,
            "form_value_id": None,
            "fields_set": [],
            "files_attached": [],
            "note": self._explain_http_error(resp.status_code, siap_message),
        }


# Singleton — built once per process to reuse env values.
_client: Optional[SiapFormClient] = None


def get_siap_form_client() -> SiapFormClient:
    global _client
    if _client is None:
        _client = SiapFormClient()
    return _client
