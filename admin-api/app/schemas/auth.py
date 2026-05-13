"""
Auth-related Pydantic schemas.

Kept deliberately small for Phase 1: login in, JWT + sanitized user out.
NIK / NPWP / phone never appear in `UserOut` — Decisions.md §4 PII rules.
"""

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    # 8 is the floor; the upper bound is generous because Argon2/bcrypt
    # can't actually hash >72 bytes meaningfully but we accept and let
    # bcrypt truncate (matching Laravel's behaviour).
    password: str = Field(min_length=1, max_length=255)


class UserOut(BaseModel):
    """Minimal admin-user payload returned to the frontend on login + /me."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: EmailStr
    role: str  # 'admin' | 'dpmptsp_staff' (msme can never log into admin)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
