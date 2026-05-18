"""
Citizen SSO — NIK or NPWP handshake against SIAP Jateng.

This is the demo-grade authentication path for the public portal:
  - Citizen types their identity number on `/login` (16-digit NIK for
    individuals, 15-digit NPWP for businesses — both stored in SIAP's
    `ptsp.person_profile` JSONB)
  - Portal SSR forwards it here (`POST /sso/login`)
  - We auto-detect the type by length, look up against SIAP's DB
  - On hit, we issue a short-lived JWT marked `kind='pemohon'` (NOT admin)
  - Portal stores the JWT in an httpOnly cookie

What this is NOT:
  - Real authentication. A NIK / NPWP is publicly knowable. The post-hackathon
    plan is to layer SMS OTP on top (open item in BIMA Vision §"end-state").
  - A route to admin-api admin endpoints. The 'pemohon' kind is intentionally
    distinct from the 'admin'/'dpmptsp_staff' roles enforced by
    `deps.get_current_admin_user` — citizen tokens cannot reach admin
    routes even if a bug lets them through CORS.

Endpoints:
  POST /sso/login   — body {identity_number} → {access_token, user}
  GET  /sso/me      — bearer → current user (from token claims; no DB read)
  POST /sso/logout  — stateless ack (JWT is bearer-only; client clears cookie)
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import OAuth2PasswordBearer
from jose import ExpiredSignatureError, JWTError, jwt
from pydantic import BaseModel, Field, field_validator

from app.config import Settings, get_settings
from app.db import is_siap_db_configured
from app.deps import create_token_with_claims
from app.services.siap_user_client import (
    IdentityType,
    lookup_pemohon_by_identity,
)

logger = logging.getLogger("bima_admin_api.sso")

router = APIRouter()

# Separate bearer scheme pointed at the SSO login URL so /docs renders a
# distinct OAuth2 flow for citizen tokens vs admin tokens.
_sso_bearer = OAuth2PasswordBearer(tokenUrl="/sso/login", auto_error=False)

# Identity-format regexes. The portal strips any user-typed separators
# (dots, dashes, spaces) before posting so the wire format is just digits.
_NIK_PATTERN = re.compile(r"^\d{16}$")   # KTP (Indonesian National ID)
_NPWP_PATTERN = re.compile(r"^\d{15}$")  # NPWP (business tax ID)


def _detect_identity_type(cleaned: str) -> Optional[IdentityType]:
    """Auto-detect KTP vs NPWP by length. Returns None for invalid lengths."""
    if _NIK_PATTERN.match(cleaned):
        return "KTP"
    if _NPWP_PATTERN.match(cleaned):
        return "NPWP"
    return None


# ---------------------------------------------------------------------------
# Schemas (local — they're SSO-only so not worth a separate schemas/sso.py)
# ---------------------------------------------------------------------------
class SsoLoginRequest(BaseModel):
    # Single generic field — accepts both NIK and NPWP. We detect the type
    # server-side. Field name kept generic so the contract reads cleanly
    # regardless of which type the user actually provides.
    identity_number: str = Field(
        ...,
        description="16-digit NIK (KTP) or 15-digit NPWP. Separators are stripped server-side.",
    )

    @field_validator("identity_number")
    @classmethod
    def _validate_identity(cls, v: str) -> str:
        # Strip any user-typed separators (dots, dashes, spaces) so the wire
        # format is just digits before matching against the length regexes.
        cleaned = re.sub(r"[\s\-.]", "", v)
        if _detect_identity_type(cleaned) is None:
            raise ValueError("Harus 16 digit (NIK) atau 15 digit (NPWP).")
        return cleaned


class SsoUser(BaseModel):
    """Public-shaped citizen identity. Carried in JWT claims and /me responses."""

    profile_id: int
    # Generic pair — type tells the UI which label to render ("NIK" or "NPWP").
    identity_number: str
    identity_type: IdentityType
    name: str
    mobile: Optional[str] = None
    kabupaten: Optional[str] = None
    kind: str = "pemohon"


class SsoLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: SsoUser


class SsoMeResponse(BaseModel):
    user: SsoUser
    # Placeholder list — Phase 2.5 will populate via SIAP monitoring-berkas.
    # Kept in the contract now so the portal doesn't need a schema bump later.
    active_permits: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# POST /sso/login
# ---------------------------------------------------------------------------
@router.post(
    "/login",
    response_model=SsoLoginResponse,
    responses={
        404: {"description": "NIK not registered in SIAP Jateng."},
        503: {"description": "SIAP integration not configured on this environment."},
    },
    summary="NIK-only SSO login for citizen portal",
)
async def sso_login(
    payload: SsoLoginRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> SsoLoginResponse:
    """
    Look up the NIK in SIAP and issue a JWT.

    We do NOT verify any second factor here — see module docstring. The
    timing characteristics intentionally leak whether a NIK exists; that is
    not a credentials oracle (the NIK itself is the credential), but we
    note it so post-hackathon auditing flags it for redesign.
    """
    if not is_siap_db_configured():
        # Clean 503 rather than a 500 from RuntimeError. The portal renders
        # a friendly "tracking sementara tidak tersedia" message on this.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SSO SIAP belum dikonfigurasi pada lingkungan ini.",
        )

    # Identity type is implied by length (15=NPWP, 16=KTP). The Pydantic
    # validator above guarantees one of the two; this assertion is for the
    # type checker.
    identity_type = _detect_identity_type(payload.identity_number)
    assert identity_type is not None  # validator would have raised otherwise

    person = await lookup_pemohon_by_identity(
        identity_number=payload.identity_number,
        identity_type=identity_type,
    )
    if person is None:
        # Indonesian copy — this string surfaces directly to the portal user.
        label = "NIK" if identity_type == "KTP" else "NPWP"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{label} tidak terdaftar di SIAP Jateng.",
        )

    user = SsoUser(
        profile_id=person["profile_id"],
        identity_number=payload.identity_number,
        identity_type=identity_type,
        name=person["full_name"] or "—",
        mobile=person["mobile"],
        kabupaten=person["kab"],
        kind="pemohon",
    )

    token = create_token_with_claims(
        subject=user.profile_id,
        extra_claims={
            "identity_number": user.identity_number,
            "identity_type": user.identity_type,
            "name": user.name,
            "kind": user.kind,
        },
        settings=settings,
    )

    logger.info(
        "sso_login_ok | profile_id=%s | type=%s | id=***%s",
        user.profile_id,
        user.identity_type,
        user.identity_number[-4:],
    )

    return SsoLoginResponse(access_token=token, user=user)


# ---------------------------------------------------------------------------
# Citizen-token decode dependency
#
# Mirrors `deps.get_current_admin_user` but for the 'pemohon' kind. Kept
# inline here (not in deps.py) so the SSO module is the single owner of
# citizen-auth semantics; if a future endpoint needs it, lift it out.
# ---------------------------------------------------------------------------
async def _get_current_pemohon(
    token: Annotated[str | None, Depends(_sso_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SsoUser:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from None
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from None

    # The 'kind' guard prevents an admin JWT from being passed to citizen
    # endpoints (or vice versa). Same key, same algorithm — different
    # audience by claim, not by signing key.
    if payload.get("kind") != "pemohon":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Citizen token required.",
        )

    sub = payload.get("sub")
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject.",
        )

    try:
        profile_id = int(sub)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token subject malformed.",
        ) from None

    # Read identity claims with backward-compatible fallback for older JWTs
    # that may still carry the legacy `nik` claim instead of the new
    # `identity_number` + `identity_type` pair. Once all citizen sessions
    # have rotated through a fresh login, the fallback can be removed.
    identity_type_claim = payload.get("identity_type", "KTP")
    if identity_type_claim not in ("KTP", "NPWP"):
        identity_type_claim = "KTP"

    return SsoUser(
        profile_id=profile_id,
        identity_number=str(
            payload.get("identity_number") or payload.get("nik") or ""
        ),
        identity_type=identity_type_claim,
        name=str(payload.get("name", "")),
        kind="pemohon",
    )


# ---------------------------------------------------------------------------
# GET /sso/me
# ---------------------------------------------------------------------------
@router.get(
    "/me",
    response_model=SsoMeResponse,
    summary="Current citizen identity from JWT claims",
)
async def sso_me(
    current: Annotated[SsoUser, Depends(_get_current_pemohon)],
) -> SsoMeResponse:
    """
    Return the bearer's identity plus an (empty for now) permit list.

    Per the brief: the permit list is a Phase 2.5 feature. We expose the
    contract today with `active_permits: []` so the portal doesn't need a
    schema migration when SIAP's monitoring-berkas integration lands.
    """
    return SsoMeResponse(user=current, active_permits=[])


# ---------------------------------------------------------------------------
# POST /sso/logout
# ---------------------------------------------------------------------------
@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Best-effort logout (stateless)",
)
async def sso_logout() -> Response:
    """
    No-op server side — the JWT is bearer-only, so logging out means the
    client clearing its httpOnly cookie. We keep the endpoint so the portal
    can centralize logout traffic for future server-side session tracking
    (e.g. a JWT denylist when we introduce SMS OTP).

    Returns an explicit `Response(status_code=204)` with no body. The
    `-> None` annotation that originally existed here caused FastAPI's
    strict 204 body-allowed check to fail at app import time
    (AssertionError: Status code 204 must not have a response body).
    """
    return Response(status_code=status.HTTP_204_NO_CONTENT)
