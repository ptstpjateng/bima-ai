"""
Dashboard summary schemas.

The single payload powers four homepage tiles + a "last updated" stamp on the
admin dashboard. Folded into one response on purpose — the frontend mounts the
dashboard once and we'd rather pay 1 round-trip than 5.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DashboardStats(BaseModel):
    """Single payload powering the admin dashboard tiles."""

    model_config = ConfigDict(from_attributes=True)

    umkm_users: int
    whatsapp_messages_today: int
    active_kbli_codes: int
    pending_ingestions: int
    # None when ai-engine /health was unreachable within the 2s budget — the
    # frontend renders this as a "—" rather than a hard error so the rest of
    # the dashboard keeps working when ai-engine flaps.
    chroma_chunks: int | None
    last_updated: datetime
