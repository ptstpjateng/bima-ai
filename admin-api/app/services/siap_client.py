"""
Minimal SIAP Jateng client used by the portal-facing /tracking router.

Why a duplicate (vs sharing ai-engine's siap_client)?
  Each BIMA service owns its own config (see DATABASE_URL pattern). Pulling
  SIAP into a shared library would create cross-service deploy coupling for a
  ~100 LoC HTTP wrapper. The two implementations stay narrow and the test
  surface stays per-service.

This client is intentionally a subset of ai-engine/services/siap_client.py:
  - no WhatsApp formatters (admin-api returns raw JSON to the portal)
  - no ticket-from-message extraction (the portal route already has the ticket)
  - returns the full SIAP record plus light normalization for the UI
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger("bima_admin_api.siap_client")


class SiapTrackingClient:
    """Async HTTP client targeting SIAP's /api/v1/monitoring-berkas endpoint."""

    def __init__(
        self,
        base: Optional[str] = None,
        token: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        settings = get_settings()
        self.base = (base if base is not None else settings.SIAP_API_BASE).rstrip("/")
        self.token = token if token is not None else settings.SIAP_API_TOKEN
        self.timeout = timeout if timeout is not None else settings.SIAP_TIMEOUT_SECONDS

    def is_configured(self) -> bool:
        return bool(self.base and self.token)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

    async def get_status_by_ticket(self, ticket: str) -> Optional[dict]:
        """
        Fetch a single permit by ticket. Returns the raw SIAP record or None
        (not configured, network failure, non-200, or empty data).

        SIAP response schema (verified 2026-05-18):
          { "no_tiket", "nama_perizinan", "nama_bidang", "nama_pemohon",
            "posisi_berkas", "tanggal_permohonan", "status_permohonan" }
        """
        if not self.is_configured():
            logger.warning(
                "SIAP client not configured (SIAP_API_BASE or SIAP_API_TOKEN missing)"
            )
            return None

        url = f"{self.base}/api/v1/monitoring-berkas"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    url,
                    headers=self._headers(),
                    params={"search": ticket.zfill(9)},
                )
        except httpx.TimeoutException:
            logger.warning("SIAP timeout for ticket=%s", ticket)
            return None
        except httpx.RequestError as exc:
            logger.warning("SIAP network error for ticket=%s: %s", ticket, exc)
            return None

        if resp.status_code != 200:
            logger.warning(
                "SIAP non-200 for ticket=%s: HTTP %d",
                ticket,
                resp.status_code,
            )
            return None

        try:
            payload = resp.json()
        except ValueError:
            logger.warning("SIAP returned non-JSON for ticket=%s", ticket)
            return None

        data = payload.get("data", [])
        if not data:
            return None
        return data[0]


_client: Optional[SiapTrackingClient] = None


def get_siap_tracking_client() -> SiapTrackingClient:
    """Singleton accessor — instantiate once per process."""
    global _client
    if _client is None:
        _client = SiapTrackingClient()
    return _client
