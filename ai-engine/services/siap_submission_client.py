"""
Submission client for SIAP Jateng — the guided-submission write seam (Wave 3).

`siap_write_client.py` owns the OFFICER-side write endpoints (forward /
decision). This module is its CITIZEN-side counterpart: the only place in
ai-engine that calls SIAP's licence-application create endpoint, used by the
guided-submission flow (`services/guided_submission.py`) when a citizen has
finished filing a permit application over WhatsApp.

  POST /api/v1/license-request
    Creates a new licence-request (permohonan) in SIAP.
    Body: {"license_id": int, "profile_id": int, "description": <optional>}
    Returns: {"request_id": int, "ticket": str, ...}
    Ability required on the token: `submission:create`

Auth model:
  A SEPARATE Sanctum bearer from every other SIAP token BIMA holds:
    - SIAP_API_TOKEN          — read-only monitoring-berkas
    - SIAP_WRITE_API_TOKEN    — officer writes (workflow:advance + decision:draft)
    - SIAP_EVENTS_API_TOKEN   — events:read changes feed
  This one — SIAP_SUBMISSION_API_TOKEN — must carry ONLY the
  `submission:create` ability. Keeping it distinct is the whole point of
  SIAP's scoped-ability design: a leaked token can do exactly one thing,
  and a citizen-submission token can never forward or decide a case.

  Mint it on Beta-SIAP with:
    php artisan bima:issue-token --user=bima@dpmptsp.go.id \
        --abilities=submission:create --name=bima-guided-submission

Network model:
  Reuses `SIAP_API_BASE` (same SIAP host as every other SIAP client).
    Local dev:  http://127.0.0.1:8500
    Production: Beta-SIAP during Wave 3, never production SIAP autonomously.

Failure model — degrade, never raise:
  * If `SIAP_SUBMISSION_API_TOKEN` (or `SIAP_API_BASE`) is blank the client
    is "not configured": every call returns `{"ok": False,
    "configured": False, ...}` with a clear note. The guided-submission
    flow surfaces that to the citizen ("submission channel not ready yet")
    instead of crashing — and the feature flag means this path is OFF until
    the token is provisioned anyway.
  * A timeout, network error, or non-2xx from SIAP returns a structured
    `{"ok": False, ...}` dict. The caller always gets a stable shape it can
    phrase for the citizen. We never raise out of a public method.

This module performs NO state-machine logic and NO validation of its own —
that lives one layer up in `guided_submission.py`. By the time `create_request`
is called the citizen's fields are collected and the BIMA validator has
already passed.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("bima_ai.siap_submission")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# SIAP host — shared with every other SIAP client (siap_client.py etc).
_SIAP_API_BASE: str = os.getenv("SIAP_API_BASE", "").rstrip("/")

# DISTINCT from SIAP_API_TOKEN / SIAP_WRITE_API_TOKEN / SIAP_EVENTS_API_TOKEN:
# this token carries ONLY the `submission:create` ability. Blank → disabled.
_SIAP_SUBMISSION_API_TOKEN: str = os.getenv("SIAP_SUBMISSION_API_TOKEN", "")

# Creating a request runs a DB transaction + ticket allocation on the SIAP
# side — give it the same headroom as the officer write client.
_SIAP_SUBMISSION_TIMEOUT_SECONDS: float = float(
    os.getenv("SIAP_SUBMISSION_TIMEOUT_SECONDS", "15")
)

# SIAP caps the free-text description server-side; mirror it so an over-long
# description is rejected locally with a clear message, not a bounced 422.
_MAX_DESCRIPTION_CHARS = 1000


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SiapSubmissionClient:
    """Async wrapper around SIAP's `POST /api/v1/license-request` endpoint.

    One instance per process is fine — it holds no per-request state.
    """

    def __init__(
        self,
        base: str = _SIAP_API_BASE,
        token: str = _SIAP_SUBMISSION_API_TOKEN,
        timeout: float = _SIAP_SUBMISSION_TIMEOUT_SECONDS,
    ) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def is_configured(self) -> bool:
        """True only when both the SIAP host and the submission token are set.

        When False, `create_request` short-circuits to a structured
        "not configured" result — the guided-submission flow degrades
        gracefully and tells the citizen the channel is not ready yet.
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
            "note": (
                "Integrasi pengajuan SIAP belum dikonfigurasi pada lingkungan "
                "ini (SIAP_SUBMISSION_API_TOKEN / SIAP_API_BASE belum diisi). "
                "Permohonan tidak dikirim."
            ),
        }

    @staticmethod
    def _explain_http_error(http_status: int, siap_message: str) -> str:
        """Turn a SIAP error status into an Indonesian explanation."""
        suffix = f" Pesan SIAP: {siap_message}" if siap_message else ""
        if http_status == 401:
            return (
                "Token layanan BIMA tidak diterima SIAP — kemungkinan token "
                "pengajuan sudah kedaluwarsa." + suffix
            )
        if http_status == 403:
            return (
                "Token layanan BIMA tidak memiliki izin untuk mengajukan "
                "permohonan (ability submission:create belum diberikan)." + suffix
            )
        if http_status == 404:
            return (
                "SIAP tidak menemukan jenis izin atau profil yang dirujuk." + suffix
            )
        if http_status == 422:
            return "Data permohonan tidak valid menurut SIAP." + suffix
        return f"SIAP merespon dengan HTTP {http_status}." + suffix

    async def create_request(
        self,
        license_id: int,
        profile_id: int,
        description: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a new licence-request (permohonan) in SIAP.

        Calls `POST /api/v1/license-request`. `license_id` is the SIAP
        licence the citizen is applying for (resolved via
        `siap_lookup_license`); `profile_id` identifies the applicant's SIAP
        profile; `description` is an optional free-text note (max 1000 chars).

        Returns a structured dict — never raises:
          done   → {"ok": True, "configured": True, "request_id": int,
                    "ticket": str, "data": {...}, "http_status": int}
          failed → {"ok": False, "configured": True|False, "note": str, ...}

        On success the caller surfaces `ticket` to the citizen plus a
        portal.nolongin.com/track/<ticket> deep link.
        """
        if not self.is_configured():
            return self._not_configured()

        try:
            license_id_int = int(license_id)
            profile_id_int = int(profile_id)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "configured": True,
                "note": "license_id dan profile_id harus berupa angka.",
            }

        body: dict[str, Any] = {
            "license_id": license_id_int,
            "profile_id": profile_id_int,
        }
        desc_clean = (description or "").strip()
        if desc_clean:
            if len(desc_clean) > _MAX_DESCRIPTION_CHARS:
                return {
                    "ok": False,
                    "configured": True,
                    "note": (
                        f"Deskripsi terlalu panjang (maks "
                        f"{_MAX_DESCRIPTION_CHARS} karakter)."
                    ),
                }
            body["description"] = desc_clean

        url = f"{self.base}/api/v1/license-request"
        # license_id / profile_id are non-PII identifiers — safe to log.
        logger.info(
            "SIAP submission create | license_id=%s profile_id=%s",
            license_id_int, profile_id_int,
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=self._headers(), json=body)
        except httpx.TimeoutException:
            logger.warning(
                "SIAP submission timeout | license_id=%s", license_id_int
            )
            return {
                "ok": False,
                "configured": True,
                "note": (
                    "SIAP tidak merespon tepat waktu. Permohonan belum tentu "
                    "tersimpan — mohon cek status sebelum mengirim ulang."
                ),
            }
        except httpx.RequestError as exc:
            logger.warning(
                "SIAP submission network error | license_id=%s | err=%s",
                license_id_int, exc,
            )
            return {
                "ok": False,
                "configured": True,
                "note": (
                    "Gangguan jaringan saat menghubungi SIAP. Permohonan "
                    "tidak dikirim."
                ),
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
            request_id = None
            ticket = None
            if isinstance(data, dict):
                request_id = data.get("request_id") or data.get("id")
                ticket = data.get("ticket")
            logger.info(
                "SIAP submission ok | license_id=%s | http=%d | request_id=%s "
                "| ticket=%s",
                license_id_int, resp.status_code, request_id, ticket,
            )
            return {
                "ok": True,
                "configured": True,
                "http_status": resp.status_code,
                "request_id": request_id,
                "ticket": ticket,
                "data": data,
            }

        siap_message = ""
        if isinstance(payload, dict):
            siap_message = str(
                payload.get("message") or payload.get("error") or ""
            ).strip()
        logger.warning(
            "SIAP submission non-2xx | license_id=%s | http=%d | body=%s",
            license_id_int, resp.status_code, resp.text[:300],
        )
        return {
            "ok": False,
            "configured": True,
            "http_status": resp.status_code,
            "note": self._explain_http_error(resp.status_code, siap_message),
        }


# Singleton — built once per process to reuse env values.
_client: Optional[SiapSubmissionClient] = None


def get_siap_submission_client() -> SiapSubmissionClient:
    global _client
    if _client is None:
        _client = SiapSubmissionClient()
    return _client
