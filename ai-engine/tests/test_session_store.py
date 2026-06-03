"""
Tests for the durable session store — `services/session_store.py` — and the
serialization round-trips in guided_submission + officer_bridge.

Run standalone (no pytest — matches the other ai-engine session tests):

    python -m tests.test_session_store     # from ai-engine/
    python tests/test_session_store.py     # also works

Covers:
  * is_enabled() honours the env flag.
  * save/load/delete are no-ops (return False/None) when the flag is OFF.
  * With the flag ON and a FAKE in-memory async Redis injected:
      - save → load round-trips a JSON blob, honouring the TTL arg.
      - a decode error drops the key and returns None (cache-miss fallback).
      - a raising client → save/load degrade to False/None (in-memory fallback).
  * guided_submission._encode_session/_decode_session round-trip a
    SubmissionSession: document bytes survive (base64), and the non-JSON
    last_score["result"] dataclass is dropped while score_percent/status stay.
  * officer_bridge._encode_officer_session/_decode_officer_session round-trip
    an OfficerCaseSession: document content bytes survive.

No real Redis, no real network. The Redis client is a hand-rolled fake.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

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


# Same stubs the sibling session tests use so the heavy deps aren't required.
_ensure_stub("asyncpg")
_ensure_stub("dotenv", {"load_dotenv": lambda *a, **k: None})
_ensure_stub("httpx")
_ensure_stub("services.agents.officer_copilot", {"get_copilot": lambda: None})
# guided_submission pulls citizen_scorer lazily inside a function; stub the
# import surface so module import is light. The scorer isn't called here.
_ensure_stub("services.citizen_scorer")
_ensure_stub(
    "services.agents.suitability_judge",
    {"UploadedDoc": object, "Issue": object, "SuitabilityResult": object,
     "judge_submission": None},
)

from services import session_store as ss  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _FakeRedis:
    """A minimal async stand-in for redis.asyncio.Redis: dict-backed get/set/
    delete/ping. Records the last ttl passed to set."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.last_ttl: int | None = None

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.last_ttl = ex

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)

    async def ping(self):
        return True


class _RaisingRedis:
    async def set(self, *a, **k):
        raise RuntimeError("redis down")

    async def get(self, *a, **k):
        raise RuntimeError("redis down")

    async def delete(self, *a, **k):
        raise RuntimeError("redis down")

    async def ping(self):
        raise RuntimeError("redis down")


def _reset_store():
    ss._client = None
    ss._client_init_failed = False
    ss._warned_unavailable = False


class TestRedisParams(unittest.TestCase):
    """Fix 5: the password is passed as a DISCRETE kwarg (not interpolated into
    a redis:// URL), so a requirepass value with URL-significant characters
    (@ : # / and whitespace) can't corrupt the connection and silently break
    auth."""

    def test_password_with_special_chars_passed_verbatim(self):
        env = {
            "REDIS_HOST": "redis", "REDIS_PORT": "6380", "REDIS_DB": "2",
            "REDIS_PASSWORD": "p@ss:w/ord#1 with space",
        }
        with patch.dict(os.environ, env, clear=False):
            params = ss._redis_params()
        self.assertEqual(params["host"], "redis")
        self.assertEqual(params["port"], 6380)
        self.assertEqual(params["db"], 2)
        # Password preserved exactly — not URL-escaped, not truncated at '@'/':'
        self.assertEqual(params["password"], "p@ss:w/ord#1 with space")

    def test_no_password_key_when_unset(self):
        env = {"REDIS_HOST": "redis", "REDIS_PORT": "6379", "REDIS_DB": "0"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("REDIS_PASSWORD", None)
            params = ss._redis_params()
        self.assertNotIn("password", params)

    def test_client_constructed_with_discrete_password(self):
        _reset_store()
        captured = {}

        class _FakeRedisCls:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        fake_aioredis = types.ModuleType("redis.asyncio")
        fake_aioredis.Redis = _FakeRedisCls
        env = {"BIMA_REDIS_SESSIONS_ENABLED": "true",
               "REDIS_PASSWORD": "a@b:c", "REDIS_HOST": "redis"}
        # Inject a fake `redis` package whose `.asyncio` has our Redis class.
        fake_redis_pkg = types.ModuleType("redis")
        fake_redis_pkg.asyncio = fake_aioredis
        with patch.dict(sys.modules, {"redis": fake_redis_pkg,
                                      "redis.asyncio": fake_aioredis}):
            with patch.dict(os.environ, env, clear=False):
                client = _run(ss._get_client())
        self.assertIsNotNone(client)
        self.assertEqual(captured.get("password"), "a@b:c")
        self.assertEqual(captured.get("host"), "redis")
        self.assertTrue(captured.get("decode_responses"))


class TestFlagGating(unittest.TestCase):
    def setUp(self):
        _reset_store()

    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BIMA_REDIS_SESSIONS_ENABLED", None)
            self.assertFalse(ss.is_enabled())

    def test_noop_when_disabled(self):
        with patch.dict(os.environ, {"BIMA_REDIS_SESSIONS_ENABLED": "false"}, clear=False):
            self.assertFalse(_run(ss.save("k", {"a": 1}, encode=lambda o: "x", ttl_seconds=10)))
            self.assertIsNone(_run(ss.load("k", decode=lambda s: s)))
            self.assertFalse(_run(ss.delete("k")))


class TestSaveLoadRoundTrip(unittest.TestCase):
    def setUp(self):
        _reset_store()

    def test_round_trip_with_ttl(self):
        fake = _FakeRedis()
        ss._client = fake  # inject so _get_client returns it without importing redis
        with patch.dict(os.environ, {"BIMA_REDIS_SESSIONS_ENABLED": "true"}, clear=False):
            ok = _run(ss.save("bima:k1", {"n": 5}, encode=lambda o: __import__("json").dumps(o), ttl_seconds=42))
            self.assertTrue(ok)
            self.assertEqual(fake.last_ttl, 42)
            out = _run(ss.load("bima:k1", decode=lambda s: __import__("json").loads(s)))
            self.assertEqual(out, {"n": 5})
            self.assertTrue(_run(ss.delete("bima:k1")))
            self.assertIsNone(_run(ss.load("bima:k1", decode=lambda s: s)))

    def test_decode_error_drops_key(self):
        fake = _FakeRedis()
        fake.store["bima:bad"] = "not-json{"
        ss._client = fake
        with patch.dict(os.environ, {"BIMA_REDIS_SESSIONS_ENABLED": "true"}, clear=False):
            def _boom(_s):
                raise ValueError("bad blob")
            out = _run(ss.load("bima:bad", decode=_boom))
            self.assertIsNone(out)
            # The malformed key is deleted so it can't strand the caller.
            self.assertNotIn("bima:bad", fake.store)

    def test_raising_client_degrades(self):
        ss._client = _RaisingRedis()
        with patch.dict(os.environ, {"BIMA_REDIS_SESSIONS_ENABLED": "true"}, clear=False):
            self.assertFalse(_run(ss.save("k", {"a": 1}, encode=lambda o: "x", ttl_seconds=10)))
            self.assertIsNone(_run(ss.load("k", decode=lambda s: s)))


class TestSubmissionSessionSerialization(unittest.TestCase):
    def test_round_trip_keeps_bytes_drops_result(self):
        from services import guided_submission as gs

        sess = gs.SubmissionSession(user_id="wa-628999")
        sess.stage = gs.Stage.REVIEW
        sess.license_id = 358
        sess.license_name = "Izin Penelitian"
        sess.fields = {"applicant_name": "Budi", "nik": "3374012345678901"}
        sess.documents = [
            gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"\xff\xd8\xff binary"),
        ]
        # last_score with a non-JSON `result` sentinel that must be stripped.
        sess.last_score = {
            "ok": False, "status": "needs_fix", "score_percent": 72,
            "summary": "s", "message": "m", "issues": [{"severity": "high", "message": "x"}],
            "result": object(),   # not JSON-safe → must be dropped on encode
        }

        blob = gs._encode_session(sess)
        restored = gs._decode_session(blob)

        self.assertEqual(restored.user_id, "wa-628999")
        self.assertEqual(restored.stage, gs.Stage.REVIEW)
        self.assertEqual(restored.license_id, 358)
        self.assertEqual(restored.fields["nik"], "3374012345678901")
        self.assertEqual(len(restored.documents), 1)
        # Document bytes survive the base64 round-trip.
        self.assertEqual(restored.documents[0].content, b"\xff\xd8\xff binary")
        # JSON-safe score fields survive; the rich `result` is gone.
        self.assertEqual(restored.last_score["score_percent"], 72)
        self.assertEqual(restored.last_score["status"], "needs_fix")
        self.assertNotIn("result", restored.last_score)

    def test_score_reminder_prefix_uses_persisted_fields(self):
        from services import guided_submission as gs

        sess = gs.SubmissionSession(user_id="wa-1")
        self.assertEqual(gs._score_reminder_prefix(sess), "")   # no score yet
        sess.last_score = {"score_percent": 88, "status": "ready"}
        prefix = gs._score_reminder_prefix(sess)
        self.assertIn("88%", prefix)
        self.assertIn("siap dikirim", prefix)


class TestOfficerSessionSerialization(unittest.TestCase):
    def test_round_trip_keeps_doc_bytes(self):
        from services import officer_bridge as ob

        sess = ob.OfficerCaseSession(
            channel_id="6285117557091",
            channel=ob.CHANNEL_WHATSAPP,
            ticket="000088002",
            request_id=42,
            license_name="Izin Penelitian",
            validation={"score_percent": 90, "status": "ready", "summary": "", "issues": []},
            documents={
                "doc-1": {
                    "filename": "ktp.jpg", "mime_type": "image/jpeg",
                    "claimed_type": "ktp", "content": b"\x89PNG binary",
                }
            },
            history=[{"role": "user", "text": "halo"}],
        )
        blob = ob._encode_officer_session(sess)
        restored = ob._decode_officer_session(blob)
        self.assertEqual(restored.ticket, "000088002")
        self.assertEqual(restored.request_id, 42)
        self.assertEqual(restored.documents["doc-1"]["content"], b"\x89PNG binary")
        self.assertEqual(restored.validation["score_percent"], 90)
        self.assertEqual(restored.history[0]["text"], "halo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
