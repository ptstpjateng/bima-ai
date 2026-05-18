"""
Application configuration.

All env reads go through `Settings()` so the codebase has one well-typed source
of truth instead of scattered `os.getenv` calls. The values listed here mirror
`.env.example` — keep them in sync.
"""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Database --------------------------------------------------------
    # Must use the +asyncpg driver, e.g.
    # postgresql+asyncpg://user:pass@host:5432/db
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://bima:bima@postgres:5432/bima_ai",
        description="SQLAlchemy async URL. asyncpg driver required.",
    )

    # ---- Service-to-service auth ---------------------------------------
    INTERNAL_API_KEY: str = Field(
        default="change-me-internal",
        description="Shared secret for X-Internal-Key. MUST match ai-engine + data-pipeline.",
    )

    # ---- JWT signing ----------------------------------------------------
    SECRET_KEY: str = Field(
        default="change-me-secret",
        description="HS256 signing key. Generate with `openssl rand -hex 32`.",
    )
    JWT_ALGORITHM: str = Field(default="HS256")
    JWT_EXPIRY_DAYS: int = Field(default=7, ge=1, le=90)

    # ---- CORS / trusted hosts ------------------------------------------
    # Comma-separated string in env, parsed to a list of clean origins below.
    ADMIN_FRONTEND_URL: str = Field(
        default="http://localhost:3000",
        description="Comma-separated allowed origins for the admin frontend.",
    )

    # ---- ai-engine cross-service ---------------------------------------
    # Used by the dashboard + KBLI detail endpoints to fetch ChromaDB chunk
    # counts from ai-engine's /health endpoint. Must be reachable on the
    # internal Docker network. 2s timeout is enforced at the call site so a
    # flapping ai-engine never blocks an admin dashboard render.
    AI_ENGINE_URL: str = Field(
        default="http://ai-engine:8000",
        description="Base URL of ai-engine for internal cross-service queries.",
    )

    # ---- SIAP Jateng (read-only Sanctum API) ---------------------------
    # DPMPTSP Central Java production licensing system. Used by the
    # /tracking/{ticket} endpoint to render permit status on the portal.
    # Token issued by SIAP via POST /api/v1/login. Leave SIAP_API_TOKEN
    # blank to disable the integration (endpoint returns 503 cleanly).
    SIAP_API_BASE: str = Field(
        default="",
        description="SIAP base URL, e.g. https://perizinan.jatengprov.go.id. Empty = disabled.",
    )
    SIAP_API_TOKEN: str = Field(
        default="",
        description="Sanctum bearer for BIMA service account. Empty = disabled.",
    )
    SIAP_TIMEOUT_SECONDS: float = Field(default=8.0, ge=1.0, le=30.0)

    # ---- SIAP Jateng (direct DB read for citizen SSO) -------------------
    # NIK-only handshake against ptsp.person_profile (Phase 1 demo-grade
    # auth — to be tightened post-hackathon with SMS OTP). Leave empty to
    # disable the SSO endpoint (it returns 503 cleanly).
    #
    # Local dev: postgresql+asyncpg://siapjateng@127.0.0.1:5432/dbsiapjateng
    # Production: the equivalent network-reachable SIAP DSN.
    SIAP_DATABASE_URL: str = Field(
        default="",
        description=(
            "Async DSN to SIAP's dbsiapjateng. Empty = SSO disabled. "
            "Must use the +asyncpg driver."
        ),
    )

    # ---- Logging --------------------------------------------------------
    LOG_LEVEL: str = Field(default="INFO")

    # ---------------------------------------------------------------------
    # Validators / derived helpers
    # ---------------------------------------------------------------------

    @field_validator("DATABASE_URL")
    @classmethod
    def _require_asyncpg(cls, v: str) -> str:
        # Catch the most common mis-config early — a sync URL would break the
        # async session factory in `db.py` with a confusing runtime error.
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must start with `postgresql+asyncpg://`. "
                f"Got: {v.split('://', 1)[0]}://..."
            )
        return v

    @field_validator("SIAP_DATABASE_URL")
    @classmethod
    def _siap_url_optional_asyncpg(cls, v: str) -> str:
        # Empty string is allowed (disables SSO); anything else MUST use the
        # async driver. Same fail-fast posture as DATABASE_URL.
        if v and not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "SIAP_DATABASE_URL must start with `postgresql+asyncpg://` "
                "or be left empty."
            )
        return v

    @property
    def cors_origins(self) -> list[str]:
        """Parsed allow-list. Strips whitespace, drops empties."""
        return [origin.strip() for origin in self.ADMIN_FRONTEND_URL.split(",") if origin.strip()]

    @property
    def trusted_hosts(self) -> list[str]:
        """
        TrustedHostMiddleware wants bare hostnames (no scheme, no path). Build
        them from the same env var so admin operators only configure one thing.
        """
        from urllib.parse import urlparse

        hosts: list[str] = []
        for origin in self.cors_origins:
            parsed = urlparse(origin)
            host = parsed.hostname
            if host:
                hosts.append(host)
        # Also allow the in-cluster service name so health-checks from the
        # Docker network don't 400 on Host validation.
        hosts.extend(["admin-api", "localhost", "127.0.0.1"])
        # De-dupe while preserving order.
        return list(dict.fromkeys(hosts))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor — Settings() reads env each call without the cache."""
    return Settings()
