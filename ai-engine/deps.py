"""
FastAPI dependency helpers shared across routers.

Two auth primitives live here:

  * `require_internal_key` — strict X-Internal-Key gate (service-to-service).
    Used by validator, vectorize, etc. The original dep, kept untouched so
    routers that already trust it keep working.

  * `require_chat_auth`    — looser gate for the citizen-chat webhook surface
    (`/webhook/chat`, `/webhook/chat/stream`). Accepts EITHER:
       1. a valid `X-Internal-Key` header (server-to-server caller — the
          portal SSR proxy or admin-api), in which case the body's
          `user_id` is trusted as-is; OR
       2. a citizen-SSO `Bearer` JWT (`kind='pemohon'`, same secret +
          algorithm admin-api signs with), in which case the auth context
          carries a `verified_user_id` derived from the token's `sub` claim.
          The router MUST use that to override (or validate against) the
          body's user_id — never trust a client-supplied id.

The webhook router used to be wide-open: any client could POST
`{user_id: 'web-7', message: '…'}` and impersonate any other citizen
session — burning their rate-limit, poisoning their history, leaking
their RAG context. This dep closes that hole.

The Telegram / APTANA inbound routers have their own provider-specific
secret-token validation (path-shared secret for APTANA, header secret
for Telegram). They are intentionally NOT routed through this dep — see
`routers/aptana.py` and `routers/telegram.py`.
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass
from typing import Annotated, Optional

from fastapi import Header, HTTPException, status
from jose import ExpiredSignatureError, JWTError, jwt

logger = logging.getLogger(__name__)

# Accept either INTERNAL_API_KEY (preferred, matches admin-api convention)
# or the legacy LARAVEL_API_KEY (which has historically held the same
# shared secret). New deployments should set INTERNAL_API_KEY.
_INTERNAL_KEY: str = os.getenv("INTERNAL_API_KEY", "") or os.getenv("LARAVEL_API_KEY", "")

# JWT verification config — MUST match admin-api/app/config.py so a token
# minted by `POST /sso/login` validates here without any cross-service
# secret indirection. Both services share the same Settings.SECRET_KEY
# in production (a single env value injected via docker-compose). If you
# rotate one, rotate both in lock-step.
_JWT_SECRET: str = os.getenv("CITIZEN_JWT_SECRET", "") or os.getenv("SECRET_KEY", "")
_JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")

# Placeholder defaults to reject at startup. admin-api ships with
# "change-me-secret" / "change-me-internal" as Pydantic defaults to make
# the codebase importable in CI without env wiring — but a real deployment
# must replace both. We never want a JWT signed with `change-me-secret`
# to succeed against ai-engine.
_FORBIDDEN_PLACEHOLDERS: frozenset[str] = frozenset({
    "",
    "change-me",
    "change-me-secret",
    "change-me-internal",
    "your_internal_api_key_here",
    "placeholder",
})


@dataclass(frozen=True)
class ChatAuthContext:
    """
    What the chat router gets back from `require_chat_auth`.

    Exactly one of `via_internal_key` / `verified_user_id` is set per
    request:

      * via_internal_key=True, verified_user_id=None
        → server-to-server caller. Trust body.user_id.

      * via_internal_key=False, verified_user_id='web-<id>'
        → citizen JWT. Router MUST override body.user_id (or 403 if the
          client-supplied id doesn't match).
    """

    via_internal_key: bool
    verified_user_id: Optional[str]


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


def _internal_key_configured() -> bool:
    """True iff a usable internal key is configured (not blank, not a placeholder)."""
    return bool(_INTERNAL_KEY) and _INTERNAL_KEY not in _FORBIDDEN_PLACEHOLDERS


def _jwt_secret_configured() -> bool:
    """True iff a usable JWT secret is configured (not blank, not a placeholder)."""
    return bool(_JWT_SECRET) and _JWT_SECRET not in _FORBIDDEN_PLACEHOLDERS


def _verify_internal_key(candidate: str) -> bool:
    """Constant-time compare of a candidate X-Internal-Key value."""
    if not _internal_key_configured():
        return False
    return hmac.compare_digest(candidate, _INTERNAL_KEY)


def _decode_citizen_jwt(token: str) -> dict:
    """
    Verify a citizen JWT and return its claims.

    Raises HTTPException(401) on any failure path — the chat router converts
    that into a generic 401 response without leaking which path failed.
    """
    if not _jwt_secret_configured():
        # Distinct error message in logs (server-side) — the client still
        # sees a generic 401 because we re-raise as such below if reached
        # via require_chat_auth. We do not 503 here because the chat surface
        # also accepts X-Internal-Key; missing JWT secret alone is not a
        # service-unavailable condition.
        logger.error(
            "CITIZEN_JWT_SECRET / SECRET_KEY is not configured or is a placeholder — "
            "all JWT-based chat auth will fail."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Citizen auth not configured.",
        )

    try:
        payload = jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
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

    # Only citizen-SSO tokens may reach the chat surface. Admin tokens are
    # routed through other deps — never through this one — to keep the
    # impersonation surface tight.
    if payload.get("kind") != "pemohon":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Citizen token required.",
        )

    sub = payload.get("sub")
    if sub is None or str(sub).strip() == "":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject.",
        )

    return payload


def _user_id_from_claims(payload: dict) -> str:
    """
    Derive the canonical chat user_id from a verified JWT payload.

    Convention: web-chat sessions are prefixed `web-` to keep them from
    colliding with WhatsApp (`wa-…`) or Telegram (`tg-…`) sessions in the
    shared history dict. The portal proxy used to compose this string from
    `web-${profile_id}`; here we do the same so the history key is stable
    across the migration from "trust the body" to "verify the token".
    """
    return f"web-{payload['sub']}"


async def require_chat_auth(
    x_internal_key: Annotated[Optional[str], Header(alias="X-Internal-Key")] = None,
    authorization: Annotated[Optional[str], Header(alias="Authorization")] = None,
) -> ChatAuthContext:
    """
    Gate for /webhook/chat and /webhook/chat/stream.

    Accepts EITHER a valid X-Internal-Key (server-to-server caller) OR a
    valid `Authorization: Bearer <citizen-JWT>`. Anything else → 401.

    The router consumes the returned `ChatAuthContext` to decide whether
    body.user_id is trusted (internal-key path) or must be overridden by
    the JWT's verified subject (citizen-JWT path).
    """
    # Path 1 — internal service-to-service caller. The portal SSR proxy
    # uses this so the browser never sees the shared secret; admin-api
    # uses it for officer-facing chat surfaces.
    if x_internal_key is not None and x_internal_key != "":
        if _verify_internal_key(x_internal_key):
            return ChatAuthContext(via_internal_key=True, verified_user_id=None)
        # Wrong key supplied — fall through to JWT check would risk
        # leaking timing about whether the key shape was right. Reject
        # immediately with 403 (matches admin-api dep behavior).
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid X-Internal-Key.",
        )

    # Path 2 — citizen JWT in the Authorization header.
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token:
            payload = _decode_citizen_jwt(token)
            return ChatAuthContext(
                via_internal_key=False,
                verified_user_id=_user_id_from_claims(payload),
            )

    # Neither auth method present and valid.
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required.",
        headers={"WWW-Authenticate": "Bearer"},
    )
