"""
Tests for the FLOW-BASED officer notification model.

Covers the SIAP "Alur Izin" step-flow resolution and its wiring into the
officer chat bridge:

  * services.siap_db.resolve_step_officers
      - returns the assignee(s) for a wa-ACTIVE OFFICER step
      - returns empty + is_applicant_step=True for a 'Pemohon Online' step
      - returns empty when the step's wa_notification != 'ACTIVE'
      - never raises: empty result when the SIAP DB is unconfigured / errors
      - normalizes stored numbers (08…/62…/+62…) to APTANA's 62… inbound form

  * services.officer_bridge.notify_officer_of_submission
      - registers a session + sends the brief PER resolved officer
      - falls back to BIMA_OFFICER_WA_PHONE when flow resolution is empty

  * services.officer_bridge.is_officer_channel_id (SYNC)
      - matches a cached officer number under 08/62/+62 equivalence
      - matches the env BIMA_OFFICER_WA_PHONE
      - returns False when the bridge flag is off

Run standalone (no pytest — matches tests/test_officer_bridge.py):

    python3 tests/test_officer_flow_notify.py

No real network, no real Postgres. The asyncpg pool is replaced with an
in-memory fake that answers each SQL by substring; senders are patched.
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _ensure_stub(name: str, attrs: dict | None = None) -> None:
    if name in sys.modules:
        return
    try:
        __import__(name)
        return
    except ImportError:
        pass
    mod = types.ModuleType(name)
    for attr, value in (attrs or {}).items():
        setattr(mod, attr, value)
    sys.modules[name] = mod


_ensure_stub("asyncpg")
_ensure_stub("dotenv", {"load_dotenv": lambda *a, **k: None})
_ensure_stub("httpx")
# officer_bridge imports `services.agents.officer_copilot.get_copilot` lazily
# (inside maybe_handle_officer_reply), so this suite never touches it. Rather
# than stubbing the whole copilot module — which would poison sys.modules for a
# sibling suite in the same process (test_officer_copilot_docsend needs the real
# `_doc_context`/`send_document`) — we stub only its heavy leaf deps so the REAL
# module can import cleanly if any test does reach it. Same set the other
# officer suites use, so all three stay consistent regardless of run order.
_ensure_stub("services.rag_service", {"query_regulations": lambda *a, **k: []})
_ensure_stub("services.agents.validator", {"_normalize_name": lambda s: s})

from services import officer_bridge as ob  # noqa: E402
from services import siap_db  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# In-memory fake asyncpg pool. resolve_step_officers issues, in order:
#   1) _SQL_REQUEST_STEP   (fetchrow)  → approval_step_id, license_id
#   2) _SQL_APPROVAL_STEP  (fetchrow)  → group_id, sort_order, wa_notification
#   3) _SQL_ROLE_NAME      (fetchrow)  → name
#   4) _SQL_STEP_OFFICER_WHATSAPPS (fetch) → whatsapp rows
# We answer each by matching a stable substring of the SQL text.
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, scenario: dict):
        self.s = scenario

    async def fetchrow(self, sql, *args):
        # get_request_case_meta reads license_request (profile_id/license_id/
        # approval_step_id/ticket) — distinguish from resolve_step_officers'
        # request query by the selected `profile_id` column.
        if "FROM ptsp.license_request" in sql and "profile_id" in sql:
            return self.s.get("case_request_row")
        if "FROM ptsp.license_request" in sql:
            return self.s.get("request_row")
        # get_request_case_meta's step+max query (CROSS JOIN LATERAL) vs
        # resolve_step_officers' plain approval-step query.
        if "FROM ptsp.license_approval_step" in sql and "max_sort_order" in sql:
            return self.s.get("step_max_row")
        if "FROM ptsp.license_approval_step" in sql:
            return self.s.get("step_row")
        if "FROM public.roles" in sql:
            return self.s.get("role_row")
        raise AssertionError(f"unexpected fetchrow SQL: {sql[:60]}")

    async def fetch(self, sql, *args):
        if "FROM scmcoresystem.tblroleizin" in sql:
            return self.s.get("officer_rows", [])
        if "FROM ptsp.profile_requirements" in sql:
            return self.s.get("doc_rows", [])
        if "FROM public.users u" in sql:  # officer directory
            return self.s.get("directory_rows", [])
        raise AssertionError(f"unexpected fetch SQL: {sql[:60]}")


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, scenario: dict):
        self._conn = _FakeConn(scenario)

    def acquire(self):
        return _AcquireCtx(self._conn)


def _install_pool(scenario: dict):
    """Patch siap_db so it's 'configured' and returns our fake pool."""
    return patch.multiple(
        siap_db,
        is_siap_db_configured=lambda: True,
        get_siap_pool=AsyncMock(return_value=_FakePool(scenario)),
    )


# ---------------------------------------------------------------------------
# resolve_step_officers
# ---------------------------------------------------------------------------


class TestResolveStepOfficers(unittest.TestCase):
    def test_officer_step_wa_active_resolves_assignees(self):
        scenario = {
            "request_row": {"approval_step_id": 5001, "license_id": 459},
            "step_row": {"group_id": 6, "sort_order": 1, "wa_notification": "ACTIVE"},
            "role_row": {"name": "Petugas SKPD"},
            # One stored as 62…, one as 08… → both normalize to 62…
            "officer_rows": [
                {"whatsapp": "6285117557091"},
                {"whatsapp": "08123456789"},
            ],
        }
        with _install_pool(scenario):
            out = _run(siap_db.resolve_step_officers(777))
        self.assertTrue(out["wa_active"])
        self.assertFalse(out["is_applicant_step"])
        self.assertEqual(out["group_id"], 6)
        self.assertEqual(out["sort_order"], 1)
        self.assertEqual(
            out["officer_whatsapps"], ["6285117557091", "628123456789"]
        )

    def test_applicant_step_returns_empty_and_flags_applicant(self):
        scenario = {
            "request_row": {"approval_step_id": 5000, "license_id": 459},
            # sort_order 0 = applicant anchor; even though wa is ACTIVE, this is
            # the CITIZEN step → no officer numbers.
            "step_row": {"group_id": 99, "sort_order": 0, "wa_notification": "ACTIVE"},
            "role_row": {"name": "Pemohon Online"},
            "officer_rows": [{"whatsapp": "6285117557091"}],  # must be ignored
        }
        with _install_pool(scenario):
            out = _run(siap_db.resolve_step_officers(778))
        self.assertTrue(out["wa_active"])
        self.assertTrue(out["is_applicant_step"])
        self.assertEqual(out["officer_whatsapps"], [])
        self.assertEqual(out["group_id"], 99)

    def test_applicant_role_name_match_is_case_insensitive(self):
        scenario = {
            "request_row": {"approval_step_id": 5000, "license_id": 459},
            "step_row": {"group_id": 99, "sort_order": 4, "wa_notification": "ACTIVE"},
            "role_row": {"name": "  PEMOHON ONLINE  "},  # padded + upper
            "officer_rows": [],
        }
        with _install_pool(scenario):
            out = _run(siap_db.resolve_step_officers(779))
        self.assertTrue(out["is_applicant_step"])
        self.assertEqual(out["officer_whatsapps"], [])

    def test_non_active_wa_returns_empty(self):
        scenario = {
            "request_row": {"approval_step_id": 5002, "license_id": 459},
            "step_row": {"group_id": 7, "sort_order": 2, "wa_notification": "NON-ACTIVE"},
            "role_row": {"name": "Kabid Perikanan Tangkap"},
            "officer_rows": [{"whatsapp": "6285117557091"}],  # must be ignored
        }
        with _install_pool(scenario):
            out = _run(siap_db.resolve_step_officers(780))
        self.assertFalse(out["wa_active"])
        self.assertFalse(out["is_applicant_step"])
        self.assertEqual(out["officer_whatsapps"], [])
        self.assertEqual(out["group_id"], 7)

    def test_null_wa_notification_returns_empty(self):
        scenario = {
            "request_row": {"approval_step_id": 5003, "license_id": 459},
            "step_row": {"group_id": 8, "sort_order": 3, "wa_notification": None},
            "role_row": {"name": "Kadin Perikanan"},
            "officer_rows": [{"whatsapp": "6285117557091"}],
        }
        with _install_pool(scenario):
            out = _run(siap_db.resolve_step_officers(781))
        self.assertFalse(out["wa_active"])
        self.assertEqual(out["officer_whatsapps"], [])

    def test_unconfigured_db_returns_safe_empty(self):
        with patch.object(siap_db, "is_siap_db_configured", lambda: False):
            out = _run(siap_db.resolve_step_officers(1))
        self.assertEqual(out["officer_whatsapps"], [])
        self.assertFalse(out["wa_active"])
        self.assertFalse(out["is_applicant_step"])
        self.assertIsNone(out["group_id"])

    def test_request_not_found_returns_empty(self):
        scenario = {"request_row": None}
        with _install_pool(scenario):
            out = _run(siap_db.resolve_step_officers(999))
        self.assertEqual(out["officer_whatsapps"], [])
        self.assertIsNone(out["group_id"])

    def test_db_error_never_raises(self):
        # A pool that raises on acquire must NOT bubble out of resolve.
        broken = AsyncMock(side_effect=RuntimeError("siap down"))
        with patch.multiple(
            siap_db,
            is_siap_db_configured=lambda: True,
            get_siap_pool=broken,
        ):
            out = _run(siap_db.resolve_step_officers(5))
        self.assertEqual(out["officer_whatsapps"], [])
        self.assertFalse(out["wa_active"])


class TestNormalizeMsisdn(unittest.TestCase):
    def test_forms_collapse_to_62(self):
        self.assertEqual(siap_db._normalize_msisdn("08123456789"), "628123456789")
        self.assertEqual(siap_db._normalize_msisdn("+62 851 1755 7091"), "6285117557091")
        self.assertEqual(siap_db._normalize_msisdn("6285117557091"), "6285117557091")
        # Bare local form (no 0, no 62) gets a 62 prefix.
        self.assertEqual(siap_db._normalize_msisdn("851-1755-7091"), "6285117557091")
        self.assertEqual(siap_db._normalize_msisdn(""), "")
        self.assertEqual(siap_db._normalize_msisdn(None), "")


# ---------------------------------------------------------------------------
# notify_officer_of_submission — flow path + env fallback
# ---------------------------------------------------------------------------


class TestNotifyFlow(unittest.TestCase):
    def setUp(self):
        ob._sessions.clear()
        ob._reset_officer_cache()

    def test_registers_session_and_sends_per_resolved_officer(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "",  # no env fallback → flow must drive
            "BIMA_OFFICER_TG_CHAT": "",
        }
        resolution = {
            "wa_active": True,
            "is_applicant_step": False,
            "officer_whatsapps": ["6285117557091", "628123456789"],
            "group_id": 6,
            "sort_order": 1,
        }
        with patch.dict("os.environ", env, clear=False):
            with patch.object(
                ob, "_send_officer_notify", new=AsyncMock(return_value=True)
            ) as send:
                with patch(
                    "services.siap_db.resolve_step_officers",
                    new=AsyncMock(return_value=resolution),
                ):
                    sent = _run(ob.notify_officer_of_submission(
                        ticket="000123456", request_id=777, license_id=459,
                        license_name="PKPP", applicant_name="Budi Santoso",
                        score=None, documents=[],
                    ))
        self.assertTrue(sent)
        # A session per resolved officer, keyed by the normalized number.
        self.assertIn("6285117557091", ob._sessions)
        self.assertIn("628123456789", ob._sessions)
        self.assertEqual(ob._sessions["6285117557091"].channel, ob.CHANNEL_WHATSAPP)
        self.assertEqual(ob._sessions["6285117557091"].ticket, "000123456")
        # Brief sent to BOTH officers on WhatsApp.
        self.assertEqual(send.await_count, 2)
        targets = {call.args[1] for call in send.await_args_list}
        self.assertEqual(targets, {"6285117557091", "628123456789"})

    def test_resolved_numbers_unioned_into_officer_cache(self):
        # FIX 2: a just-resolved officer must be recognizable by the SYNC
        # is_officer_channel_id immediately — even with a cold/stale cache and
        # no env fallback — because notify unions the resolved numbers into the
        # same _officer_cache set the predicate reads. (Without this, an officer
        # whose roster row changed since the last 5-min refresh would fall
        # through to the citizen path on their first reply.)
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        resolution = {
            "wa_active": True,
            "is_applicant_step": False,
            "officer_whatsapps": ["6285117557091", "628123456789"],
            "group_id": 6,
            "sort_order": 1,
        }
        # Start cold: cache empty, but marked loaded+fresh so the sync predicate
        # answers from the (now-augmented) set without kicking a refresh.
        ob._reset_officer_cache()
        import time as _t
        ob._officer_cache_loaded = True
        ob._officer_cache_at = _t.monotonic()
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ob, "_send_officer_notify", new=AsyncMock(return_value=True)):
                with patch(
                    "services.siap_db.resolve_step_officers",
                    new=AsyncMock(return_value=resolution),
                ):
                    _run(ob.notify_officer_of_submission(
                        ticket="000123456", request_id=777, license_id=459,
                        license_name="PKPP", applicant_name="Budi",
                        score=None, documents=[],
                    ))
        # Both resolved numbers are now in the cache directory set...
        self.assertEqual(ob._officer_cache, {"6285117557091", "628123456789"})
        # ...so the SYNC predicate recognizes them on a first reply, including
        # under 08…/+62… equivalence.
        with patch.dict("os.environ", env, clear=False):
            self.assertTrue(ob.is_officer_channel_id("6285117557091"))
            self.assertTrue(ob.is_officer_channel_id("085117557091"))
            self.assertTrue(ob.is_officer_channel_id("628123456789"))
            # A number that was NOT resolved is still not an officer.
            self.assertFalse(ob.is_officer_channel_id("628000111222"))

    def test_resolved_union_preserves_existing_cache_entries(self):
        # FIX 2 (additive, not replace): unioning the resolved numbers must NOT
        # drop numbers already warmed in the cache by the startup refresh.
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        resolution = {
            "wa_active": True, "is_applicant_step": False,
            "officer_whatsapps": ["628123456789"], "group_id": 6, "sort_order": 1,
        }
        ob._reset_officer_cache()
        ob._officer_cache = {"6280000000000"}  # pre-warmed entry
        import time as _t
        ob._officer_cache_loaded = True
        ob._officer_cache_at = _t.monotonic()
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ob, "_send_officer_notify", new=AsyncMock(return_value=True)):
                with patch(
                    "services.siap_db.resolve_step_officers",
                    new=AsyncMock(return_value=resolution),
                ):
                    _run(ob.notify_officer_of_submission(
                        ticket="1", request_id=777, license_id=459,
                        license_name="PKPP", applicant_name="A",
                        score=None, documents=[],
                    ))
        self.assertEqual(ob._officer_cache, {"6280000000000", "628123456789"})

    def test_falls_back_to_env_when_resolution_empty(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        empty = {
            "wa_active": False,
            "is_applicant_step": False,
            "officer_whatsapps": [],
            "group_id": 7,
            "sort_order": 2,
        }
        with patch.dict("os.environ", env, clear=False):
            with patch.object(
                ob, "_send_officer_notify", new=AsyncMock(return_value=True)
            ) as send:
                with patch(
                    "services.siap_db.resolve_step_officers",
                    new=AsyncMock(return_value=empty),
                ):
                    sent = _run(ob.notify_officer_of_submission(
                        ticket="789", request_id=780, license_id=459,
                        license_name="PKPP", applicant_name="A",
                        score=None, documents=[],
                    ))
        self.assertTrue(sent)
        # Session keyed by the env fallback number (normalized).
        self.assertIn("628999000111", ob._sessions)
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[1], "628999000111")

    def test_no_resolution_no_env_is_noop(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        empty = {
            "wa_active": True, "is_applicant_step": True,
            "officer_whatsapps": [], "group_id": 99, "sort_order": 0,
        }
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ob, "_send_officer_notify", new=AsyncMock(return_value=True)) as send:
                with patch(
                    "services.siap_db.resolve_step_officers",
                    new=AsyncMock(return_value=empty),
                ):
                    sent = _run(ob.notify_officer_of_submission(
                        ticket="1", request_id=778, license_id=459,
                        license_name="PKPP", applicant_name="A",
                        score=None, documents=[],
                    ))
        self.assertFalse(sent)
        self.assertEqual(len(ob._sessions), 0)
        send.assert_not_awaited()


# ---------------------------------------------------------------------------
# is_officer_channel_id — SYNC, data-driven cache + env, normalization
# ---------------------------------------------------------------------------


class TestIsOfficerChannelId(unittest.TestCase):
    def setUp(self):
        ob._sessions.clear()
        ob._reset_officer_cache()

    def test_matches_cached_officer_under_number_equivalence(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        # Warm the cache with the canonical 62… form, then probe with 08…/+62…
        # variants of the SAME number — all must match. Mark loaded+fresh so the
        # sync predicate doesn't try to kick a refresh.
        ob._officer_cache = {"6285117557091"}
        ob._officer_cache_loaded = True
        import time as _t
        ob._officer_cache_at = _t.monotonic()
        with patch.dict("os.environ", env, clear=False):
            self.assertTrue(ob.is_officer_channel_id("6285117557091"))
            self.assertTrue(ob.is_officer_channel_id("085117557091"))
            self.assertTrue(ob.is_officer_channel_id("+62 851 1755 7091"))
            # A different number is NOT an officer.
            self.assertFalse(ob.is_officer_channel_id("628000111222"))

    def test_matches_env_fallback_number(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        ob._officer_cache = set()  # empty cache → only env can match
        ob._officer_cache_loaded = True
        import time as _t
        ob._officer_cache_at = _t.monotonic()
        with patch.dict("os.environ", env, clear=False):
            self.assertTrue(ob.is_officer_channel_id("628999000111"))
            # Equivalent 08… form of the env number also matches.
            self.assertTrue(ob.is_officer_channel_id("08999000111"))
            self.assertFalse(ob.is_officer_channel_id("628000111222"))

    def test_false_when_flag_off(self):
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "false",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        ob._officer_cache = {"6285117557091"}
        ob._officer_cache_loaded = True
        import time as _t
        ob._officer_cache_at = _t.monotonic()
        with patch.dict("os.environ", env, clear=False):
            self.assertFalse(ob.is_officer_channel_id("628999000111"))
            self.assertFalse(ob.is_officer_channel_id("6285117557091"))

    def test_sync_predicate_does_not_block_or_raise_without_loop(self):
        # Called outside an event loop (pure sync), with a stale/unloaded cache:
        # it must NOT raise and must NOT block — _maybe_kick_officer_refresh
        # silently no-ops when there's no running loop.
        env = {
            "BIMA_OFFICER_NOTIFY_ENABLED": "true",
            "BIMA_OFFICER_WA_PHONE": "628999000111",
            "BIMA_OFFICER_TG_CHAT": "",
        }
        ob._reset_officer_cache()  # unloaded + stale
        with patch.dict("os.environ", env, clear=False):
            # No exception; env number still resolves from the env branch.
            self.assertTrue(ob.is_officer_channel_id("628999000111"))
            self.assertFalse(ob.is_officer_channel_id("628000111222"))


# ---------------------------------------------------------------------------
# warm_officer_cache → get_officer_directory (non-blocking refresh plumbing)
# ---------------------------------------------------------------------------


class TestOfficerCacheWarm(unittest.TestCase):
    def setUp(self):
        ob._reset_officer_cache()

    def test_warm_populates_cache_from_directory(self):
        with patch(
            "services.siap_db.get_officer_directory",
            new=AsyncMock(return_value=["6285117557091", "628123456789"]),
        ):
            _run(ob.warm_officer_cache())
        self.assertEqual(ob._officer_cache, {"6285117557091", "628123456789"})
        self.assertTrue(ob._officer_cache_loaded)

    def test_warm_failure_keeps_last_known_set(self):
        ob._officer_cache = {"6285117557091"}
        ob._officer_cache_loaded = True
        with patch(
            "services.siap_db.get_officer_directory",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            _run(ob.warm_officer_cache())  # must not raise
        # Last-known set preserved.
        self.assertEqual(ob._officer_cache, {"6285117557091"})


# ---------------------------------------------------------------------------
# SIAP-grounded case reads (get_request_case_meta / get_submission_doc_refs)
# ---------------------------------------------------------------------------


class TestRequestCaseMeta(unittest.TestCase):
    def test_resolves_step_and_flags_final(self):
        scenario = {
            "case_request_row": {
                "profile_id": 321, "license_id": 459,
                "approval_step_id": 5099, "ticket": "000077591",
            },
            # current sort_order == max → LAST step
            "step_max_row": {
                "current_sort_order": 4, "group_id": 12,
                "stereotype": "LICENSE-RECOMMEND", "max_sort_order": 4,
            },
            "role_row": {"name": "Kepala Dinas"},
        }
        with _install_pool(scenario):
            out = _run(siap_db.get_request_case_meta(900))
        self.assertTrue(out["found"])
        self.assertEqual(out["profile_id"], 321)
        self.assertEqual(out["license_id"], 459)
        self.assertEqual(out["ticket"], "000077591")
        self.assertTrue(out["is_final_step"])
        self.assertEqual(out["owner_desk"], "Kepala Dinas")
        self.assertEqual(out["stereotype"], "LICENSE-RECOMMEND")

    def test_intermediate_step_is_not_final(self):
        scenario = {
            "case_request_row": {
                "profile_id": 1, "license_id": 459,
                "approval_step_id": 5001, "ticket": "000000123",
            },
            "step_max_row": {
                "current_sort_order": 1, "group_id": 6,
                "stereotype": None, "max_sort_order": 4,
            },
            "role_row": {"name": "Petugas SKPD"},
        }
        with _install_pool(scenario):
            out = _run(siap_db.get_request_case_meta(901))
        self.assertTrue(out["found"])
        self.assertFalse(out["is_final_step"])

    def test_missing_request_returns_safe_empty(self):
        scenario = {"case_request_row": None}
        with _install_pool(scenario):
            out = _run(siap_db.get_request_case_meta(902))
        self.assertFalse(out["found"])
        self.assertIsNone(out["ticket"])

    def test_unconfigured_db_returns_empty(self):
        with patch.object(siap_db, "is_siap_db_configured", lambda: False):
            out = _run(siap_db.get_request_case_meta(1))
        self.assertFalse(out["found"])

    def test_db_error_never_raises(self):
        broken = AsyncMock(side_effect=RuntimeError("siap down"))
        with patch.multiple(
            siap_db,
            is_siap_db_configured=lambda: True,
            get_siap_pool=broken,
        ):
            out = _run(siap_db.get_request_case_meta(5))
        self.assertFalse(out["found"])


class TestSubmissionDocRefs(unittest.TestCase):
    def test_returns_only_rows_with_a_file(self):
        scenario = {
            "doc_rows": [
                {"requirements_id": 10, "file_ref": "berkas/ktp.pdf",
                 "req_status": "OK", "requirement_name": "Fotokopi KTP"},
                # A blank file_ref must be dropped (SQL guards, but the loader
                # also strips defensively).
                {"requirements_id": 11, "file_ref": "  ",
                 "req_status": None, "requirement_name": "NIB"},
            ],
        }
        with _install_pool(scenario):
            out = _run(siap_db.get_submission_doc_refs(321))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["requirement_name"], "Fotokopi KTP")
        self.assertEqual(out[0]["file_ref"], "berkas/ktp.pdf")

    def test_empty_when_no_docs(self):
        with _install_pool({"doc_rows": []}):
            out = _run(siap_db.get_submission_doc_refs(321))
        self.assertEqual(out, [])

    def test_db_error_never_raises(self):
        broken = AsyncMock(side_effect=RuntimeError("down"))
        with patch.multiple(
            siap_db,
            is_siap_db_configured=lambda: True,
            get_siap_pool=broken,
        ):
            out = _run(siap_db.get_submission_doc_refs(321))
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
