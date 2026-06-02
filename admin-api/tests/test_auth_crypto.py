"""
Regression tests for the auth crypto seam after the CVE patch bumps:
    * python-jose >= 3.4.0 (CVE-2024-33664)
    * bcrypt      >= 4.0   (bcrypt 3.x EOL)

These tests deliberately avoid the DB layer — they exercise only the pure
crypto helpers in app.routers.auth and app.deps so they can be run without
a live Postgres. The goal isn't coverage; it's an early-warning canary for
the two libraries we just bumped.

Run with:
    cd admin-api && pytest tests/ -q
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt
from passlib.context import CryptContext

# Mirror the production CryptContext exactly (see app/routers/auth.py:34).
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

# A fixed HS256 secret. NOT a real key — only used to round-trip jwt.encode /
# jwt.decode in-process.
_TEST_SECRET = "test-secret-" + "x" * 32
_TEST_ALG = "HS256"


# ---------------------------------------------------------------------------
# bcrypt: hash + verify must work on the new bcrypt 4.x backend.
# ---------------------------------------------------------------------------
def test_bcrypt_hash_then_verify_roundtrip() -> None:
    """Fresh hash → verify True for the same plaintext, False for any other."""
    plain = "BimaAdmin2026!"
    hashed = _pwd.hash(plain)
    # bcrypt hashes are 60 chars and start with $2b$ on modern bcrypt.
    assert hashed.startswith("$2b$"), hashed
    assert _pwd.verify(plain, hashed) is True
    assert _pwd.verify("wrong-password", hashed) is False


def test_bcrypt_verifies_laravel_style_2y_prefix() -> None:
    """
    Laravel's bcrypt driver emits hashes with the '$2y$' prefix. passlib treats
    $2y$ and $2b$ as interchangeable; this is the production path that
    /auth/login takes against the Laravel-issued `users.password` column. If
    bcrypt 4.x ever breaks $2y$ acceptance, this fails loudly.
    """
    # Generated locally with bcrypt and the prefix replaced — equivalent to a
    # Laravel-issued hash of the plaintext below.
    plain = "password123"
    hashed_2b = _pwd.hash(plain)
    hashed_2y = "$2y$" + hashed_2b[4:]
    assert _pwd.verify(plain, hashed_2y) is True


def test_bcrypt_malformed_hash_does_not_raise_in_wrapper() -> None:
    """
    auth._verify_password catches ValueError/TypeError from passlib so callers
    get a clean False instead of a 500. This guards that contract.
    """
    # Import lazily so the test module can be collected without app context.
    from app.routers.auth import _verify_password  # noqa: PLC0415

    assert _verify_password("anything", "not-a-bcrypt-hash") is False
    assert _verify_password("anything", "") is False


# ---------------------------------------------------------------------------
# python-jose: encode + decode must round-trip cleanly on the new 3.4.x line.
# ---------------------------------------------------------------------------
def test_jwt_hs256_roundtrip() -> None:
    """Encode a payload, decode it back, claims survive intact."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "42",
        "kind": "admin",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    token = jwt.encode(payload, _TEST_SECRET, algorithm=_TEST_ALG)
    decoded = jwt.decode(token, _TEST_SECRET, algorithms=[_TEST_ALG])
    assert decoded["sub"] == "42"
    assert decoded["kind"] == "admin"


def test_jwt_expired_token_raises_expired_signature_error() -> None:
    """Expired tokens must raise ExpiredSignatureError, not generic JWTError —
    the auth deps depend on that distinction to return the right 401 body."""
    from jose import ExpiredSignatureError  # noqa: PLC0415

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    payload = {
        "sub": "1",
        "iat": int(past.timestamp()),
        "exp": int(past.timestamp()) + 1,  # expired 1h ago + 1s
    }
    token = jwt.encode(payload, _TEST_SECRET, algorithm=_TEST_ALG)
    with pytest.raises(ExpiredSignatureError):
        jwt.decode(token, _TEST_SECRET, algorithms=[_TEST_ALG])


def test_jwt_bad_signature_raises_jwt_error() -> None:
    """A token signed with key A must NOT decode under key B."""
    from jose import JWTError  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    payload = {
        "sub": "1",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    token = jwt.encode(payload, _TEST_SECRET, algorithm=_TEST_ALG)
    with pytest.raises(JWTError):
        jwt.decode(token, _TEST_SECRET + "-tampered", algorithms=[_TEST_ALG])


def test_jwt_algorithm_whitelist_rejects_none() -> None:
    """
    Defense-in-depth: python-jose has historically been vulnerable to alg=none
    confusion. Confirm that passing algorithms=['HS256'] rejects an alg=none
    token. This is the contract app/deps.py:145 relies on.
    """
    from jose import JWTError  # noqa: PLC0415

    # alg=none JWT: header.payload.<empty-sig>
    import base64
    import json

    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps({"sub": "1"}).encode()).rstrip(b"=")
    forged = b".".join([header, body, b""]).decode()

    with pytest.raises(JWTError):
        jwt.decode(forged, _TEST_SECRET, algorithms=[_TEST_ALG])
