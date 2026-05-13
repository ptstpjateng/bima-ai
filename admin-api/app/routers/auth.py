"""
Auth router — Phase 1.

Endpoints:
    POST /auth/login   — email + password → JWT (admin/dpmptsp_staff only)
    GET  /auth/me      — bearer token → current user payload

`/auth/me` isn't strictly required for Phase 1 but it's three lines and the
frontend will need it the moment the login form ships. Cheaper to add now.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.deps import create_access_token, get_current_admin_user, get_db
from app.models import User
from app.schemas.auth import LoginRequest, TokenResponse, UserOut

router = APIRouter(prefix="/auth", tags=["Auth"])

# Bcrypt context — wire-compatible with Laravel's `'password' => 'hashed'`
# cast which uses BCRYPT_DEFAULT_ROUNDS=12 by default. passlib auto-detects
# the cost factor from the stored hash, so rounds mismatches don't matter.
#
# IMPORTANT: this only works AFTER Sprint A's double-hash bug fix has landed
# in Laravel. Pre-fix passwords are bcrypted twice and unverifiable here.
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Roles allowed to authenticate. Mirror of deps._ADMIN_ROLES — duplicated
# intentionally so a future tweak to login policy doesn't accidentally widen
# the post-auth dependency.
_ADMIN_ROLES: frozenset[str] = frozenset({"admin", "dpmptsp_staff"})


def _verify_password(plain: str, hashed: str) -> bool:
    """passlib wrapper. Catches malformed-hash errors as a verify failure."""
    try:
        return _pwd_context.verify(plain, hashed)
    except (ValueError, TypeError):
        # passlib raises on bad hash format. Treat as auth failure rather
        # than 500 — if a row in `users.password` is corrupt we don't want
        # to leak that fact to anonymous callers.
        return False


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenResponse:
    """
    Verify credentials → issue JWT.

    Returns 401 for any failure (bad email, bad password, wrong role, soft-
    deleted user). Discriminating between them in the response body would be
    a username-enumeration vector.
    """
    # email lookup — index hit, plus soft-delete filter (Laravel SoftDeletes
    # equivalent: deleted_at IS NULL).
    result = await db.execute(
        select(User).where(
            User.email == payload.email.lower(),
            User.deleted_at.is_(None),
        )
    )
    user = result.scalar_one_or_none()

    # Run a dummy verify even on miss to keep timing constant-ish. This is a
    # cheap mitigation; a determined attacker can still measure DB latency,
    # but the front line is rate limiting (added in Phase 2 via slowapi).
    bcrypt_dummy = "$2b$12$" + "a" * 53  # syntactically valid bcrypt placeholder
    password_ok = _verify_password(
        payload.password,
        user.password if user else bcrypt_dummy,
    )

    if user is None or not password_ok or user.role not in _ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
        )

    token = create_access_token(subject=user.id, settings=settings)

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut)
async def me(current_user: Annotated[User, Depends(get_current_admin_user)]) -> UserOut:
    """Return the authenticated admin user. Cheap profile probe for the SPA."""
    return UserOut.model_validate(current_user)
