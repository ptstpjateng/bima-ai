"""
Liveness + readiness probes.

`/health` is a cheap liveness check: the process is up and middleware works.
Phase 2 will add `/ready` that pings Postgres so docker-compose / k8s can
gate traffic on actual DB reachability.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health", include_in_schema=False, tags=["Health"])
async def health() -> dict[str, str]:
    """Liveness: returns immediately if the FastAPI process is responsive."""
    return {"status": "ok", "service": "admin-api"}
