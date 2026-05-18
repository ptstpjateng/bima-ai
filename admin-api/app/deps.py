"""
FastAPI dependency functions.

All dependencies live here so router files stay thin and tests can import
the same callables without going through HTTP. Three deps for Phase 1:

- `get_db`            — async session per request.
- `get_current_admin_user` — JWT bearer; loads + role-checks the user.
- `require_internal_key`   — X-Internal-Key header, constant-time compared.

Adding more deps in Phase 2 (require_role(...), pagination, etc.) goes here.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.models import User

# tokenUrl points at the login endpoint so /docs renders the auth flow
# correctly. auto_error=False lets us return our own 401 body instead of the
# default {"detail": "Not authenticated"}.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped AsyncSession. Mirror of `db.get_session`."""
    async for session in get_session():
        yield session


# ---------------------------------------------------------------------------
# JWT helpers (kept here, not in routers, so multiple endpoints can issue
# tokens later without duplicating the encode logic).
# ---------------------------------------------------------------------------
def create_access_token(*, subject: str | int, settings: Settings) -> str:
    """Issue an HS256-signed JWT with `sub`, `iat`, `exp` claims."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.JWT_EXPIRY_DAYS)).timestamp()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_token_with_claims(
    *,
    subject: str | int,
    extra_claims: dict,
    settings: Settings,
) -> str:
    """
    Like `create_access_token` but accepts extra (non-reserved) claims.

    Used by `routers/sso.py` to embed citizen identity in the token (nik,
    name, kind='pemohon') so `/sso/me` is a stateless reads from the JWT
    instead of another SIAP DB round-trip.

    `extra_claims` MUST NOT contain `sub`, `iat`, or `exp` — those are
    managed here. We assert rather than silently overwrite so a typo in a
    caller surfaces immediately.
    """
    reserved = {"sub", "iat", "exp"}
    overlap = reserved & set(extra_claims)
    if overlap:
        raise ValueError(f"extra_claims cannot contain reserved keys: {overlap}")

    now = datetime.now(timezone.utc)
    payload = {
        **extra_claims,
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.JWT_EXPIRY_DAYS)).timestamp()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


# ---------------------------------------------------------------------------
# Current admin user
# ---------------------------------------------------------------------------
# These are the only roles permitted to call admin endpoints. 'msme' users
# never get a JWT from this service — they go through the legacy Laravel
# magic-link flow until Sprint C migrates that too.
_ADMIN_ROLES: frozenset[str] = frozenset({"admin", "dpmptsp_staff"})


async def get_current_admin_user(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    """
    Decode the bearer token, load the user, enforce admin role.

    - Missing token / bad signature / expired → 401
    - Valid JWT but user gone / soft-deleted → 401
    - Valid user but wrong role → 403
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
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

    sub = payload.get("sub")
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing subject."
        )

    try:
        user_id = int(sub)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token subject malformed."
        ) from None

    # Load + filter out soft-deleted accounts in a single query.
    result = await db.execute(
        select(User).where(User.id == user_id, User.deleted_at.is_(None))
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists."
        )

    if user.role not in _ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required."
        )

    return user


# ---------------------------------------------------------------------------
# X-Internal-Key (service-to-service auth)
# ---------------------------------------------------------------------------
async def require_internal_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_internal_key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
) -> None:
    """
    Mirror Laravel's `hash_equals` check on the X-Internal-Key header.

    Same header name + same comparison primitive (`hmac.compare_digest` is
    Python's constant-time equivalent) so ai-engine and data-pipeline don't
    need any code changes when their downstream flips from Laravel to
    admin-api.
    """
    if not x_internal_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing X-Internal-Key header.",
        )

    if not hmac.compare_digest(x_internal_key, settings.INTERNAL_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid internal key.",
        )
