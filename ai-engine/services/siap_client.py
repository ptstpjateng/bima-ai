"""
Read-only client for SIAP Jateng (DPMPTSP Central Java licensing system).

BIMA-AI calls this when a citizen WhatsApps a permit-status question. The
client wraps SIAP's existing Sanctum-protected v1 API and never writes
anything — read-only by design for Phase 1.

Phase 1 surface (single endpoint):
  GET /api/v1/monitoring-berkas?search={ticket}
    → ticket lookup against view ptsp.vi_monitoring_berkas_v3

Auth model:
  SIAP issues one long-lived Sanctum bearer to BIMA's service account
  (POST /api/v1/login {email, password, device_name}). The plain-text
  token lands in ai-engine/.env as SIAP_API_TOKEN. Token has no expiry
  by default — rotate manually for now (see vault Decisions §17 for the
  ask to SIAP team to scope abilities + expiration).

Network model:
  Local dev:   SIAP_API_BASE=http://127.0.0.1:8500  (local Laravel server)
  Production:  SIAP_API_BASE=https://perizinan.jatengprov.go.id

Failure model:
  Any non-200 or network error returns None. Calling code falls back to
  Gemma. We do NOT raise — the WhatsApp user should always get *some*
  reply, even if SIAP is down.
"""

import logging
import os
import re
from datetime import datetime
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_SIAP_API_BASE: str = os.getenv("SIAP_API_BASE", "").rstrip("/")
_SIAP_API_TOKEN: str = os.getenv("SIAP_API_TOKEN", "")
_SIAP_TIMEOUT_SECONDS: float = float(os.getenv("SIAP_TIMEOUT_SECONDS", "8"))
_PORTAL_TRACK_URL_BASE: str = os.getenv(
    "PORTAL_TRACK_URL_BASE",
    "https://portal.nolongin.com/track",
).rstrip("/")


# ---------------------------------------------------------------------------
# Ticket extraction
# ---------------------------------------------------------------------------

# SIAP ticket numbers are 9-digit zero-padded (e.g. "000077591"). A citizen
# may type any of: bare digits, "tiket 77591", "status izin 000077591",
# "lacak 77591", "cek izin saya nomor 77591". We accept all of these via
# three tiers (most-specific first; falls through to looser tiers).
_TICKET_KEYWORD_PATTERN = re.compile(
    r"(?:status|lacak|cek|track|tiket|izin\s+saya|nomor\s+izin|nomer\s+izin|no\.\s*izin)"
    r"[\s:.]*0*(\d{4,9})\b",
    re.IGNORECASE,
)
# Tier 2: a bare 9-digit number anywhere — only matches the SIAP-format length.
_BARE_9DIGIT_PATTERN = re.compile(r"\b(\d{9})\b")
# Tier 3 (looser): for messages that mention izin/tiket/permohonan AND contain
# any 4-9 digit number, treat the digit run as the ticket. `\b...\b` ensures
# we don't match "izin" inside "izinkan".
_HAS_PERMIT_CONTEXT_PATTERN = re.compile(r"\b(izin|tiket|permohonan)\b", re.IGNORECASE)
_ANY_4_9_DIGIT_PATTERN = re.compile(r"\b(\d{4,9})\b")


def extract_ticket(message: str) -> Optional[str]:
    """
    Extract a SIAP ticket number from a user WhatsApp message.

    Three-tier match:
      1. explicit keyword adjacent to the number (e.g. "status 77591")
      2. bare 9-digit number anywhere (e.g. "000077591")
      3. message mentions izin/tiket/permohonan AND has a 4-9 digit number
         (e.g. "cek tiket saya 77591")

    Returns the zero-padded 9-digit string, or None if no pattern matches.
    """
    msg = message.strip()

    m = _TICKET_KEYWORD_PATTERN.search(msg)
    if m:
        return m.group(1).zfill(9)

    m = _BARE_9DIGIT_PATTERN.search(msg)
    if m:
        return m.group(1)

    if _HAS_PERMIT_CONTEXT_PATTERN.search(msg):
        m = _ANY_4_9_DIGIT_PATTERN.search(msg)
        if m:
            return m.group(1).zfill(9)

    return None


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class SiapClient:
    """Thin async wrapper around SIAP's Sanctum-protected v1 API."""

    def __init__(
        self,
        base: str = _SIAP_API_BASE,
        token: str = _SIAP_API_TOKEN,
        timeout: float = _SIAP_TIMEOUT_SECONDS,
    ) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def is_configured(self) -> bool:
        return bool(self.base and self.token)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

    async def get_status_by_ticket(self, ticket: str) -> Optional[dict]:
        """
        Fetch one permit application's current status by ticket number.

        Returns the first matching record dict on success, None on miss or
        any error. Caller handles the None case (e.g. fall back to Gemma
        or send a generic "tiket tidak ditemukan" reply).

        Response schema (verified against local SIAP 2026-05-18):
          {
            "no_tiket": "000077591",
            "nama_perizinan": "Perpanjangan Izin Pemakaian Tanah/Bangunan Pengairan",
            "nama_bidang": "Pekerjaan Umum Sumber Daya Air dan Penataan Ruang",
            "nama_pemohon": "FAJAR JOKO WASKITO",
            "posisi_berkas": "Front Office PTSP",
            "tanggal_permohonan": "2026-05-17 18:40:06",
            "status_permohonan": "VERIFIKASI"
          }
        """
        if not self.is_configured():
            logger.warning(
                "SIAP client not configured (SIAP_API_BASE or SIAP_API_TOKEN missing)"
            )
            return None

        ticket = ticket.zfill(9)
        url = f"{self.base}/api/v1/monitoring-berkas"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    url,
                    headers=self._headers(),
                    params={"search": ticket},
                )
        except httpx.TimeoutException:
            logger.warning("SIAP timeout for ticket=%s", ticket)
            return None
        except httpx.RequestError as e:
            logger.warning("SIAP network error for ticket=%s: %s", ticket, e)
            return None

        if resp.status_code != 200:
            logger.warning(
                "SIAP non-200 for ticket=%s: HTTP %d body=%s",
                ticket,
                resp.status_code,
                resp.text[:200],
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


# Singleton — instantiate once per process to reuse env values.
_client: Optional[SiapClient] = None


def get_siap_client() -> SiapClient:
    global _client
    if _client is None:
        _client = SiapClient()
    return _client


# ---------------------------------------------------------------------------
# WhatsApp reply formatting
# ---------------------------------------------------------------------------

# Indonesian month names for the friendly date rendering.
_BULAN_ID = [
    "", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
]


def _format_date_id(raw: str) -> str:
    """Convert '2026-05-17 18:40:06' (or ISO 8601) into '17 Mei 2026'."""
    if not raw:
        return ""
    try:
        # Accept both 'YYYY-MM-DD HH:MM:SS' and ISO 8601 with Z/offset.
        normalized = raw.replace("Z", "+00:00")
        if "T" not in normalized and " " in normalized:
            normalized = normalized.replace(" ", "T", 1)
        d = datetime.fromisoformat(normalized)
        return f"{d.day} {_BULAN_ID[d.month]} {d.year}"
    except (ValueError, TypeError, IndexError):
        return raw


def _format_name(raw: str) -> str:
    """Convert 'FAJAR JOKO WASKITO' → 'Fajar Joko Waskito'."""
    if not raw:
        return ""
    return raw.title()


def _mask_name(raw: str) -> str:
    """Privacy-preserving applicant name for an UNAUTHENTICATED ticket lookup.

    SIAP tickets are sequential 9-digit numbers, so anyone can enumerate them
    over the public WhatsApp line. Returning the full real name lets an attacker
    harvest names at scale (and link them to NIKs elsewhere). We keep the given
    name + the last-name initial so the owner still recognises their own
    application, but the full surname never leaves the system.
    'FAJAR JOKO WASKITO' → 'Fajar W.'  ·  'BUDI' → 'Budi'  ·  '' → ''
    """
    parts = [p for p in (raw or "").split() if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].title()
    return f"{parts[0].title()} {parts[-1][0].upper()}."


def format_status_reply(record: dict) -> str:
    """
    Render a SIAP monitoring-berkas record into a WhatsApp-friendly Indonesian
    message. Pure data-driven — no Gemma call, so no hallucination risk on the
    permit details.

    Includes a deep-link to portal.nolongin.com/track/{ticket} for citizens
    who want a richer view.
    """
    ticket = record.get("no_tiket", "??")
    nama_pemohon = _mask_name(record.get("nama_pemohon", ""))
    nama_perizinan = record.get("nama_perizinan", "permohonan Anda")
    nama_bidang = record.get("nama_bidang", "")
    posisi_berkas = record.get("posisi_berkas", "—")
    status_permohonan = record.get("status_permohonan", "—")
    tanggal_permohonan = _format_date_id(record.get("tanggal_permohonan", ""))

    parts = []
    if nama_pemohon:
        parts.append(f"Halo {nama_pemohon} 👋")
        parts.append("")

    parts.append(f"Permohonan *{nama_perizinan}*")
    if nama_bidang:
        parts.append(f"_(Bidang: {nama_bidang})_")
    parts.append(f"Tiket: `{ticket}`")
    parts.append("")
    parts.append(f"📍 Saat ini di: *{posisi_berkas}*")
    parts.append(f"📋 Status: *{status_permohonan}*")
    if tanggal_permohonan:
        parts.append(f"📅 Didaftarkan: {tanggal_permohonan}")
    parts.append("")
    parts.append(f"🔗 Lihat detail di portal: {_PORTAL_TRACK_URL_BASE}/{ticket}")

    return "\n".join(parts)


def format_not_found_reply(ticket: str) -> str:
    """Friendly reply when the ticket lookup returns nothing."""
    return (
        f"Maaf, saya tidak menemukan permohonan dengan tiket `{ticket}`. 🤔\n\n"
        "Pastikan nomor tiketnya benar (9 digit, biasanya tercetak di tanda terima "
        "saat Anda mendaftar). Kalau masih bermasalah, silakan hubungi DPMPTSP "
        "Jateng atau cek di portal: " + _PORTAL_TRACK_URL_BASE + "/" + ticket
    )


def format_service_down_reply(ticket: str) -> str:
    """Friendly reply when SIAP is unreachable — degrade gracefully."""
    return (
        f"Maaf, sistem informasi SIAP Jateng sedang tidak dapat dihubungi "
        f"untuk pengecekan tiket `{ticket}`. 😔\n\n"
        "Coba lagi dalam beberapa menit, atau buka langsung di portal: "
        + _PORTAL_TRACK_URL_BASE + "/" + ticket
    )
