"""
Write client for SIAP Jateng — the BIMA↔SIAP write seam (Wave 2).

`siap_client.py` is read-only by design (Phase 1). This module is its
deliberate counterpart: the ONLY place in ai-engine that calls SIAP's two
state-changing endpoints, both shipped on Beta-SIAP in Wave 1:

  POST /api/v1/license-request/{id}/forward
    Advances an in-flight request to the next approval desk.
    Body: {"note": <optional, max 1000 chars>}
    Ability required on the token: `workflow:advance`

  POST /api/v1/license-request/{id}/decision
    Records an officer's approve/reject decision against a request. A
    `rejected` decision routes the request back to the previous desk.
    Body: {"decision": "approved"|"rejected", "notes": <required, max 1000>}
    Ability required on the token: `decision:draft`

Auth model:
  A SEPARATE Sanctum bearer from `SIAP_API_TOKEN` (which is read-only) and
  from `SIAP_EVENTS_API_TOKEN` (events:read only). This token must carry
  the `workflow:advance` + `decision:draft` abilities and is minted on
  Beta-SIAP with `php artisan bima:issue-token`. It lands in
  ai-engine/.env as `SIAP_WRITE_API_TOKEN`.

  Keeping the write token distinct from the read token is the whole point
  of SIAP's scoped-ability design (write-seam doc §3): a leaked read token
  cannot forward or decide a case.

Network model:
  Reuses `SIAP_API_BASE` (same SIAP host as the read client).
    Local dev:  http://127.0.0.1:8500
    Production: https://perizinan.jatengprov.go.id (Beta during Wave 2)

Failure model — degrade, never raise:
  * If `SIAP_WRITE_API_TOKEN` (or `SIAP_API_BASE`) is blank the client is
    "not configured": every call returns `{"ok": False, "configured": False,
    ...}` with a clear note. The officer copilot surfaces that to the
    officer instead of crashing.
  * A timeout, network error, or non-2xx from SIAP returns a structured
    `{"ok": False, ...}` dict — the caller (the copilot write tools) always
    gets a stable shape it can phrase for the officer. We never raise out
    of a public method.

This module performs NO confirmation logic of its own. The officer-in-the-
loop guard lives one layer up, in `officer_copilot.py` — by the time a
method here is called the officer has already confirmed.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("bima_ai.siap_write")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# SIAP host — shared with the read client (siap_client.py).
_SIAP_API_BASE: str = os.getenv("SIAP_API_BASE", "").rstrip("/")

# DISTINCT from SIAP_API_TOKEN: this token carries the write abilities
# (workflow:advance + decision:draft). Blank → client disabled.
_SIAP_WRITE_API_TOKEN: str = os.getenv("SIAP_WRITE_API_TOKEN", "")

# Writes can run a DB transaction on the SIAP side, so allow a touch more
# headroom than the 8 s read timeout — but still bounded.
_SIAP_WRITE_TIMEOUT_SECONDS: float = float(
    os.getenv("SIAP_WRITE_TIMEOUT_SECONDS", "15")
)

# SIAP enforces these caps server-side; we mirror them so a too-long note is
# rejected locally with a clear message rather than bouncing off a 422.
_MAX_NOTE_CHARS = 1000

_VALID_DECISIONS = ("approved", "rejected")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SiapWriteClient:
    """Async wrapper around SIAP's two Sanctum-protected write endpoints.

    One instance per process is fine — it holds no per-request state.
    """

    def __init__(
        self,
        base: str = _SIAP_API_BASE,
        token: str = _SIAP_WRITE_API_TOKEN,
        timeout: float = _SIAP_WRITE_TIMEOUT_SECONDS,
    ) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def is_configured(self) -> bool:
        """True only when both the SIAP host and the write token are set.

        When False every write method short-circuits to a structured
        "not configured" result — the copilot degrades gracefully.
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
                "Integrasi tulis SIAP belum dikonfigurasi pada lingkungan ini "
                "(SIAP_WRITE_API_TOKEN / SIAP_API_BASE belum diisi). Tindakan "
                "tidak dijalankan."
            ),
        }

    async def _post(
        self,
        path: str,
        json_body: dict[str, Any],
        *,
        log_label: str,
        log_request_id: int,
    ) -> dict[str, Any]:
        """Run one POST against SIAP, mapping every outcome to a stable dict.

        Returns `{"ok": True, "configured": True, "data": <SIAP data>,
        "http_status": int}` on a 2xx, and `{"ok": False, ...}` with a note
        on every other outcome. Never raises.
        """
        url = f"{self.base}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    url, headers=self._headers(), json=json_body
                )
        except httpx.TimeoutException:
            logger.warning(
                "SIAP write timeout | op=%s | request_id=%s", log_label, log_request_id
            )
            return {
                "ok": False,
                "configured": True,
                "note": (
                    "SIAP tidak merespon tepat waktu. Tindakan belum tentu "
                    "tersimpan — mohon periksa status berkas sebelum mengulang."
                ),
            }
        except httpx.RequestError as exc:
            logger.warning(
                "SIAP write network error | op=%s | request_id=%s | err=%s",
                log_label, log_request_id, exc,
            )
            return {
                "ok": False,
                "configured": True,
                "note": "Gangguan jaringan saat menghubungi SIAP. Tindakan tidak dijalankan.",
            }

        # Parse the body once; SIAP always returns JSON for these endpoints.
        try:
            payload = resp.json()
        except ValueError:
            payload = {}

        if 200 <= resp.status_code < 300:
            logger.info(
                "SIAP write ok | op=%s | request_id=%s | http=%d",
                log_label, log_request_id, resp.status_code,
            )
            return {
                "ok": True,
                "configured": True,
                "http_status": resp.status_code,
                "data": payload.get("data", payload) if isinstance(payload, dict) else payload,
            }

        # Non-2xx: surface SIAP's own message so the officer knows why.
        siap_message = ""
        if isinstance(payload, dict):
            siap_message = str(
                payload.get("message") or payload.get("error") or ""
            ).strip()
        logger.warning(
            "SIAP write non-2xx | op=%s | request_id=%s | http=%d | body=%s",
            log_label, log_request_id, resp.status_code, resp.text[:300],
        )
        note = self._explain_http_error(resp.status_code, siap_message)
        return {
            "ok": False,
            "configured": True,
            "http_status": resp.status_code,
            "note": note,
        }

    @staticmethod
    def _explain_http_error(http_status: int, siap_message: str) -> str:
        """Turn a SIAP error status into an Indonesian explanation for the officer."""
        suffix = f" Pesan SIAP: {siap_message}" if siap_message else ""
        if http_status == 403:
            return (
                "Token layanan BIMA tidak memiliki izin untuk tindakan ini "
                "(ability workflow:advance / decision:draft belum diberikan)." + suffix
            )
        if http_status == 404:
            return "SIAP tidak menemukan permohonan dengan request_id tersebut." + suffix
        if http_status == 409:
            return (
                "SIAP menolak tindakan ini karena kondisi alur kerja: misalnya "
                "berkas sudah di tahap akhir, atau tahap berikutnya bercabang "
                "sehingga harus dipilih petugas secara manual." + suffix
            )
        if http_status == 422:
            return "Data yang dikirim tidak valid menurut SIAP." + suffix
        return f"SIAP merespon dengan HTTP {http_status}." + suffix

    # -- public write methods -------------------------------------------------

    async def forward_request(
        self,
        request_id: int,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        """Advance request `request_id` to the next approval desk.

        Calls `POST /api/v1/license-request/{id}/forward`. `note` is an
        optional officer note (max 1000 chars) recorded in `ptsp.license_log`
        so the next desk can read why the file was forwarded.

        Returns a structured dict — `{"ok": True, "data": {...}}` on success
        (SIAP's data carries `previous_step_id`, `approval_step_id`,
        `approval_step_status`, `status`), `{"ok": False, "note": ...}`
        otherwise. Never raises.
        """
        if not self.is_configured():
            return self._not_configured()

        note_clean = (note or "").strip()
        if len(note_clean) > _MAX_NOTE_CHARS:
            return {
                "ok": False,
                "configured": True,
                "note": f"Catatan terlalu panjang (maks {_MAX_NOTE_CHARS} karakter).",
            }

        body: dict[str, Any] = {}
        if note_clean:
            body["note"] = note_clean

        return await self._post(
            f"/api/v1/license-request/{int(request_id)}/forward",
            body,
            log_label="forward",
            log_request_id=int(request_id),
        )

    async def record_decision(
        self,
        request_id: int,
        decision: str,
        notes: str,
    ) -> dict[str, Any]:
        """Record an officer's approve/reject decision against `request_id`.

        Calls `POST /api/v1/license-request/{id}/decision`. `decision` must be
        `"approved"` or `"rejected"`; `notes` is REQUIRED (SIAP rejects an
        empty note) and capped at 1000 chars. A `rejected` decision routes the
        request back to the previous desk on the SIAP side.

        Returns a structured dict — `{"ok": True, "data": {...}}` on success
        (SIAP's data carries `decision`, `status`, `approval_step_id`,
        `routed_back`), `{"ok": False, "note": ...}` otherwise. Never raises.
        """
        if not self.is_configured():
            return self._not_configured()

        decision_clean = (decision or "").strip().lower()
        if decision_clean not in _VALID_DECISIONS:
            return {
                "ok": False,
                "configured": True,
                "note": (
                    "Keputusan tidak valid — harus 'approved' (terima) atau "
                    "'rejected' (tolak)."
                ),
            }

        notes_clean = (notes or "").strip()
        if not notes_clean:
            return {
                "ok": False,
                "configured": True,
                "note": "Catatan keputusan wajib diisi — SIAP menolak keputusan tanpa alasan.",
            }
        if len(notes_clean) > _MAX_NOTE_CHARS:
            return {
                "ok": False,
                "configured": True,
                "note": f"Catatan terlalu panjang (maks {_MAX_NOTE_CHARS} karakter).",
            }

        return await self._post(
            f"/api/v1/license-request/{int(request_id)}/decision",
            {"decision": decision_clean, "notes": notes_clean},
            log_label="decision",
            log_request_id=int(request_id),
        )


# Singleton — built once per process to reuse env values.
_client: Optional[SiapWriteClient] = None


def get_siap_write_client() -> SiapWriteClient:
    global _client
    if _client is None:
        _client = SiapWriteClient()
    return _client
