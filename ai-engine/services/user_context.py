"""
BIMA-AI – User Context Fetcher

Calls the Laravel backend to get a user's business profile and license vault,
returning a structured string ready for LLM prompt injection.
"""

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("bima_ai.user_context")

_BACKEND_URL: str = os.getenv("LARAVEL_BACKEND_URL", "http://backend:80")
_INTERNAL_KEY: str = os.getenv("LARAVEL_API_KEY", "")


async def fetch_user_context(user_id: str) -> dict[str, Any]:
    """
    Fetch user context from the Laravel API.
    Returns a dict with 'found', 'user', 'active_licenses', 'pending_applications'.
    Never raises; returns {'found': False} on any error.
    """
    if not _INTERNAL_KEY or not user_id.isdigit():
        return {"found": False}

    url = f"{_BACKEND_URL}/api/internal/user-context/{user_id}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                url,
                headers={"X-Internal-Key": _INTERNAL_KEY, "Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {"found": False})
    except Exception:
        logger.exception("Failed to fetch user context | user_id=%s", user_id)
        return {"found": False}


def format_user_context(ctx: dict[str, Any]) -> str:
    """
    Format user context dict into a prompt-ready string.
    Returns empty string if user not found or has no meaningful data.
    """
    if not ctx.get("found"):
        return ""

    user = ctx.get("user", {})
    lines = ["=== PROFIL PENGGUNA ==="]

    if user.get("name"):
        lines.append(f"Nama: {user['name']}")
    if user.get("business_name"):
        lines.append(f"Nama Usaha: {user['business_name']}")
    if user.get("legal_type"):
        lines.append(f"Bentuk Badan Usaha: {user['legal_type']}")
    if user.get("business_scale"):
        scale_map = {"mikro": "Mikro", "kecil": "Kecil", "menengah": "Menengah", "besar": "Besar"}
        lines.append(f"Skala Usaha: {scale_map.get(user['business_scale'], user['business_scale'])}")
    if user.get("kbli_code"):
        lines.append(f"KBLI Utama: {user['kbli_code']} — {user.get('kbli_description', '')}")
    if user.get("city") or user.get("province"):
        loc = ", ".join(filter(None, [user.get("city"), user.get("province")]))
        lines.append(f"Lokasi Usaha: {loc}")
    if user.get("employee_count"):
        lines.append(f"Jumlah Karyawan: {user['employee_count']} orang")

    active = ctx.get("active_licenses", [])
    if active:
        lines.append("\nIzin Aktif / NIB Diterbitkan:")
        for lic in active:
            lines.append(f"  • NIB {lic.get('nib', '—')} | KBLI {lic.get('kbli_code')} — {lic.get('kbli_description', '')}")

    pending = ctx.get("pending_applications", [])
    if pending:
        lines.append("\nPengajuan Sedang Diproses:")
        for app in pending:
            status_map = {
                "submitted": "Diajukan",
                "under_review": "Sedang Ditinjau",
                "additional_docs_required": "Dokumen Tambahan Diperlukan",
            }
            lines.append(
                f"  • {app.get('kbli_code')} — {status_map.get(app.get('status',''), app.get('status',''))}"
            )

    lines.append("=== AKHIR PROFIL ===")
    return "\n".join(lines)
