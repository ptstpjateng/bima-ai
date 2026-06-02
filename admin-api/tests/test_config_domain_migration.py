"""
Tests for the bimaptsp.com domain-migration config plumbing in
`admin-api/app/config.py`.

Why this matters: the migration relies on admin-api accepting requests from
BOTH legacy (nolongin.com) and target (bimaptsp.com) origins simultaneously
during the transition window. A regression that drops one of the apex
domains from CORS or TrustedHost would cause silent 400s/403s on the
admin dashboard right around demo day.

These tests pin the merge behavior (ADMIN_FRONTEND_URL ∪ CORS_ALLOW_ORIGINS)
and the trusted-host derivation explicitly, so a future refactor that
breaks either contract fails loudly.

Run standalone (matches ai-engine's tests/ convention):

    python -m tests.test_config_domain_migration   # from admin-api/
    python tests/test_config_domain_migration.py   # also works
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Make `app` importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402


def _make_settings(**env: str) -> Settings:
    """Build a Settings instance with env vars patched in.

    Settings reads from .env by default; we force `_env_file=None` to make
    the test deterministic regardless of any .env present in the worktree.
    """
    # patch.dict + Settings() with no env_file = pure-env construction.
    full_env = {
        "DATABASE_URL": "postgresql+asyncpg://bima:x@postgres:5432/bima_ai",
        "INTERNAL_API_KEY": "test",
        "SECRET_KEY": "test-secret",
        **env,
    }
    with patch.dict("os.environ", full_env, clear=True):
        return Settings(_env_file=None)  # type: ignore[call-arg]


class CorsOriginsTests(unittest.TestCase):
    def test_default_includes_both_apex(self) -> None:
        """With default env, cors_origins includes BOTH nolongin.com and
        bimaptsp.com — the whole point of the migration scaffolding.
        """
        s = _make_settings()
        origins = s.cors_origins
        # Legacy
        self.assertIn("https://admin.nolongin.com", origins)
        self.assertIn("https://portal.nolongin.com", origins)
        # Target
        self.assertIn("https://admin.bimaptsp.com", origins)
        self.assertIn("https://portal.bimaptsp.com", origins)

    def test_admin_frontend_url_takes_precedence_in_ordering(self) -> None:
        """A dev's localhost (ADMIN_FRONTEND_URL) should stay at the front
        even when CORS_ALLOW_ORIGINS adds prod domains. Order matters for
        readability in logs, not for CORS semantics.
        """
        s = _make_settings(
            ADMIN_FRONTEND_URL="http://localhost:3000",
            CORS_ALLOW_ORIGINS="https://admin.bimaptsp.com",
        )
        self.assertEqual(s.cors_origins[0], "http://localhost:3000")
        self.assertIn("https://admin.bimaptsp.com", s.cors_origins)

    def test_dedup_across_both_vars(self) -> None:
        """If the same origin appears in BOTH env vars, it should appear
        once. Trailing slashes are normalized away before dedup.
        """
        s = _make_settings(
            ADMIN_FRONTEND_URL="https://admin.bimaptsp.com/",
            CORS_ALLOW_ORIGINS="https://admin.bimaptsp.com",
        )
        # Exactly one entry, normalized (no trailing slash).
        self.assertEqual(
            s.cors_origins.count("https://admin.bimaptsp.com"), 1
        )

    def test_empty_entries_are_skipped(self) -> None:
        """Extra commas (e.g. trailing in CORS_ALLOW_ORIGINS) must not
        introduce empty origin strings — those would fail CORS preflight.
        """
        s = _make_settings(
            ADMIN_FRONTEND_URL="",
            CORS_ALLOW_ORIGINS=",https://admin.bimaptsp.com,,",
        )
        self.assertEqual(s.cors_origins, ["https://admin.bimaptsp.com"])


class TrustedHostsTests(unittest.TestCase):
    def test_default_lists_both_apex_hostnames(self) -> None:
        """The default TRUSTED_HOSTS should cover every bare hostname
        admin-api could be addressed as during the migration.
        """
        s = _make_settings()
        hosts = s.trusted_hosts
        # Legacy
        self.assertIn("nolongin.com", hosts)
        self.assertIn("admin.nolongin.com", hosts)
        self.assertIn("portal.nolongin.com", hosts)
        # Target
        self.assertIn("bimaptsp.com", hosts)
        self.assertIn("admin.bimaptsp.com", hosts)
        self.assertIn("portal.bimaptsp.com", hosts)
        # In-cluster defaults still present.
        self.assertIn("admin-api", hosts)
        self.assertIn("localhost", hosts)
        self.assertIn("127.0.0.1", hosts)

    def test_explicit_trusted_hosts_are_merged(self) -> None:
        s = _make_settings(
            ADMIN_FRONTEND_URL="https://admin.bimaptsp.com",
            CORS_ALLOW_ORIGINS="",
            TRUSTED_HOSTS="extra.bimaptsp.com,another.example",
        )
        self.assertIn("admin.bimaptsp.com", s.trusted_hosts)  # from cors
        self.assertIn("extra.bimaptsp.com", s.trusted_hosts)
        self.assertIn("another.example", s.trusted_hosts)

    def test_hosts_are_deduped(self) -> None:
        """Hostnames extracted from cors_origins AND listed in TRUSTED_HOSTS
        must appear once, not twice.
        """
        s = _make_settings(
            ADMIN_FRONTEND_URL="https://admin.bimaptsp.com",
            CORS_ALLOW_ORIGINS="",
            TRUSTED_HOSTS="admin.bimaptsp.com",
        )
        self.assertEqual(s.trusted_hosts.count("admin.bimaptsp.com"), 1)


class PortalAndSiapBaseTests(unittest.TestCase):
    """Smoke tests for the new cross-service deep-link config fields. These
    don't drive code paths in admin-api today, but the defaults must match
    what ai-engine ships with — otherwise a single .env push on cutover day
    risks splitting the two services across two domains.
    """

    def test_portal_track_url_base_default_is_legacy(self) -> None:
        s = _make_settings()
        self.assertEqual(
            s.PORTAL_TRACK_URL_BASE, "https://portal.nolongin.com/track"
        )

    def test_siap_signing_url_base_default_is_beta(self) -> None:
        s = _make_settings()
        self.assertEqual(
            s.SIAP_SIGNING_URL_BASE, "https://beta-siap.nolongin.com"
        )


if __name__ == "__main__":
    unittest.main()
