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
    #
    # ADMIN_FRONTEND_URL is the historical name and remains the canonical
    # source for cors_origins. CORS_ALLOW_ORIGINS and TRUSTED_HOSTS are
    # *additive* env vars introduced for the bimaptsp.com migration window:
    # they let DevOps add the new apex alongside the legacy one without
    # rewriting the existing prod value of ADMIN_FRONTEND_URL.
    #
    # Resolution order:
    #   cors_origins  = ADMIN_FRONTEND_URL ∪ CORS_ALLOW_ORIGINS
    #   trusted_hosts = hosts(cors_origins) ∪ TRUSTED_HOSTS ∪ in-cluster defaults
    ADMIN_FRONTEND_URL: str = Field(
        default="http://localhost:3000",
        description="Comma-separated allowed origins for the admin frontend.",
    )
    # Additive CORS allow-list for the domain migration. Comma-separated, no
    # trailing slashes. Default carries BOTH the legacy nolongin.com apex AND
    # the target bimaptsp.com apex so a flipped DNS record doesn't 403 the
    # admin during the transition window. Production may override.
    CORS_ALLOW_ORIGINS: str = Field(
        default=(
            "https://admin.nolongin.com,https://portal.nolongin.com,"
            "https://nolongin.com,"
            "https://admin.bimaptsp.com,https://portal.bimaptsp.com,"
            "https://bimaptsp.com"
        ),
        description=(
            "Additional comma-separated CORS allow-origins, merged with "
            "ADMIN_FRONTEND_URL. Used to stage the bimaptsp.com migration."
        ),
    )
    # Additive trusted-host list for the domain migration. Comma-separated
    # BARE hostnames (no scheme, no path). Merged with the hostnames derived
    # from cors_origins plus the in-cluster service defaults.
    TRUSTED_HOSTS: str = Field(
        default=(
            "nolongin.com,www.nolongin.com,admin.nolongin.com,portal.nolongin.com,"
            "bimaptsp.com,www.bimaptsp.com,admin.bimaptsp.com,portal.bimaptsp.com"
        ),
        description=(
            "Additional comma-separated trusted hostnames for "
            "TrustedHostMiddleware. Merged with cors_origins-derived hosts."
        ),
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

    # ---- Portal deep-link base -----------------------------------------
    # Used by /case/{ticket}/validate and /tracking responses when admin-api
    # surfaces a portal track URL back to the caller. Documented here so
    # admin-api stays in sync with ai-engine's PORTAL_TRACK_URL_BASE during
    # the bimaptsp.com migration. No code path reads this yet — leaving the
    # field declared so DevOps can flip both services in one config push.
    #   Legacy:  https://portal.nolongin.com/track
    #   Target:  https://portal.bimaptsp.com/track  (post-DNS cutover)
    PORTAL_TRACK_URL_BASE: str = Field(
        default="https://portal.nolongin.com/track",
        description="Portal /track deep-link base. Migrated to bimaptsp.com post-DNS.",
    )

    # ---- SIAP signing deep-link base -----------------------------------
    # Documented here for cross-service config parity. Today only ai-engine's
    # officer_copilot reads SIAP_SIGNING_URL_BASE; declaring it on admin-api
    # too lets a single .env push update both services and prevents drift if
    # a future admin-api endpoint needs to surface the same signing URL.
    SIAP_SIGNING_URL_BASE: str = Field(
        default="https://beta-siap.nolongin.com",
        description="SIAP Filament admin base for signature deep-links.",
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
        """Parsed allow-list. Strips whitespace, drops empties.

        Merges ADMIN_FRONTEND_URL with CORS_ALLOW_ORIGINS — the additive env
        var introduced for the bimaptsp.com migration. The merge preserves
        the order: ADMIN_FRONTEND_URL entries first (so a dev's localhost
        stays in front during local dev), additive entries next, de-duped.
        """
        merged = (
            list(self.ADMIN_FRONTEND_URL.split(","))
            + list(self.CORS_ALLOW_ORIGINS.split(","))
        )
        seen: dict[str, None] = {}
        for origin in merged:
            clean = origin.strip().rstrip("/")
            if clean:
                seen.setdefault(clean, None)
        return list(seen.keys())

    @property
    def trusted_hosts(self) -> list[str]:
        """
        TrustedHostMiddleware wants bare hostnames (no scheme, no path).

        Builds the allowlist from three sources, in order:
          1. Hostnames derived from cors_origins (so admin operators only
             need to configure ADMIN_FRONTEND_URL + CORS_ALLOW_ORIGINS for
             the common case).
          2. Anything the operator explicitly listed in TRUSTED_HOSTS — used
             during the bimaptsp.com migration to keep BOTH apex domains
             trusted without restating every origin scheme.
          3. In-cluster defaults (admin-api / localhost / 127.0.0.1) so
             health checks on the Docker network don't 400 on Host
             validation.
        """
        from urllib.parse import urlparse

        hosts: list[str] = []
        for origin in self.cors_origins:
            parsed = urlparse(origin)
            host = parsed.hostname
            if host:
                hosts.append(host)
        for raw in self.TRUSTED_HOSTS.split(","):
            host = raw.strip().lower()
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
