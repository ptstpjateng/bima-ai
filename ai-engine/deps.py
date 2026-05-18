"""
FastAPI dependency helpers shared across routers.

For now: just `require_internal_key` for service-to-service auth. Other
existing routers (vectorize, webhooks) are still un-gated — that's a known
gap tracked as Risk #4 in [[BIMA Critique]]. New routers should use this
dep; the old ones get retrofitted in a separate hardening pass.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Annotated, Optional

from fastapi import Header, HTTPException, status

logger = logging.getLogger(__name__)

# Accept either INTERNAL_API_KEY (preferred, matches admin-api convention)
# or the legacy LARAVEL_API_KEY (which has historically held the same
# shared secret). New deployments should set INTERNAL_API_KEY.
_INTERNAL_KEY: str = os.getenv("INTERNAL_API_KEY", "") or os.getenv("LARAVEL_API_KEY", "")


async def require_internal_key(
    x_internal_key: Annotated[Optional[str], Header(alias="X-Internal-Key")] = None,
) -> bool:
    """
    Constant-time check against the shared secret. Mirrors admin-api/app/deps.py
    and the Laravel `hash_equals` pattern so all three services validate the
    same header the same way.
    """
    if not _INTERNAL_KEY:
        logger.error(
            "INTERNAL_API_KEY (or fallback LARAVEL_API_KEY) is not configured — "
            "rejecting all X-Internal-Key gated requests."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service auth not configured.",
        )

    if not x_internal_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Internal-Key header.",
        )

    if not hmac.compare_digest(x_internal_key, _INTERNAL_KEY):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid X-Internal-Key.",
        )

    return True
