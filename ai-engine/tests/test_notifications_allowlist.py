"""
Tests for the URL allowlist guard in `services/notifications.py`.

Scope: the `_validate_param_urls` security guard (security review finding
N3, 2026-05-21) — it rejects template params that smuggle a URL whose host
isn't BIMA-owned. The domain-migration PR adds bimaptsp.com to the allow-
list so that flipping PORTAL_TRACK_URL_BASE during the cutover doesn't
suddenly cause every outbound citizen_progress to be rejected.

Why a dedicated test: a regression here is silent — `notify()` would log a
warning and return False, and a busy on-call would see "notifications not
firing" without realising the allowlist became misaligned with the env
var. Pinning both sides in tests catches it on PR.

Run standalone (matches the project's test-script convention — no pytest):

    python -m tests.test_notifications_allowlist   # from ai-engine/
    python tests/test_notifications_allowlist.py   # also works
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Make `services` importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# `services.notifications` imports services.whatsapp_sender +
# services.whatsapp_template at module load, which in turn import httpx.
# That's all fine on a dev machine — we don't need to stub those because
# the test never calls `notify()`, only the pure validator.
from services.notifications import (  # noqa: E402
    _ALLOWED_URL_HOSTS,
    _validate_param_urls,
)


class AllowlistMembershipTests(unittest.TestCase):
    """Pin the exact set of hosts the dispatcher trusts. Adding/removing
    one without updating this test should fail loudly — the allowlist is
    a security boundary, not an internal detail.
    """

    def test_legacy_hosts_present(self) -> None:
        # During the migration window we must still accept the old apex,
        # otherwise in-flight WhatsApp links break.
        self.assertIn("nolongin.com", _ALLOWED_URL_HOSTS)
        self.assertIn("portal.nolongin.com", _ALLOWED_URL_HOSTS)
        self.assertIn("beta-siap.nolongin.com", _ALLOWED_URL_HOSTS)

    def test_target_hosts_present(self) -> None:
        # The whole point of this PR: bimaptsp.com must be trusted BEFORE
        # the env var flips, so the cutover doesn't 4xx every notification.
        self.assertIn("bimaptsp.com", _ALLOWED_URL_HOSTS)
        self.assertIn("portal.bimaptsp.com", _ALLOWED_URL_HOSTS)
        self.assertIn("beta-siap.bimaptsp.com", _ALLOWED_URL_HOSTS)

    def test_siap_production_host_present(self) -> None:
        # Unrelated to the migration, but in scope for the guard.
        self.assertIn("perizinan.jatengprov.go.id", _ALLOWED_URL_HOSTS)


class ValidatorBehaviorTests(unittest.TestCase):
    def test_target_apex_url_passes(self) -> None:
        params = {
            "name": "Budi",
            "ticket": "000077591",
            "fix_url": "https://portal.bimaptsp.com/track/000077591",
        }
        self.assertIsNone(_validate_param_urls(params))

    def test_legacy_apex_url_passes(self) -> None:
        params = {
            "fix_url": "https://portal.nolongin.com/track/000077591",
        }
        self.assertIsNone(_validate_param_urls(params))

    def test_lookalike_host_is_rejected(self) -> None:
        # Classic phishing-relay vector — registrar-flexible lookalike that
        # ISN'T owned by DPMPTSP. Must NOT pass even though the substring
        # 'bimaptsp.com' appears in the URL.
        params = {
            "fix_url": "https://bimaptsp.com.attacker.example/login",
        }
        err = _validate_param_urls(params)
        self.assertIsNotNone(err)
        self.assertIn("non-allowlisted URL host", err or "")

    def test_unknown_host_is_rejected(self) -> None:
        params = {"fix_url": "https://random.example/x"}
        err = _validate_param_urls(params)
        self.assertIsNotNone(err)

    def test_trailing_punctuation_does_not_poison_host(self) -> None:
        # Mirrors the existing guard's `rstrip(").,;'\"")` behavior — a URL
        # at end-of-sentence in a template body must still parse cleanly.
        params = {
            "fix_url": "Cek di https://portal.bimaptsp.com/track/123.",
        }
        self.assertIsNone(_validate_param_urls(params))


if __name__ == "__main__":
    unittest.main()
