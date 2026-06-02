"""
Unit tests for the production placeholder-secret validators in app.config.

Why these tests exist
---------------------
The Settings class refuses to instantiate in ``APP_ENV=production`` or
``APP_ENV=staging`` if SECRET_KEY or INTERNAL_API_KEY still look like the
in-code defaults. That refusal is the only thing standing between a sloppy
deploy and an attacker minting valid admin JWTs (SECRET_KEY) or forging
service-to-service requests with a known X-Internal-Key.

The tests below pin the matrix down: development is lenient, prod/staging
is strict, and the regex catches the placeholders we actually ship in
``.env.example``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings, _looks_like_placeholder

# A pair of secrets long enough and "random" enough to clear every check.
# Don't replace with real entropy — we want the test corpus deterministic.
GOOD_SECRET = "a" * 40
GOOD_INTERNAL = "b" * 40
GOOD_DB_URL = "postgresql+asyncpg://user:pw@host:5432/db"


def _build(**overrides):
    """Construct Settings with safe defaults plus per-test overrides.

    ``_env_file=None`` keeps Pydantic from picking up a stray .env on the
    contributor's machine — every value here must come from the test's own
    kwargs.
    """
    base = {
        "_env_file": None,
        "DATABASE_URL": GOOD_DB_URL,
        "SECRET_KEY": GOOD_SECRET,
        "INTERNAL_API_KEY": GOOD_INTERNAL,
    }
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# _looks_like_placeholder helper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        "",  # empty
        "short",  # < 32 chars
        "change-me-secret",  # in-code default
        "change-me-internal",  # in-code default
        "CHANGE_ME_match_existing_laravel_value",  # .env.example placeholder
        "CHANGE_ME_openssl_rand_hex_32",  # .env.example placeholder
        "your-secret-here-please-replace",  # common template pattern
        "placeholder-value-do-not-use",
    ],
)
def test_helper_flags_obvious_placeholders(value: str) -> None:
    assert _looks_like_placeholder(value) is True


def test_helper_passes_good_secret() -> None:
    # A 40-char random-looking token must clear both length and regex checks.
    assert _looks_like_placeholder(GOOD_SECRET) is False


# ---------------------------------------------------------------------------
# Development environment: lenient
# ---------------------------------------------------------------------------

def test_development_allows_placeholder_secrets() -> None:
    # Local contributors must be able to `uvicorn` without writing a real
    # .env. The Settings class therefore keeps placeholder defaults usable
    # whenever APP_ENV is anything other than production/staging.
    s = _build(
        APP_ENV="development",
        SECRET_KEY="change-me-secret",
        INTERNAL_API_KEY="change-me-internal",
    )
    assert s.APP_ENV == "development"


def test_unset_app_env_defaults_to_development() -> None:
    # If APP_ENV is omitted entirely we should not accidentally enter
    # strict mode — that would brick CI runs and fresh clones.
    s = _build(SECRET_KEY="change-me-secret", INTERNAL_API_KEY="change-me-internal")
    assert s.APP_ENV == "development"


# ---------------------------------------------------------------------------
# Production / staging: strict
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env", ["production", "staging", "PRODUCTION", "Staging"])
def test_strict_env_rejects_placeholder_secret_key(env: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _build(APP_ENV=env, SECRET_KEY="change-me-secret")
    # The error message MUST name the offending field so an operator
    # tailing logs at 3am can fix it without re-reading the source.
    assert "SECRET_KEY" in str(exc.value)


@pytest.mark.parametrize("env", ["production", "staging"])
def test_strict_env_rejects_placeholder_internal_api_key(env: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _build(APP_ENV=env, INTERNAL_API_KEY="change-me-internal")
    assert "INTERNAL_API_KEY" in str(exc.value)


def test_production_rejects_short_secret_key() -> None:
    # 31 chars must fail (the floor is 32 per RFC 7518 for HS256).
    with pytest.raises(ValidationError):
        _build(APP_ENV="production", SECRET_KEY="a" * 31)


def test_production_rejects_dotenv_example_values() -> None:
    # The two literal values that ship in .env.example must be refused
    # in prod — that's the whole reason this validator exists.
    with pytest.raises(ValidationError) as exc:
        _build(
            APP_ENV="production",
            SECRET_KEY="CHANGE_ME_openssl_rand_hex_32",
            INTERNAL_API_KEY="CHANGE_ME_match_existing_laravel_value",
        )
    # Both fields should be reported in the same load — fewer round-trips
    # for the operator fixing the deploy.
    msg = str(exc.value)
    assert "SECRET_KEY" in msg and "INTERNAL_API_KEY" in msg


def test_production_accepts_good_secrets() -> None:
    # Sanity: a properly-set production deploy still works.
    s = _build(APP_ENV="production")
    assert s.APP_ENV == "production"
    assert s.SECRET_KEY == GOOD_SECRET
    assert s.INTERNAL_API_KEY == GOOD_INTERNAL
