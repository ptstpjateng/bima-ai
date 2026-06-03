"""
Characterization tests for the INBOUND WhatsApp media HANDLER — the
`_process_inbound_media` background task in `routers/aptana.py` — plus two
cross-cutting behaviours the sibling files only test in pieces:

  (1) session_store round-trip through the PUBLIC async API for BOTH session
      types, and the in-memory fallback when Redis is unavailable (mocked).
  (3) the score reminder appears on a follow-up REVIEW turn after a score
      exists (driven through guided_submission.maybe_handle, not the helper).

The existing `test_whatsapp_media.py` exercises the media PARSER and the
download GUARDS in isolation, and `test_guided_submission_scoring.py` covers
`handle_inbound_documents`. NOTHING exercised the actual aptana handler that
glues download -> attach -> score -> reply together. This file does, because
that handler is where the wiring (and its bugs) live.

These are CHARACTERIZATION tests: every assertion captures what the code does
TODAY, against literal inputs and literal expected outputs. Where the current
behaviour looks wrong, the test still asserts the real behaviour and the
discrepancy is reported separately (see the module-level NOTE on the
`_SCOREABLE_MEDIA_TYPES` import).

Run:
    pytest tests/test_aptana_media_handler.py            # preferred
    python tests/test_aptana_media_handler.py            # also works (unittest)

No real network, no real Redis, no real Gemini. httpx + fastapi + the heavy
service deps are stubbed BEFORE the targets import, mirroring the sibling
session tests.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# Stub the heavy / uninstalled deps BEFORE importing the targets.
#
# Installed in the test env: httpx (real). NOT installed: redis, fastapi,
# asyncpg, dotenv. The sibling tests stub the same surface; we keep a single
# coherent stub set here so this file is self-contained and order-independent.
# ===========================================================================

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


# --- httpx: a controllable async stream/get fake (same design as the sibling
#     media test) so download_media() can be driven without a network. -------

class _FakeStreamResponse:
    def __init__(self, *, status_code=200, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _FakeAsyncClient:
    stream_response = None
    get_response = None
    last_stream_url = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, headers=None):
        type(self).last_stream_url = url
        return type(self).stream_response

    async def get(self, url, headers=None):
        return type(self).get_response


# `whatsapp_media` does `import httpx` and references httpx.RequestError /
# HTTPError. Prefer the REAL httpx if it's installed (so we never clobber the
# real module's other attributes — e.g. httpx.TimeoutException, which
# test_guided_submission.py relies on; an earlier version of this guard used
# `"httpx" not in sys.modules`, which is true at first import even when httpx
# IS installed, and so wrongly installed a stub that leaked to sibling files).
# Only fall back to a stub when httpx is genuinely absent. Either way we do NOT
# globally swap AsyncClient here — each test that drives download_media patches
# `whatsapp_media.httpx.AsyncClient` in a SCOPED, auto-restored patch (see
# _capture_sends), so this file leaves no global httpx side effect behind.
try:
    import httpx as _httpx  # noqa: F401  (real, if installed)
except ImportError:
    _httpx = types.ModuleType("httpx")
    _httpx.AsyncClient = _FakeAsyncClient

    class _RequestError(Exception):
        pass

    class _HTTPError(Exception):
        pass

    _httpx.RequestError = _RequestError
    _httpx.HTTPError = _HTTPError
    sys.modules["httpx"] = _httpx


# --- Capture-friendly stubs for the router's module-top service imports. ----
# whatsapp_sender.send_text is the citizen-facing reply channel; we record
# every call so we can assert what the citizen actually receives.
#
# NOTE: aptana.py binds `send_text` / `acknowledge_received` at MODULE TOP
# (`from services.whatsapp_sender import acknowledge_received, send_text`). The
# real whatsapp_sender imports fine (no missing deps), so stubbing it in
# sys.modules is both unnecessary and ineffective — aptana already holds a
# reference to the real functions. We therefore patch the names ON THE APTANA
# MODULE in each test (see _capture_sends). The ai_handler / input_sanitizer /
# notifications / prompt_injection stubs below ARE needed: those modules pull
# chromadb / heavy deps that aren't installed in the test env.

_SENT: list[dict] = []


async def _fake_send_text(recipient_phone=None, body=None, **kwargs):
    _SENT.append({"to": recipient_phone, "body": body})
    return True


async def _fake_acknowledge_received(message_id, recipient_phone):
    return None


_ensure_stub("services.ai_handler", {"generate_ai_response": None, "log_to_backend": None})
_ensure_stub("services.input_sanitizer", {"sanitize_user_input": lambda s: s})
_ensure_stub(
    "services.notifications",
    {"record_engagement": lambda *a: None, "record_opt_out": lambda *a: None},
)
_ensure_stub("services.prompt_injection_detector", {"audit_user_input": lambda *a: None})

# Minimal fastapi stub (APIRouter + the symbols imported at module top).
if "fastapi" not in sys.modules:
    _fastapi = types.ModuleType("fastapi")

    class _APIRouter:
        def post(self, *a, **k):
            def _decorator(fn):
                return fn
            return _decorator

    class _HTTPException(Exception):
        def __init__(self, status_code=400, detail=""):
            self.status_code = status_code
            self.detail = detail

    _fastapi.APIRouter = _APIRouter
    _fastapi.BackgroundTasks = object
    _fastapi.HTTPException = _HTTPException
    _fastapi.Request = object
    sys.modules["fastapi"] = _fastapi

from routers import aptana  # noqa: E402
from services import guided_submission as gs  # noqa: E402
from services import session_store as ss  # noqa: E402
from services.agents.suitability_judge import (  # noqa: E402
    CompletenessSection,
    Issue,
    SuitabilityResult,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _suitability_result(percent=90, critical=False):
    issues = []
    if critical:
        issues = [Issue(id="c", severity="critical", title="Dokumen wajib hilang", detail="d")]
    return SuitabilityResult(
        completeness=CompletenessSection(score=1.0, missing=[], required=["KTP"]),
        type_correctness=[],
        suitability=[],
        compatibility_findings=[],
        overall_suitability_score=percent / 100.0,
        issues=issues,
    )


def _make_review_session(user_id, *, last_score=None):
    sess = gs.SubmissionSession(user_id=user_id)
    sess.license_id = 358
    sess.license_name = "Izin Penelitian"
    sess.stage = gs.Stage.REVIEW
    sess.fields = {
        "applicant_name": "Budi",
        "nik": "3374012345678901",
        "business_name": "CV Riset",
        "phone": "628123",
    }
    sess.last_score = last_score
    return sess


def _reset_all():
    _SENT.clear()
    gs._sessions.clear()
    ss._client = None
    ss._client_init_failed = False
    ss._warned_unavailable = False
    _FakeAsyncClient.stream_response = None
    _FakeAsyncClient.get_response = None
    _FakeAsyncClient.last_stream_url = None


def _capture_sends():
    """Scope-patch, for the duration of one test:
      * aptana.send_text / aptana.acknowledge_received → record into _SENT
        (aptana binds these at module top, so we patch the names ON aptana).
      * whatsapp_media.httpx.AsyncClient → the network-free _FakeAsyncClient,
        so download_media never touches the wire. Patched on the httpx object
        whatsapp_media holds, and AUTO-RESTORED on exit, so this file leaves no
        global httpx mutation that could poison sibling test files.
    Returns a context manager."""
    from services import whatsapp_media as wm

    class _Patches:
        def __enter__(_s):
            _s._a = patch.object(aptana, "send_text", _fake_send_text)
            _s._b = patch.object(aptana, "acknowledge_received", _fake_acknowledge_received)
            _s._c = patch.object(wm.httpx, "AsyncClient", _FakeAsyncClient)
            _s._a.__enter__()
            _s._b.__enter__()
            _s._c.__enter__()
            return _s

        def __exit__(_s, *exc):
            _s._c.__exit__(*exc)
            _s._b.__exit__(*exc)
            _s._a.__exit__(*exc)
            return False

    return _Patches()


# ===========================================================================
# (2) APTANA media HANDLER — _process_inbound_media end-to-end.
#
# This is the central ask: a representative inbound media payload must travel
# download -> attach_documents -> score -> reply.
#
# FIXED (was a blocker): the handler previously did
#   from services.whatsapp_media import _SCOREABLE_MEDIA_TYPES, download_media
# but _SCOREABLE_MEDIA_TYPES is defined in routers/aptana.py, NOT in
# services/whatsapp_media.py — so every media message hit an ImportError. The
# handler now imports only `download_media` from whatsapp_media and references
# the in-scope module-level _SCOREABLE_MEDIA_TYPES on the aptana module. The
# tests below run the real download->attach->score path with NO symbol
# injection, proving the fixed wiring works.
# ===========================================================================

# _SCOREABLE_MEDIA_TYPES lives on the aptana router module.
_INTENDED_SCOREABLE = ("image", "document")


class TestScoreableSymbolLocation(unittest.TestCase):
    """The import blocker is fixed. `_SCOREABLE_MEDIA_TYPES` is a module-level
    tuple on routers/aptana.py and is NOT (and need not be) exported by
    services/whatsapp_media.py; the handler references the aptana global rather
    than importing it from whatsapp_media."""

    def test_scoreable_lives_on_aptana_not_whatsapp_media(self):
        from services import whatsapp_media as wm
        self.assertEqual(aptana._SCOREABLE_MEDIA_TYPES, _INTENDED_SCOREABLE)
        # whatsapp_media must NOT define it — the handler no longer imports it
        # from there (importing it would re-introduce the ImportError blocker).
        self.assertFalse(hasattr(wm, "_SCOREABLE_MEDIA_TYPES"))

    def test_handler_does_not_raise_importerror_for_image(self):
        # Active session, flag ON, a clean image — the handler runs the real
        # download->attach->score path with NO symbol injection (the fix means
        # _SCOREABLE_MEDIA_TYPES resolves as an aptana module global).
        sess = _make_review_session("wa-628111222")
        media = aptana._extract_media_from_payload(
            {"messages": [{"type": "image",
                           "image": {"id": "IMG1", "link": "https://lookaside.fbsbx.com/IMG1",
                                     "mime_type": "image/jpeg"}}]}
        )
        _FakeAsyncClient.stream_response = _FakeStreamResponse(
            headers={"content-type": "image/jpeg"}, chunks=[b"\xff\xd8\xff", b"jpegbody"],
        )
        fake_score = _suitability_result(percent=77)
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true",
                                     "GUIDED_SUBMISSION_DEMO_PACKET": ""}, clear=False):
            _run(gs._put_session(sess))
            with _capture_sends(), patch(
                "services.citizen_scorer.score_session_documents",
                new=AsyncMock(return_value=fake_score),
            ):
                # Must NOT raise ImportError — the blocker is fixed.
                _run(aptana._process_inbound_media(
                    msisdn="628111222", media=media, message_id="wamid.X"
                ))
        # The citizen got a reply (the upload is no longer silently lost).
        self.assertEqual(len(_SENT), 1)
        self.assertIn("77%", _SENT[0]["body"])


# These document the contract the handler satisfies, running the real
# download->attach->score path with NO symbol injection (the import blocker is
# fixed: the handler references the aptana module-level _SCOREABLE_MEDIA_TYPES).

class TestMediaHandlerIntendedBehaviour(unittest.TestCase):
    def setUp(self):
        _reset_all()

    def test_image_download_attach_score_reply(self):
        sess = _make_review_session("wa-628111222")
        media = aptana._extract_media_from_payload(
            {"messages": [{"type": "image",
                           "image": {"id": "IMG1",
                                     "link": "https://lookaside.fbsbx.com/IMG1",
                                     "mime_type": "image/jpeg"}}]}
        )
        self.assertIsNotNone(media)
        self.assertEqual(media.kind, "image")

        # download_media will stream this clean JPEG.
        _FakeAsyncClient.stream_response = _FakeStreamResponse(
            headers={"content-type": "image/jpeg"}, chunks=[b"\xff\xd8\xff", b"jpegbody"],
        )
        fake_score = _suitability_result(percent=88)
        env = {"BIMA_GUIDED_SUBMISSION_ENABLED": "true",
               "GUIDED_SUBMISSION_DEMO_PACKET": ""}

        with patch.dict(os.environ, env, clear=False):
            _run(gs._put_session(sess))
            with _capture_sends(), patch(
                "services.citizen_scorer.score_session_documents",
                new=AsyncMock(return_value=fake_score),
            ):
                _run(aptana._process_inbound_media(
                    msisdn="628111222", media=media, message_id="wamid.ABC"
                ))

        # The document was attached to the citizen's session...
        attached = gs._sessions["wa-628111222"].documents
        self.assertEqual(len(attached), 1)
        self.assertEqual(attached[0].content, b"\xff\xd8\xffjpegbody")
        self.assertEqual(attached[0].mime_type, "image/jpeg")
        # ...the file_id is the Meta message id (handler's choice)...
        self.assertEqual(attached[0].file_id, "wamid.ABC")
        # ...and the citizen got exactly one reply carrying the 88% score.
        self.assertEqual(len(_SENT), 1)
        self.assertEqual(_SENT[0]["to"], "628111222")
        self.assertIn("88%", _SENT[0]["body"])

    def test_disallowed_media_type_declines(self):
        # An audio message is parseable but not scoreable → polite decline,
        # no download attempted.
        from services.whatsapp_media import InboundMedia
        audio = InboundMedia(kind="audio", media_id="AUD1")
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            with _capture_sends():
                _run(aptana._process_inbound_media(
                    msisdn="628000111", media=audio, message_id="wamid.AUD"
                ))
        self.assertEqual(len(_SENT), 1)
        body = _SENT[0]["body"].lower()
        self.assertIn("foto", body)   # asks for JPG/PNG...
        self.assertIn("pdf", body)    # ...or PDF

    def test_no_active_session_nudges_to_start_flow(self):
        from services.whatsapp_media import InboundMedia
        img = InboundMedia(kind="image", media_id="IMG1",
                           link="https://graph.facebook.com/IMG1", mime_type="image/jpeg")
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            with _capture_sends():
                # No session put → has_active_session is False.
                _run(aptana._process_inbound_media(
                    msisdn="628999888", media=img, message_id="wamid.X"
                ))
        self.assertEqual(len(_SENT), 1)
        self.assertIn("belum ada pengajuan", _SENT[0]["body"].lower())

    def test_oversize_download_returns_unsupported_format_reply(self):
        # Active session, but the streamed file blows the 10 MB cap →
        # download_media returns None → citizen gets the "unsupported/too big"
        # decline. The size-guard path the ask calls out, end-to-end.
        sess = _make_review_session("wa-628111222")
        from services.whatsapp_media import InboundMedia, _MAX_MEDIA_BYTES
        big = b"x" * (_MAX_MEDIA_BYTES + 1)
        _FakeAsyncClient.stream_response = _FakeStreamResponse(
            headers={"content-type": "application/pdf"}, chunks=[big],
        )
        media = InboundMedia(kind="document",
                             link="https://graph.facebook.com/big.pdf",
                             mime_type="application/pdf")
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            _run(gs._put_session(sess))
            with _capture_sends():
                _run(aptana._process_inbound_media(
                    msisdn="628111222", media=media, message_id="wamid.BIG"
                ))
        # No document attached (download refused)...
        self.assertEqual(len(gs._sessions["wa-628111222"].documents), 0)
        # ...and the citizen is told the format/size is unsupported.
        self.assertEqual(len(_SENT), 1)
        body = _SENT[0]["body"].lower()
        self.assertTrue("10 mb" in body or "tidak dapat" in body)

    def test_disallowed_content_type_download_returns_decline(self):
        # Active session; server responds text/html → download_media rejects →
        # decline reply. The content-type guard path, end-to-end.
        sess = _make_review_session("wa-628111222")
        from services.whatsapp_media import InboundMedia
        _FakeAsyncClient.stream_response = _FakeStreamResponse(
            headers={"content-type": "text/html"}, chunks=[b"<html>"],
        )
        media = InboundMedia(kind="document",
                             link="https://graph.facebook.com/x",
                             mime_type="application/pdf")
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            _run(gs._put_session(sess))
            with _capture_sends():
                _run(aptana._process_inbound_media(
                    msisdn="628111222", media=media, message_id="wamid.HTML"
                ))
        self.assertEqual(len(gs._sessions["wa-628111222"].documents), 0)
        self.assertEqual(len(_SENT), 1)

    def test_guided_flag_off_drops_media_silently(self):
        # With the symbol injected, the is_enabled()==False path now runs: the
        # handler returns before any send. (Documents the intended dark-mode.)
        from services.whatsapp_media import InboundMedia
        img = InboundMedia(kind="image", media_id="IMG1",
                           link="https://graph.facebook.com/IMG1", mime_type="image/jpeg")
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "false"}, clear=False):
            with _capture_sends():
                _run(aptana._process_inbound_media(
                    msisdn="628111222", media=img, message_id="wamid.X"
                ))
        self.assertEqual(_SENT, [])


class TestInboundRouteSelectsMedia(unittest.TestCase):
    """The synchronous inbound endpoint decision: a media-only payload (no
    text) must be routed to the media background task, not the text path.
    We assert the routing decision via _extract_media_from_payload + the
    'no text' condition the endpoint uses, without standing up FastAPI."""

    def setUp(self):
        _reset_all()

    def test_media_only_payload_has_no_text_but_yields_media(self):
        payload = {"messages": [{"type": "document",
                                 "document": {"id": "D1", "filename": "ktp.pdf",
                                              "mime_type": "application/pdf"}}]}
        # The endpoint computes: text = _extract_text_from_payload(...)
        self.assertIsNone(aptana._extract_text_from_payload(payload))
        # ...and media = _extract_media_from_payload(...) → routes to media.
        media = aptana._extract_media_from_payload(payload)
        self.assertIsNotNone(media)
        self.assertEqual(media.kind, "document")
        self.assertEqual(media.media_id, "D1")

    def test_message_id_extracted_for_ack(self):
        payload = {"messages": [{"id": "wamid.MID1", "type": "image",
                                 "image": {"id": "IMG1"}}]}
        self.assertEqual(aptana._extract_message_id_from_payload(payload), "wamid.MID1")


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _FakeBackground:
    """Records scheduled background tasks instead of running them."""

    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *a, **k):
        self.tasks.append((fn, a, k))


class TestOfficerMediaShortCircuit(unittest.TestCase):
    """Fix 4: a media-only message from the configured officer must NOT enter
    the citizen media flow (it would get the 'no active submission' nudge).
    The endpoint short-circuits with officer_media_ignored before scheduling
    _process_inbound_media."""

    def setUp(self):
        _reset_all()

    def _media_payload(self):
        return {"phoneNumber": "628999000111",
                "payload": {"messages": [{"type": "document", "id": "wamid.D",
                                          "document": {"id": "D1",
                                                       "mime_type": "application/pdf"}}]}}

    def test_officer_media_only_is_ignored(self):
        env = {"BIMA_OFFICER_NOTIFY_ENABLED": "true",
               "BIMA_OFFICER_WA_PHONE": "628999000111",
               "BIMA_OFFICER_TG_CHAT": ""}
        bg = _FakeBackground()
        with patch.dict(os.environ, env, clear=False):
            with patch.object(aptana, "_PATH_SECRET", "s3cret"):
                res = _run(aptana.aptana_inbound(
                    "s3cret", _FakeRequest(self._media_payload()), bg))
        self.assertEqual(res.get("skipped"), "officer_media_ignored")
        # No media background task scheduled for the officer.
        self.assertEqual(bg.tasks, [])

    def test_citizen_media_only_still_routed(self):
        # Same payload but the sender is NOT the officer → media task scheduled.
        env = {"BIMA_OFFICER_NOTIFY_ENABLED": "true",
               "BIMA_OFFICER_WA_PHONE": "628000000000",   # a different number
               "BIMA_OFFICER_TG_CHAT": ""}
        bg = _FakeBackground()
        with patch.dict(os.environ, env, clear=False):
            with patch.object(aptana, "_PATH_SECRET", "s3cret"):
                res = _run(aptana.aptana_inbound(
                    "s3cret", _FakeRequest(self._media_payload()), bg))
        self.assertEqual(res.get("media"), "document")
        self.assertEqual(len(bg.tasks), 1)
        self.assertIs(bg.tasks[0][0], aptana._process_inbound_media)


# ===========================================================================
# (1) session_store round-trip through the PUBLIC async API + in-memory
#     fallback when Redis is unavailable.
#
# The sibling test_session_store.py round-trips the encode/decode helpers
# directly. Here we drive the REAL public path the production code uses:
#   guided_submission._put_session / _get_session
#   officer_bridge._put_session    / _get_session
# with a fake Redis injected — proving a SubmissionSession and an
# OfficerCaseSession survive save->Redis->load incl. document bytes + score,
# and that an in-memory copy is kept (write-through), and that on a Redis-miss
# (cold process) the durable copy rehydrates.
# ===========================================================================

class _FakeRedis:
    """dict-backed async stand-in for redis.asyncio.Redis."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.last_ttl = None

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


class TestSubmissionSessionDurableRoundTrip(unittest.TestCase):
    def setUp(self):
        _reset_all()

    def test_survives_redis_round_trip_incl_bytes_and_score(self):
        fake = _FakeRedis()
        ss._client = fake
        sess = _make_review_session(
            "wa-628777",
            last_score={"ok": False, "status": "needs_fix", "score_percent": 73,
                        "summary": "s", "message": "m", "issues": [],
                        "result": object()},  # rich result must be dropped
        )
        sess.documents = [
            gs.SessionDocument("doc-1", "ktp", "ktp.jpg", "image/jpeg", b"\xff\xd8\xff KTP"),
        ]
        with patch.dict(os.environ, {"BIMA_REDIS_SESSIONS_ENABLED": "true"}, clear=False):
            _run(gs._put_session(sess))
            # Persisted with the session TTL.
            self.assertEqual(fake.last_ttl, gs._SESSION_TTL_SECONDS)
            # Now evict the in-memory copy → forces a durable (Redis) read.
            gs._sessions.clear()
            restored = _run(gs._get_session("wa-628777"))

        self.assertIsNotNone(restored)
        self.assertEqual(restored.stage, gs.Stage.REVIEW)
        self.assertEqual(restored.license_id, 358)
        self.assertEqual(restored.fields["nik"], "3374012345678901")
        # Document bytes survived base64.
        self.assertEqual(restored.documents[0].content, b"\xff\xd8\xff KTP")
        # JSON-safe score fields survived; rich `result` dropped.
        self.assertEqual(restored.last_score["score_percent"], 73)
        self.assertNotIn("result", restored.last_score)
        # A durable hit re-warms the in-memory LRU.
        self.assertIn("wa-628777", gs._sessions)

    def test_in_memory_fallback_when_redis_unavailable(self):
        # Redis raises on every op → save/load must NOT lose the session; the
        # in-memory LRU is the always-on fallback.
        ss._client = _RaisingRedis()
        sess = _make_review_session("wa-628888")
        with patch.dict(os.environ, {"BIMA_REDIS_SESSIONS_ENABLED": "true"}, clear=False):
            _run(gs._put_session(sess))           # Redis SET raises, swallowed
            got = _run(gs._get_session("wa-628888"))  # served from memory
        self.assertIsNotNone(got)
        self.assertEqual(got.user_id, "wa-628888")
        self.assertEqual(got.stage, gs.Stage.REVIEW)

    def test_flag_off_keeps_session_in_memory_only(self):
        # Flag OFF: session_store is a no-op, but the flow still works entirely
        # in memory (the pre-Redis behaviour). Nothing is written to Redis.
        fake = _FakeRedis()
        ss._client = fake
        sess = _make_review_session("wa-628999")
        with patch.dict(os.environ, {"BIMA_REDIS_SESSIONS_ENABLED": "false"}, clear=False):
            _run(gs._put_session(sess))
            self.assertEqual(fake.store, {})       # nothing persisted
            got = _run(gs._get_session("wa-628999"))
        self.assertIsNotNone(got)
        self.assertEqual(got.user_id, "wa-628999")


class TestOfficerSessionDurableRoundTrip(unittest.TestCase):
    def setUp(self):
        _reset_all()
        from services import officer_bridge as ob
        ob._sessions.clear()

    def test_survives_redis_round_trip_incl_doc_bytes(self):
        from services import officer_bridge as ob
        fake = _FakeRedis()
        ss._client = fake
        sess = ob.OfficerCaseSession(
            channel_id="628999000111",
            channel=ob.CHANNEL_WHATSAPP,
            ticket="000088002",
            request_id=42,
            license_name="Izin Penelitian",
            validation={"score_percent": 90, "status": "ready", "summary": "", "issues": []},
            documents={"doc-1": {"filename": "proposal.pdf", "mime_type": "application/pdf",
                                 "claimed_type": "proposal", "content": b"%PDF body"}},
            history=[{"role": "user", "text": "halo"}],
        )
        with patch.dict(os.environ, {"BIMA_REDIS_SESSIONS_ENABLED": "true"}, clear=False):
            _run(ob._put_session(sess))
            self.assertEqual(fake.last_ttl, ob._OFFICER_SESSION_TTL_SECONDS)
            ob._sessions.clear()                       # force durable read
            restored = _run(ob._get_session("628999000111"))

        self.assertIsNotNone(restored)
        self.assertEqual(restored.ticket, "000088002")
        self.assertEqual(restored.request_id, 42)
        self.assertEqual(restored.documents["doc-1"]["content"], b"%PDF body")
        self.assertEqual(restored.validation["score_percent"], 90)
        self.assertEqual(restored.history[0]["text"], "halo")

    def test_in_memory_fallback_when_redis_unavailable(self):
        from services import officer_bridge as ob
        ss._client = _RaisingRedis()
        sess = ob.OfficerCaseSession(
            channel_id="628999000111", channel=ob.CHANNEL_WHATSAPP, ticket="T1",
        )
        with patch.dict(os.environ, {"BIMA_REDIS_SESSIONS_ENABLED": "true"}, clear=False):
            _run(ob._put_session(sess))
            got = _run(ob._get_session("628999000111"))
        self.assertIsNotNone(got)
        self.assertEqual(got.ticket, "T1")


# ===========================================================================
# (3) Score reminder on a FOLLOW-UP turn after a score exists.
#
# Driven through the public maybe_handle entry point (the path ai_handler
# actually calls), not just the _score_reminder_prefix helper. A REVIEW-stage
# session with a persisted last_score, given a non-YA / non-KIRIM message,
# must prepend the compact "Skor terkini: X% (status)" line.
# ===========================================================================

class TestScoreReminderOnFollowUp(unittest.TestCase):
    def setUp(self):
        _reset_all()

    def test_reminder_prepended_on_clarifying_turn(self):
        sess = _make_review_session(
            "wa-628321",
            last_score={"ok": False, "status": "needs_fix", "score_percent": 73,
                        "summary": "s", "message": "m", "issues": []},
        )
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            _run(gs._put_session(sess))
            reply = _run(gs.maybe_handle("wa-628321", "apakah ini sudah benar?"))
        self.assertIsNotNone(reply)
        # Literal expected reminder line.
        self.assertTrue(reply.startswith("📊 Skor terkini: 73% (perlu diperbaiki)"))
        # The standard confirm instruction still follows the reminder.
        self.assertIn("YA", reply.upper())

    def test_ready_status_label(self):
        sess = _make_review_session(
            "wa-628322",
            last_score={"ok": True, "status": "ready", "score_percent": 92,
                        "summary": "s", "message": "m", "issues": []},
        )
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            _run(gs._put_session(sess))
            reply = _run(gs.maybe_handle("wa-628322", "boleh tanya dulu?"))
        self.assertTrue(reply.startswith("📊 Skor terkini: 92% (siap dikirim)"))

    def test_no_reminder_before_any_score(self):
        sess = _make_review_session("wa-628323", last_score=None)
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            _run(gs._put_session(sess))
            reply = _run(gs.maybe_handle("wa-628323", "halo?"))
        self.assertIsNotNone(reply)
        self.assertNotIn("Skor terkini", reply)

    def test_reminder_not_shown_on_YA_confirm(self):
        # On YA the flow submits (not a clarifying turn) → no reminder prefix.
        # SIAP client is unconfigured here, so _submit returns the "channel not
        # active" message — but crucially WITHOUT the score-reminder prefix.
        sess = _make_review_session(
            "wa-628324",
            last_score={"ok": True, "status": "ready", "score_percent": 92,
                        "summary": "s", "message": "m", "issues": []},
        )
        unconfigured = types.SimpleNamespace(is_configured=lambda: False)
        with patch.dict(os.environ, {"BIMA_GUIDED_SUBMISSION_ENABLED": "true"}, clear=False):
            _run(gs._put_session(sess))
            with patch("services.siap_submission_client.get_siap_submission_client",
                       return_value=unconfigured):
                reply = _run(gs.maybe_handle("wa-628324", "YA"))
        self.assertIsNotNone(reply)
        self.assertNotIn("Skor terkini", reply)


if __name__ == "__main__":
    unittest.main(verbosity=2)
