"""
Application configuration.

All env reads go through `Settings()` so the codebase has one well-typed source
of truth instead of scattered `os.getenv` calls. The values listed here mirror
`.env.example` — keep them in sync.

Production safety
-----------------
When ``APP_ENV`` is ``production`` or ``staging`` the loader refuses to start
with placeholder values for the two secrets that gate trust boundaries:

* ``SECRET_KEY`` — signs every operator JWT. A leaked or guessable value lets
  any actor mint admin tokens.
* ``INTERNAL_API_KEY`` — the shared ``X-Internal-Key`` between admin-api,
  ai-engine, and data-pipeline. A placeholder here means any internet-reachable
  caller can forge "internal" requests.

The validators below treat anything obviously templated (``change-me``,
``placeholder``, ``CHANGE_ME``, ``your-...-here``) or shorter than 32 chars as
"still the default" and ``raise ValueError`` at import time. The service then
exits with a non-zero status before binding a port, so a misconfigured deploy
fails loud instead of silently running with predictable secrets.

Local dev (``APP_ENV=development`` / unset) keeps the in-class defaults so
``docker compose up`` still works without ceremony.
"""

import re
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Regex of obviously-templated tokens we never want to see in prod secrets.
# Case-insensitive: matches "change-me", "CHANGE_ME", "placeholder", "your-
# secret-here", "replace-me", "example", "TODO". Kept tight on purpose — we
# don't want false positives on legitimately random hex that happens to
# contain a dictionary word.
_PLACEHOLDER_PATTERN = re.compile(
    r"(change[-_ ]?me|placeholder|replace[-_ ]?me|your[-_ ].*[-_ ]?here|^example$|^todo$|^secret$|^changeme$)",
    re.IGNORECASE,
)

# Minimum acceptable secret length in production. 32 chars is the floor for an
# HS256 key per RFC 7518 §3.2 ("a key of the same size as the hash output"),
# and a comfortable lower bound for any shared-secret token.
_MIN_PROD_SECRET_LEN = 32

# Environments where placeholder secrets are a hard failure. Anything else
# (development, test, local, unset) keeps the lenient defaults so contributors
# can run `uvicorn` without writing a real .env first.
_STRICT_ENVS = frozenset({"production", "staging"})


def _looks_like_placeholder(value: str) -> bool:
    """Return True if ``value`` is empty, too short, or matches a template token."""
    if not value or len(value) < _MIN_PROD_SECRET_LEN:
        return True
    return bool(_PLACEHOLDER_PATTERN.search(value))


# ---------------------------------------------------------------------------
# Safe built-in allow-list defaults.
#
# Mirror ai-engine/main.py so the two services stay in lock-step. NEVER
# includes '*' — we'd rather break a fresh deploy than silently accept
# cross-site requests from arbitrary origins / hosts.
#
# Updating these is a code change on purpose; env override is the supported
# runtime knob (CORS_ALLOW_ORIGINS / TRUSTED_HOSTS).
# ---------------------------------------------------------------------------
DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "https://portal.nolongin.com",
    "https://admin.nolongin.com",
    "https://portal.bimaptsp.com",
    "https://admin.bimaptsp.com",
)
DEFAULT_TRUSTED_HOSTS: tuple[str, ...] = (
    "nolongin.com",
    "*.nolongin.com",
    "bimaptsp.com",
    "*.bimaptsp.com",
    "localhost",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Runtime environment --------------------------------------------
    # Drives the production/staging placeholder checks below. Anything other
    # than "production" or "staging" is treated as a dev-style environment
    # where defaults are acceptable. Matches Laravel's APP_ENV convention so
    # operators only memorise one value.
    APP_ENV: str = Field(
        default="development",
        description="Runtime environment: development, staging, or production.",
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
        description=(
            "Shared secret for X-Internal-Key. MUST match ai-engine + "
            "data-pipeline. Refused at startup in prod/staging if it still "
            "looks like a placeholder (see module docstring)."
        ),
    )

    # ---- JWT signing ----------------------------------------------------
    SECRET_KEY: str = Field(
        default="change-me-secret",
        description=(
            "HS256 signing key. Generate with `openssl rand -hex 32`. "
            "Refused at startup in prod/staging if it still looks like a "
            "placeholder (see module docstring)."
        ),
    )
    JWT_ALGORITHM: str = Field(default="HS256")
    JWT_EXPIRY_DAYS: int = Field(default=7, ge=1, le=90)

    # ---- CORS / trusted hosts ------------------------------------------
    # Three knobs, listed in precedence order:
    #
    #   1. CORS_ALLOW_ORIGINS — explicit override. Comma-separated full origins
    #      (scheme + host). When set, this REPLACES the ADMIN_FRONTEND_URL
    #      list. Use this in prod when origins diverge from "the one admin
    #      frontend URL" (e.g. supporting both nolongin.com and bimaptsp.com).
    #   2. ADMIN_FRONTEND_URL — legacy single-purpose var, kept for backward
    #      compat with existing local-dev `.env` files. Comma-separated.
    #   3. Built-in safe defaults — used when both vars are empty/unset. NEVER
    #      "*"; we'd rather break a misconfigured deploy than silently accept
    #      cross-site requests from arbitrary origins.
    #
    # TRUSTED_HOSTS is a separate independent knob (host headers, not origins):
    #   - When set, REPLACES the auto-derived host list.
    #   - When unset, hosts are derived from cors_origins (matches old behaviour)
    #     plus a safe default list.
    CORS_ALLOW_ORIGINS: str = Field(
        default="",
        description=(
            "Comma-separated allowed CORS origins (full scheme+host, e.g. "
            "https://admin.nolongin.com). Empty falls back to ADMIN_FRONTEND_URL "
            "then to a safe built-in default list. Never '*'."
        ),
    )
    ADMIN_FRONTEND_URL: str = Field(
        default="http://localhost:3000",
        description=(
            "Legacy: comma-separated allowed origins for the admin frontend. "
            "Prefer CORS_ALLOW_ORIGINS in new deployments."
        ),
    )
    TRUSTED_HOSTS: str = Field(
        default="",
        description=(
            "Comma-separated trusted Host header values (bare hostnames, no "
            "scheme). Starlette supports wildcard subdomains like '*.foo.com'. "
            "Empty falls back to the host part of cors_origins + safe defaults. "
            "Never '*'."
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

    @field_validator("APP_ENV")
    @classmethod
    def _normalize_app_env(cls, v: str) -> str:
        # Normalise to lower-case so "Production" / "PROD" still match the
        # strict-env check. We intentionally don't restrict the allowed set
        # (e.g. "test", "ci") — only "production" and "staging" trigger the
        # placeholder-secret refusal.
        return (v or "development").strip().lower()

    @field_validator("SECRET_KEY")
    @classmethod
    def _reject_placeholder_secret_key(cls, v: str, info) -> str:
        # APP_ENV is defined before SECRET_KEY in the class body, so by the
        # time this validator runs Pydantic has already populated info.data
        # with the normalised APP_ENV. If APP_ENV failed validation we'd
        # never reach this point.
        env = (info.data.get("APP_ENV") or "development").lower()
        if env in _STRICT_ENVS and _looks_like_placeholder(v):
            raise ValueError(
                "SECRET_KEY looks like a placeholder or is shorter than "
                f"{_MIN_PROD_SECRET_LEN} chars. Refusing to start in "
                f"APP_ENV={env!r}. Generate one with `openssl rand -hex 32` "
                "and set it via the SECRET_KEY env var. This guards against "
                "shipping the in-code default and letting anyone mint admin "
                "JWTs."
            )
        return v

    @field_validator("INTERNAL_API_KEY")
    @classmethod
    def _reject_placeholder_internal_key(cls, v: str, info) -> str:
        # Same posture as SECRET_KEY — a placeholder here would let any
        # caller forge "internal" requests to ai-engine + data-pipeline.
        env = (info.data.get("APP_ENV") or "development").lower()
        if env in _STRICT_ENVS and _looks_like_placeholder(v):
            raise ValueError(
                "INTERNAL_API_KEY looks like a placeholder or is shorter "
                f"than {_MIN_PROD_SECRET_LEN} chars. Refusing to start in "
                f"APP_ENV={env!r}. Set INTERNAL_API_KEY to the rotated "
                "shared secret (must match ai-engine + data-pipeline). "
                "This guards against forged X-Internal-Key calls bypassing "
                "service-to-service auth."
            )
        return v

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

    @staticmethod
    def _split_csv(raw: str) -> list[str]:
        """Comma-split, strip, drop empties. Pure function for testability."""
        return [item.strip() for item in (raw or "").split(",") if item.strip()]

    @property
    def cors_origins(self) -> list[str]:
        """
        Resolved CORS allow-list.

        Precedence:
          1. CORS_ALLOW_ORIGINS (explicit, comma-separated).
          2. ADMIN_FRONTEND_URL (legacy single-purpose var).
          3. Built-in safe defaults — never '*'.
        """
        explicit = self._split_csv(self.CORS_ALLOW_ORIGINS)
        if explicit:
            return explicit
        legacy = self._split_csv(self.ADMIN_FRONTEND_URL)
        if legacy:
            return legacy
        return list(DEFAULT_CORS_ORIGINS)

    @property
    def trusted_hosts(self) -> list[str]:
        """
        Resolved TrustedHostMiddleware allow-list (bare hostnames, no scheme).

        Precedence:
          1. TRUSTED_HOSTS (explicit, comma-separated; supports '*.foo.com').
          2. Auto-derived from cors_origins host parts + a safe default list.
             This preserves the prior behaviour where one env var configured
             both CORS and TrustedHost in lock-step.
        """
        explicit = self._split_csv(self.TRUSTED_HOSTS)
        if explicit:
            return explicit

        from urllib.parse import urlparse

        hosts: list[str] = []
        for origin in self.cors_origins:
            parsed = urlparse(origin)
            host = parsed.hostname
            if host:
                hosts.append(host)
        # Always allow the in-cluster service name + loopback so internal
        # health-checks don't 400 on Host validation; otherwise fall back to
        # the safe default suffix list when no CORS host was derivable.
        hosts.extend(["admin-api", "localhost", "127.0.0.1"])
        if not any(h for h in hosts if h not in {"admin-api", "localhost", "127.0.0.1"}):
            hosts.extend(DEFAULT_TRUSTED_HOSTS)
        # De-dupe while preserving order.
        return list(dict.fromkeys(hosts))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor — Settings() reads env each call without the cache."""
    return Settings()
