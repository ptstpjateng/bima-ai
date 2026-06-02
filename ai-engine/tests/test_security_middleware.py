"""
Tests for the env-driven TrustedHost + CORS allow-lists in `ai-engine/main.py`.

We don't import `main` itself (it transitively pulls chromadb, transformers,
google-genai, ...). Instead we exercise the two helpers that drive middleware
config — `_parse_csv_env` and the `_DEFAULT_*` lists — and then build a
minimal FastAPI app wired up with the SAME middleware classes and parameters
that `main.py` uses. That isolates the security policy under test from the
rest of the stack.

Run standalone (matches the other ai-engine tests):

    python -m tests.test_security_middleware     # from ai-engine/
    python tests/test_security_middleware.py     # also works

What it covers:
  * `_parse_csv_env` — unset env → safe default, empty env → safe default,
    populated env → parsed list, whitespace-only → safe default.
  * TrustedHostMiddleware rejects an unknown Host header with 400.
  * TrustedHostMiddleware accepts the configured host + a wildcard subdomain.
  * CORS preflight from a non-allowlisted origin returns no
    `access-control-allow-origin` header.
  * CORS preflight from an allowlisted origin echoes it back.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Load the two helpers from ai-engine/main.py WITHOUT executing the whole
# module (which would import every router + chromadb + the LLM stack). We
# parse the source, splice out the two top-level objects we care about, and
# exec them in an isolated namespace.
#
# This keeps the test light and removes any risk that an unrelated import
# failure in main.py masks a real middleware bug.
# ---------------------------------------------------------------------------

_MAIN_PATH = Path(__file__).resolve().parent.parent / "main.py"


def _load_helpers() -> tuple[callable, list[str], list[str]]:
    """Return (`_parse_csv_env`, `_DEFAULT_TRUSTED_HOSTS`, `_DEFAULT_CORS_ORIGINS`)."""
    source = _MAIN_PATH.read_text()
    import ast

    tree = ast.parse(source)
    wanted = {"_parse_csv_env", "_DEFAULT_TRUSTED_HOSTS", "_DEFAULT_CORS_ORIGINS"}
    chunks: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            chunks.append(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in wanted:
                chunks.append(node)
    module = ast.Module(body=chunks, type_ignores=[])
    ns: dict = {"os": os, "list": list}
    exec(compile(module, str(_MAIN_PATH), "exec"), ns)
    return ns["_parse_csv_env"], ns["_DEFAULT_TRUSTED_HOSTS"], ns["_DEFAULT_CORS_ORIGINS"]


_parse_csv_env, _DEFAULT_TRUSTED_HOSTS, _DEFAULT_CORS_ORIGINS = _load_helpers()


def _build_app(trusted_hosts: list[str], cors_origins: list[str]) -> FastAPI:
    """Same middleware stack `main.py` assembles, around a single dummy route."""
    app = FastAPI()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/ping")
    async def _ping() -> dict[str, str]:
        return {"ok": "yes"}

    return app


class ParseCsvEnvTests(unittest.TestCase):
    """The little helper that drives every middleware decision."""

    def setUp(self) -> None:
        # Snapshot + clean the env so each test starts known-good.
        self._saved = os.environ.pop("X_TEST_CSV", None)

    def tearDown(self) -> None:
        os.environ.pop("X_TEST_CSV", None)
        if self._saved is not None:
            os.environ["X_TEST_CSV"] = self._saved

    def test_unset_returns_default(self) -> None:
        self.assertEqual(_parse_csv_env("X_TEST_CSV", ["a", "b"]), ["a", "b"])

    def test_empty_returns_default(self) -> None:
        # Operator explicitly cleared the var — still use the safe list,
        # never collapse to "" → [] → an empty allow-list that rejects
        # everything (or, worse, that a downstream wildcards over).
        os.environ["X_TEST_CSV"] = ""
        self.assertEqual(_parse_csv_env("X_TEST_CSV", ["a"]), ["a"])

    def test_whitespace_only_returns_default(self) -> None:
        os.environ["X_TEST_CSV"] = "  , , "
        self.assertEqual(_parse_csv_env("X_TEST_CSV", ["a"]), ["a"])

    def test_populated_parses_and_strips(self) -> None:
        os.environ["X_TEST_CSV"] = " one , two,three "
        self.assertEqual(_parse_csv_env("X_TEST_CSV", ["fallback"]), ["one", "two", "three"])

    def test_defaults_are_not_wildcards(self) -> None:
        # The whole point of this PR: defaults must never be "*". If someone
        # ever softens them, this test fails loudly.
        self.assertNotIn("*", _DEFAULT_TRUSTED_HOSTS)
        self.assertNotIn("*", _DEFAULT_CORS_ORIGINS)
        # Spot-check the prod domains are present.
        self.assertIn("nolongin.com", _DEFAULT_TRUSTED_HOSTS)
        self.assertIn("https://admin.nolongin.com", _DEFAULT_CORS_ORIGINS)


class TrustedHostMiddlewareTests(unittest.TestCase):
    """A request with Host: evil.com must be rejected."""

    def test_unknown_host_is_rejected(self) -> None:
        app = _build_app(
            trusted_hosts=["nolongin.com", "*.nolongin.com"],
            cors_origins=["https://admin.nolongin.com"],
        )
        with TestClient(app) as client:
            resp = client.get("/ping", headers={"Host": "evil.com"})
        # Starlette returns 400 Bad Request on a host-header mismatch.
        self.assertEqual(resp.status_code, 400)

    def test_exact_host_is_accepted(self) -> None:
        app = _build_app(
            trusted_hosts=["nolongin.com", "*.nolongin.com"],
            cors_origins=["https://admin.nolongin.com"],
        )
        with TestClient(app) as client:
            resp = client.get("/ping", headers={"Host": "nolongin.com"})
        self.assertEqual(resp.status_code, 200)

    def test_wildcard_subdomain_is_accepted(self) -> None:
        app = _build_app(
            trusted_hosts=["nolongin.com", "*.nolongin.com"],
            cors_origins=["https://admin.nolongin.com"],
        )
        with TestClient(app) as client:
            resp = client.get("/ping", headers={"Host": "admin.nolongin.com"})
        self.assertEqual(resp.status_code, 200)


class CorsAllowlistTests(unittest.TestCase):
    """A preflight from a non-allowlisted origin must get no allow-origin."""

    def _preflight(self, app: FastAPI, origin: str):
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
        app = _build_app(
            trusted_hosts=["*.nolongin.com"],
            cors_origins=["https://admin.nolongin.com"],
        )
        resp = self._preflight(app, "https://evil.example")
        # CORSMiddleware silently omits the allow-origin header for a non-
        # matching origin. We assert on absence (the browser will block the
        # request). Status code may be 200 with no header, or 400 — either
        # way, what matters is the header is gone.
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in resp.headers})

    def test_allowlisted_origin_is_echoed_back(self) -> None:
        app = _build_app(
            trusted_hosts=["*.nolongin.com"],
            cors_origins=["https://admin.nolongin.com"],
        )
        resp = self._preflight(app, "https://admin.nolongin.com")
        # Headers are case-insensitive — use a normalised lookup.
        lc = {k.lower(): v for k, v in resp.headers.items()}
        self.assertEqual(lc.get("access-control-allow-origin"), "https://admin.nolongin.com")


if __name__ == "__main__":
    unittest.main()
