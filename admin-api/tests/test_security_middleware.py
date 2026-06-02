"""
Tests for the env-driven TrustedHost + CORS allow-lists in admin-api.

Verifies:
  * `Settings.cors_origins` honours the documented precedence
    (CORS_ALLOW_ORIGINS > ADMIN_FRONTEND_URL > built-in defaults).
  * `Settings.trusted_hosts` honours TRUSTED_HOSTS, otherwise derives from
    cors_origins, never returns "*".
  * A request with `Host: evil.com` is rejected (TrustedHostMiddleware).
  * A CORS preflight from a non-allowlisted origin returns no
    `access-control-allow-origin` header.

We instantiate `Settings` directly with constructor kwargs (no env mutation,
no `.env` reads) so each test is hermetic. The integration tests build a
minimal FastAPI app wired up with the same middleware classes and parameters
that `app/main.py` uses — that isolates the security policy under test from
the rest of the admin-api stack (DB engine, alembic, etc.).

Run with pytest:

    cd admin-api && pytest tests/test_security_middleware.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.testclient import TestClient

# Make `app` importable when running pytest from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import (  # noqa: E402  (path setup must come first)
    DEFAULT_CORS_ORIGINS,
    DEFAULT_TRUSTED_HOSTS,
    Settings,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides) -> Settings:
    """
    Build a Settings instance with only the security-relevant fields set.

    Pydantic-settings reads env + `.env` on instantiation, which would make
    tests order-dependent. Constructor kwargs win over both, so we pass
    sensible defaults for the required-shape fields and let each test
    override what it cares about.
    """
    base = {
        "DATABASE_URL": "postgresql+asyncpg://x:y@h:5432/d",
        "INTERNAL_API_KEY": "test",
        "SECRET_KEY": "test",
        # The three security knobs default to "" (empty); tests pass overrides.
        "CORS_ALLOW_ORIGINS": "",
        "ADMIN_FRONTEND_URL": "",
        "TRUSTED_HOSTS": "",
    }
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings) -> FastAPI:
    """Reproduce app/main.py's middleware stack around a dummy route."""
    app = FastAPI()
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.trusted_hosts,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.get("/ping")
    async def _ping() -> dict[str, str]:
        return {"ok": "yes"}

    return app


# ---------------------------------------------------------------------------
# Pure-config tests — no HTTP, no middleware, just the resolved allow-lists.
# ---------------------------------------------------------------------------


class TestCorsOriginsPrecedence:
    """CORS_ALLOW_ORIGINS > ADMIN_FRONTEND_URL > built-in defaults."""

    def test_explicit_cors_var_wins(self) -> None:
        s = _make_settings(
            CORS_ALLOW_ORIGINS="https://a.example, https://b.example",
            ADMIN_FRONTEND_URL="http://legacy:3000",
        )
        assert s.cors_origins == ["https://a.example", "https://b.example"]

    def test_legacy_var_used_when_cors_var_empty(self) -> None:
        s = _make_settings(
            CORS_ALLOW_ORIGINS="",
            ADMIN_FRONTEND_URL="http://legacy:3000,http://other:3001",
        )
        assert s.cors_origins == ["http://legacy:3000", "http://other:3001"]

    def test_falls_back_to_safe_defaults(self) -> None:
        s = _make_settings(CORS_ALLOW_ORIGINS="", ADMIN_FRONTEND_URL="")
        assert s.cors_origins == list(DEFAULT_CORS_ORIGINS)

    def test_defaults_never_wildcard(self) -> None:
        # If anyone weakens the safe default, this test fails loudly.
        assert "*" not in DEFAULT_CORS_ORIGINS
        assert "https://admin.nolongin.com" in DEFAULT_CORS_ORIGINS


class TestTrustedHostsPrecedence:
    """TRUSTED_HOSTS > auto-derived from cors_origins."""

    def test_explicit_var_wins(self) -> None:
        s = _make_settings(TRUSTED_HOSTS="nolongin.com, *.nolongin.com")
        assert s.trusted_hosts == ["nolongin.com", "*.nolongin.com"]

    def test_auto_derived_from_cors_origins(self) -> None:
        s = _make_settings(
            CORS_ALLOW_ORIGINS="https://admin.example.com, https://portal.example.com",
        )
        hosts = s.trusted_hosts
        assert "admin.example.com" in hosts
        assert "portal.example.com" in hosts
        # In-cluster service + loopback are always allowed for healthchecks.
        assert "admin-api" in hosts
        assert "localhost" in hosts

    def test_no_cors_origin_falls_back_to_safe_defaults(self) -> None:
        # With nothing usable in cors_origins, the trusted_hosts list should
        # still resolve to something secure rather than just [loopback].
        s = _make_settings(
            CORS_ALLOW_ORIGINS="",
            ADMIN_FRONTEND_URL="",
        )
        hosts = s.trusted_hosts
        # cors_origins falls through to DEFAULT_CORS_ORIGINS, which gives us
        # admin.nolongin.com et al as auto-derived hosts.
        assert "admin.nolongin.com" in hosts
        assert "*" not in hosts

    def test_defaults_never_wildcard(self) -> None:
        assert "*" not in DEFAULT_TRUSTED_HOSTS


# ---------------------------------------------------------------------------
# HTTP integration tests — middleware behaviour with a real TestClient.
# ---------------------------------------------------------------------------


class TestTrustedHostMiddleware:
    def test_unknown_host_is_rejected(self) -> None:
        s = _make_settings(TRUSTED_HOSTS="nolongin.com,*.nolongin.com")
        with TestClient(_build_app(s)) as client:
            resp = client.get("/ping", headers={"Host": "evil.com"})
        assert resp.status_code == 400

    def test_known_host_is_accepted(self) -> None:
        s = _make_settings(TRUSTED_HOSTS="nolongin.com,*.nolongin.com")
        with TestClient(_build_app(s)) as client:
            resp = client.get("/ping", headers={"Host": "admin.nolongin.com"})
        assert resp.status_code == 200


class TestCorsPreflight:
    @staticmethod
    def _preflight(app: FastAPI, origin: str):
        with TestClient(app) as client:
            return client.options(
                "/ping",
                headers={
                    "Host": "admin.nolongin.com",
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                },
            )

    def test_non_allowlisted_origin_gets_no_allow_origin(self) -> None:
        s = _make_settings(
            TRUSTED_HOSTS="*.nolongin.com",
            CORS_ALLOW_ORIGINS="https://admin.nolongin.com",
        )
        resp = self._preflight(_build_app(s), "https://evil.example")
        lc = {k.lower() for k in resp.headers}
        assert "access-control-allow-origin" not in lc

    def test_allowlisted_origin_is_echoed(self) -> None:
        s = _make_settings(
            TRUSTED_HOSTS="*.nolongin.com",
            CORS_ALLOW_ORIGINS="https://admin.nolongin.com",
        )
        resp = self._preflight(_build_app(s), "https://admin.nolongin.com")
        lc = {k.lower(): v for k, v in resp.headers.items()}
        assert lc.get("access-control-allow-origin") == "https://admin.nolongin.com"


if __name__ == "__main__":
    # Allow `python tests/test_security_middleware.py` for parity with the
    # ai-engine convention. Pytest is the supported entry point in CI.
    sys.exit(pytest.main([__file__, "-v"]))
