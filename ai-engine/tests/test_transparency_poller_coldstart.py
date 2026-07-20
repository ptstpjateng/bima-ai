"""
Tests for the transparency poller's cold-start durability + freshness gate.

Regression for the 2026-07-20 incident: a `docker compose up -d --build`
recreated ai-engine, whose `/app/data` was NOT on a volume, wiping the cursor
to 0. The old cold-start guard skipped only the first page, so the poller then
crawled a 310k-row backlog at 50 rows/tick (~4.4 days) — and any backlog row
that classified as sendable would have been pushed to a live citizen.

Two defences are tested here:

  * `_change_is_fresh` / the `_process_change` freshness gate — a stale change
    (or one with no parseable `changed_at`) is NEVER notified, however the
    cursor reached it. FAIL CLOSED.
  * `_seek_head` + the `_poll_once` cold-start branch — a since==0 cursor jumps
    straight to max(log_id) in one query instead of crawling the backlog.

Run standalone (no pytest — matches the other officer/notify suites):

    python3 tests/test_transparency_poller_coldstart.py

No real network, no real Postgres — the DB pool and senders are patched.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import transparency_poller as tp  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class TestParseChangedAt(unittest.TestCase):
    def test_iso_with_z(self):
        dt = tp._parse_changed_at("2026-07-20T15:07:51Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.hour, 15)

    def test_iso_with_offset(self):
        dt = tp._parse_changed_at("2026-07-20T15:07:51+00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.hour, 15)

    def test_fractional_seconds_and_offset(self):
        dt = tp._parse_changed_at("2026-07-20 15:07:51.845194+00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.minute, 7)

    def test_naive_space_form_assumed_utc(self):
        dt = tp._parse_changed_at("2026-07-20 15:07:51")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_non_utc_offset_normalised_to_utc(self):
        # +07:00 local 22:07 == 15:07 UTC
        dt = tp._parse_changed_at("2026-07-20T22:07:51+07:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.hour, 15)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_garbage_is_none(self):
        for bad in (None, "", "   ", "not-a-date", "20/07/2026", 12345):
            self.assertIsNone(tp._parse_changed_at(bad), bad)


class TestChangeIsFresh(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 20, 15, 0, 0, tzinfo=timezone.utc)

    def test_recent_change_is_fresh(self):
        c = {"changed_at": _iso(self.now - timedelta(hours=1))}
        self.assertTrue(tp._change_is_fresh(c, now=self.now))

    def test_old_change_is_stale(self):
        c = {"changed_at": _iso(self.now - timedelta(hours=48))}
        self.assertFalse(tp._change_is_fresh(c, now=self.now))

    def test_boundary_just_inside_window(self):
        c = {"changed_at": _iso(self.now - timedelta(hours=23, minutes=59))}
        self.assertTrue(tp._change_is_fresh(c, now=self.now))

    def test_missing_timestamp_fails_closed(self):
        self.assertFalse(tp._change_is_fresh({}, now=self.now))

    def test_unparseable_timestamp_fails_closed(self):
        self.assertFalse(tp._change_is_fresh({"changed_at": "soon"}, now=self.now))

    def test_future_timestamp_counts_as_fresh(self):
        # Clock skew: SIAP a little ahead of ai-engine must not drop a live event.
        c = {"changed_at": _iso(self.now + timedelta(minutes=5))}
        self.assertTrue(tp._change_is_fresh(c, now=self.now))


class TestProcessChangeFreshnessGate(unittest.TestCase):
    """A stale change must never reach notify(); a fresh one must."""

    def _completed_change(self, *, age_hours: float) -> dict:
        ts = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        return {
            "log_id": 316163,
            "profile_id": 42,
            "request_id": 77774,
            "license_id": 459,
            "status": "SELESAI",
            "stereotype": "",
            "description": "",
            "current_approval_step_id": None,  # final → citizen_completed
            "changed_at": _iso(ts),
        }

    def test_stale_completed_change_is_not_sent(self):
        notify = AsyncMock(return_value=True)
        person = AsyncMock(return_value={"full_name": "Test", "mobile": "08123456789"})
        tick = AsyncMock(return_value=("000077774", "PKPP"))
        with patch.object(tp, "notify", notify), \
             patch.object(tp, "_lookup_person", person), \
             patch.object(tp, "_resolve_ticket_and_license", tick):
            _run(tp._process_change(self._completed_change(age_hours=72)))
        notify.assert_not_awaited()
        # Gate fires BEFORE any DB lookup — cheap skip.
        person.assert_not_awaited()

    def test_fresh_completed_change_is_sent(self):
        notify = AsyncMock(return_value=True)
        person = AsyncMock(return_value={"full_name": "Test", "mobile": "08123456789"})
        tick = AsyncMock(return_value=("000077774", "PKPP"))
        with patch.object(tp, "notify", notify), \
             patch.object(tp, "_lookup_person", person), \
             patch.object(tp, "_resolve_ticket_and_license", tick):
            _run(tp._process_change(self._completed_change(age_hours=0.05)))
        notify.assert_awaited_once()
        self.assertEqual(notify.await_args.kwargs["event"], "citizen_completed")

    def test_missing_changed_at_is_not_sent(self):
        notify = AsyncMock(return_value=True)
        change = self._completed_change(age_hours=0.05)
        change.pop("changed_at")
        with patch.object(tp, "notify", notify), \
             patch.object(tp, "_lookup_person", AsyncMock()), \
             patch.object(tp, "_resolve_ticket_and_license", AsyncMock()):
            _run(tp._process_change(change))
        notify.assert_not_awaited()


class _FakeConn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, *a, **k):
        return self._row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, row):
        self._row = row

    def acquire(self):
        return _FakeConn(self._row)


class TestSeekHead(unittest.TestCase):
    def test_returns_head_when_configured(self):
        with patch.object(tp, "is_siap_db_configured", return_value=True), \
             patch.object(tp, "get_siap_pool", AsyncMock(return_value=_FakePool({"head": 316163}))):
            head = _run(tp._seek_head())
        self.assertEqual(head, 316163)

    def test_none_when_db_unconfigured(self):
        with patch.object(tp, "is_siap_db_configured", return_value=False):
            self.assertIsNone(_run(tp._seek_head()))

    def test_none_when_table_empty(self):
        with patch.object(tp, "is_siap_db_configured", return_value=True), \
             patch.object(tp, "get_siap_pool", AsyncMock(return_value=_FakePool({"head": None}))):
            self.assertIsNone(_run(tp._seek_head()))

    def test_none_on_db_error(self):
        with patch.object(tp, "is_siap_db_configured", return_value=True), \
             patch.object(tp, "get_siap_pool", AsyncMock(side_effect=RuntimeError("boom"))):
            self.assertIsNone(_run(tp._seek_head()))


class TestPollOnceColdStart(unittest.TestCase):
    """since==0 must fast-forward to head and NOT fetch/replay the backlog."""

    def test_cold_start_jumps_to_head_and_skips_fetch(self):
        save = patch.object(tp, "_save_cursor")
        fetch = AsyncMock()
        with patch.object(tp, "_poller_configured", return_value=True), \
             patch.object(tp, "_load_cursor", return_value=0), \
             patch.object(tp, "_seek_head", AsyncMock(return_value=316200)), \
             patch.object(tp, "_fetch_changes", fetch), \
             save as save_mock:
            _run(tp._poll_once())
        save_mock.assert_called_once_with(316200)
        fetch.assert_not_awaited()  # backlog never fetched

    def test_cold_start_head_unknown_falls_through_to_fetch(self):
        # DB unreachable → head None → old cap-guard path (still freshness-gated).
        fetch = AsyncMock(return_value=None)
        with patch.object(tp, "_poller_configured", return_value=True), \
             patch.object(tp, "_load_cursor", return_value=0), \
             patch.object(tp, "_seek_head", AsyncMock(return_value=None)), \
             patch.object(tp, "_fetch_changes", fetch):
            _run(tp._poll_once())
        fetch.assert_awaited_once()

    def test_warm_cursor_does_not_seek_head(self):
        seek = AsyncMock()
        with patch.object(tp, "_poller_configured", return_value=True), \
             patch.object(tp, "_load_cursor", return_value=316000), \
             patch.object(tp, "_seek_head", seek), \
             patch.object(tp, "_fetch_changes", AsyncMock(return_value=None)):
            _run(tp._poll_once())
        seek.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
