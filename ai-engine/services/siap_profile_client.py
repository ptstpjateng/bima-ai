"""
Profile client for SIAP Jateng — the applicant-profile resolve/create seam.

`siap_submission_client.py` files a brand-new licence-request; that request must
be bound to an APPLICANT PROFILE (`profile_id`) on the SIAP side — the "Nama
Pemohon" the reviewing officer sees. Before this module, every BIMA guided
submission was bound to a single FIXED profile id (GUIDED_SUBMISSION_PROFILE_ID),
so the applicant name on the SIAP record was always wrong. This module is the
only place in ai-engine that resolves-or-creates the REAL applicant's profile
from the KTP fields BIMA already read during scoring, so each submission carries
the correct pemohon.

  POST /api/v1/person-profile
    Resolve-or-create a person profile keyed by the 16-digit NIK.
    Body: {"identity_number": str (16-digit NIK, required),
           "full_name": str (required), ...optional identity fields}
    Optional fields: address, birth_place, birth_date, gender, phone,
      mobile_phone, email, regency, sub_district, village, nationality,
      identity_type, nip, position_name.
    Returns: 200 (found existing) / 201 (created):
      {"status":"success","data":{"profile_id": int, "created": bool}}
    Ability required on the token: `profile:upsert`

Auth model:
  A SEPARATE Sanctum bearer from every other SIAP token BIMA holds:
    - SIAP_API_TOKEN          — read-only monitoring-berkas
    - SIAP_WRITE_API_TOKEN    — officer writes (workflow:advance + decision:draft)
    - SIAP_SUBMISSION_API_TOKEN — citizen submission (submission:create)
    - SIAP_DOCUMENT_API_TOKEN — per-request attachments (document:upload/read)
    - SIAP_EVENTS_API_TOKEN   — events:read changes feed
  This one — SIAP_PROFILE_API_TOKEN — must carry ONLY the `profile:upsert`
  ability. Keeping it distinct is the whole point of SIAP's scoped-ability
  design: a leaked profile token can only resolve/create a person profile — it
  can never forward, decide, create a request, or read documents.

  Mint it on Beta-SIAP with:
    php artisan bima:issue-token --user=bima@dpmptsp.go.id \
        --abilities=profile:upsert --name=bima-profile

Network model:
  Reuses `SIAP_API_BASE` (same SIAP host as every other SIAP client).
    Local dev:  http://127.0.0.1:8500
    Production: Beta-SIAP during this wave, never production SIAP autonomously.

Failure model — degrade, never raise:
  * If `SIAP_PROFILE_API_TOKEN` (or `SIAP_API_BASE`) is blank the client is
    "not configured": `upsert_profile` short-circuits to a structured
    `{"ok": False, "configured": False, ...}` result with a clear note. The
    guided-submission caller then FALLS BACK to the fixed
    GUIDED_SUBMISSION_PROFILE_ID — the submission still proceeds.
  * A blank/invalid NIK (not exactly 16 digits) is rejected LOCALLY before any
    call, returning `{"ok": False, ...}` — the caller falls back too.
  * A timeout, network error, or non-2xx from SIAP returns a structured
    `{"ok": False, ...}` dict. The caller always gets a stable shape and falls
    back to the fixed profile. We NEVER raise out of a public method — a profile
    hiccup must never block or delay the citizen's submission.

PII hygiene — this is the one SIAP client that handles the raw NIK + full name:
  The applicant's NIK is masked to at most its last 4 digits in every log line,
  and the full name is NEVER logged. The token is redacted in every log line.
  This module performs NO state-machine logic of its own — that lives one layer
  up in `guided_submission.py`.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("bima_ai.siap_profile")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# SIAP host — shared with every other SIAP client (siap_client.py etc).
_SIAP_API_BASE: str = os.getenv("SIAP_API_BASE", "").rstrip("/")

# DISTINCT from SIAP_API_TOKEN / SIAP_WRITE_API_TOKEN / SIAP_SUBMISSION_API_TOKEN
# / SIAP_DOCUMENT_API_TOKEN / SIAP_EVENTS_API_TOKEN: this token carries ONLY the
# `profile:upsert` ability. Blank → client disabled.
_SIAP_PROFILE_API_TOKEN: str = os.getenv("SIAP_PROFILE_API_TOKEN", "")

# Resolve-or-create runs a keyed lookup + optional insert on the SIAP side — a
# fast keyed lookup/insert. This step is awaited on the citizen critical path
# BEFORE create_request (additive to the submission client's own timeout), so
# keep the budget tight: a fast fallback to the fixed profile beats a long stall.
_SIAP_PROFILE_TIMEOUT_SECONDS: float = float(
    os.getenv("SIAP_PROFILE_TIMEOUT_SECONDS", "5")
)

# The optional identity fields SIAP accepts alongside identity_number +
# full_name. Only whitelisted keys are forwarded (an unknown key would bounce a
# 422); a blank/None value is dropped so we never overwrite SIAP with emptiness.
_OPTIONAL_FIELDS: tuple[str, ...] = (
    "address",
    "birth_place",
    "birth_date",
    "gender",
    "phone",
    "mobile_phone",
    "email",
    "regency",
    "sub_district",
    "village",
    "nationality",
    "identity_type",
    "nip",
    "position_name",
)


def _mask_nik(nik: str) -> str:
    """Mask a NIK to at most its last 4 digits for logging (never the full 16)."""
    s = str(nik or "")
    if len(s) <= 4:
        return "*" * len(s)
    return "*" * (len(s) - 4) + s[-4:]


# Any run of 12+ consecutive ASCII digits in a SIAP message is treated as an
# identity number and masked to its last 4 (a 16-digit NIK, or a near-NIK echo).
_LONG_DIGIT_RUN = re.compile(r"\d{12,}")


def _scrub_message_for_log(message: str, *, nik: str = "", name: str = "") -> str:
    """Scrub a SIAP error message before it is LOGGED.

    A Laravel 4xx `message`/`error` field frequently echoes the offending field
    value back — so even the PARSED message (not just the raw resp.text) can
    carry the raw 16-digit NIK or the applicant's full name. This is the one
    client that handles that PII, and its contract is "NIK masked, name never
    logged" — so mask long digit runs and redact the applicant's own NIK/name
    from the message before it reaches a log line. (The user-facing `note`
    keeps the original SIAP message; only the LOG is scrubbed.)
    """
    scrubbed = str(message or "")
    if not scrubbed:
        return scrubbed
    if name:
        scrubbed = scrubbed.replace(name, "[name]")
    if nik:
        scrubbed = scrubbed.replace(nik, _mask_nik(nik))
    # Catch any remaining long digit run (e.g. a NIK the message reformatted).
    scrubbed = _LONG_DIGIT_RUN.sub(lambda m: _mask_nik(m.group(0)), scrubbed)
    return scrubbed


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SiapProfileClient:
    """Async wrapper around SIAP's `POST /api/v1/person-profile` endpoint.

    One instance per process is fine — it holds no per-request state.
    """

    def __init__(
        self,
        base: str = _SIAP_API_BASE,
        token: str = _SIAP_PROFILE_API_TOKEN,
        timeout: float = _SIAP_PROFILE_TIMEOUT_SECONDS,
    ) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def is_configured(self) -> bool:
        """True only when both the SIAP host and the profile token are set.

        When False, `upsert_profile` short-circuits to a structured "not
        configured" result — the guided-submission caller falls back to the
        fixed GUIDED_SUBMISSION_PROFILE_ID instead of crashing.

        A plain method (not a property) for parity with every other SIAP client
        (siap_write_client / siap_submission_client / siap_document_client /
        siap_client) — callers invoke it WITH parens.
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
            "profile_id": None,
            "created": None,
            "note": (
                "Integrasi profil pemohon SIAP belum dikonfigurasi pada "
                "lingkungan ini (SIAP_PROFILE_API_TOKEN / SIAP_API_BASE belum "
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
                "profil sudah kedaluwarsa." + suffix
            )
        if http_status == 403:
            return (
                "Token layanan BIMA tidak memiliki izin untuk profil pemohon "
                "(ability profile:upsert belum diberikan)." + suffix
            )
        if http_status == 404:
            return "SIAP tidak menemukan endpoint profil pemohon." + suffix
        if http_status == 422:
            return "Data profil pemohon tidak valid menurut SIAP." + suffix
        return f"SIAP merespon dengan HTTP {http_status}." + suffix

    async def upsert_profile(
        self,
        *,
        identity_number: str,
        full_name: str,
        **optional_fields: Any,
    ) -> dict[str, Any]:
        """Resolve-or-create the applicant's SIAP person profile by NIK.

        Calls `POST /api/v1/person-profile`. `identity_number` is the applicant's
        16-digit NIK (read from the KTP by Gemini Vision); `full_name` is the
        name printed on the KTP. Only whitelisted `optional_fields` (address,
        birth_date, gender, phone, email, …) are forwarded; blank/None values
        are dropped.

        Returns a structured dict — NEVER raises:
          done   → {"ok": True, "configured": True, "profile_id": int|None,
                    "created": bool|None, "note": "", "http_status": int}
          failed → {"ok": False, "configured": True|False, "profile_id": None,
                    "created": None, "note": str, ...}

        The NIK is validated LOCALLY as exactly 16 digits before any call — a
        blank/garbled read returns ok:false so the caller falls back to the
        fixed profile rather than bouncing a 422. The NIK is masked (last 4 at
        most) and the full name is NEVER logged.
        """
        if not self.is_configured():
            return self._not_configured()

        nik = str(identity_number or "").strip()
        # ASCII-only match: str.isdigit() also accepts non-ASCII/full-width
        # numerals (e.g. "３３…"), which SIAP's `[0-9]{16}` regex 422-rejects —
        # so an ASCII regex is what catches that class locally as intended.
        if re.fullmatch(r"[0-9]{16}", nik) is None:
            # Reject locally — never send a bad NIK to SIAP. NIK masked in log.
            logger.warning(
                "SIAP profile upsert: NIK not exactly 16 digits | nik=%s",
                _mask_nik(nik),
            )
            return {
                "ok": False,
                "configured": True,
                "profile_id": None,
                "created": None,
                "note": "NIK harus tepat 16 digit angka.",
            }

        name = str(full_name or "").strip()
        if not name:
            return {
                "ok": False,
                "configured": True,
                "profile_id": None,
                "created": None,
                "note": "Nama lengkap pemohon wajib diisi.",
            }

        body: dict[str, Any] = {"identity_number": nik, "full_name": name}
        for key in _OPTIONAL_FIELDS:
            if key in optional_fields:
                value = optional_fields[key]
                cleaned = str(value).strip() if value is not None else ""
                if cleaned:
                    body[key] = cleaned

        url = f"{self.base}/api/v1/person-profile"
        # NEVER log the NIK (only masked last-4) or the full name — both are PII.
        logger.info(
            "SIAP profile upsert | nik=%s | fields=%d",
            _mask_nik(nik), len(body),
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=self._headers(), json=body)
        except httpx.TimeoutException:
            logger.warning("SIAP profile upsert timeout | nik=%s", _mask_nik(nik))
            return {
                "ok": False,
                "configured": True,
                "profile_id": None,
                "created": None,
                "note": "SIAP tidak merespon tepat waktu saat memproses profil pemohon.",
            }
        except httpx.RequestError as exc:
            logger.warning(
                "SIAP profile upsert network error | nik=%s | err=%s",
                _mask_nik(nik), exc,
            )
            return {
                "ok": False,
                "configured": True,
                "profile_id": None,
                "created": None,
                "note": "Gangguan jaringan saat memproses profil pemohon di SIAP.",
            }

        try:
            payload = resp.json()
        except ValueError:
            payload = {}

        if 200 <= resp.status_code < 300:
            # SIAP may nest the record under `data` or return it flat.
            data = (
                payload.get("data", payload)
                if isinstance(payload, dict)
                else payload
            )
            profile_id = None
            created = None
            if isinstance(data, dict):
                profile_id = data.get("profile_id") or data.get("id")
                if "created" in data:
                    created = bool(data.get("created"))
            logger.info(
                "SIAP profile upsert ok | nik=%s | http=%d | profile_id=%s | created=%s",
                _mask_nik(nik), resp.status_code, profile_id, created,
            )
            return {
                "ok": True,
                "configured": True,
                "http_status": resp.status_code,
                "profile_id": profile_id,
                "created": created,
                "note": "",
            }

        siap_message = ""
        if isinstance(payload, dict):
            siap_message = str(
                payload.get("message") or payload.get("error") or ""
            ).strip()
        # NEVER log the raw resp.text here: this is the one SIAP client whose
        # request body carries the raw 16-digit NIK + full_name, and a Laravel
        # 422/4xx JSON body frequently echoes the offending field value back —
        # logging it would leak the NIK/name (violating this module's contract).
        # Log ONLY the status code + the parsed siap_message, and SCRUB even that
        # parsed message: the `message`/`error` field itself can echo the raw NIK
        # or the applicant name, so mask them before the line reaches the log.
        logger.warning(
            "SIAP profile upsert non-2xx | nik=%s | http=%d | siap_message=%s",
            _mask_nik(nik), resp.status_code,
            _scrub_message_for_log(siap_message, nik=nik, name=name),
        )
        return {
            "ok": False,
            "configured": True,
            "http_status": resp.status_code,
            "profile_id": None,
            "created": None,
            "note": self._explain_http_error(resp.status_code, siap_message),
        }


# Singleton — built once per process to reuse env values.
_client: Optional[SiapProfileClient] = None


def get_siap_profile_client() -> SiapProfileClient:
    global _client
    if _client is None:
        _client = SiapProfileClient()
    return _client
