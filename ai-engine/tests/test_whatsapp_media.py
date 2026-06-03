"""
Tests for inbound WhatsApp media intake — the security-critical download
guards in `services/whatsapp_media.py` and the best-effort APTANA media parser
in `routers/aptana.py`.

Run standalone (no pytest):

    python -m tests.test_whatsapp_media     # from ai-engine/
    python tests/test_whatsapp_media.py     # also works

Covers:
  * _host_allowed — SSRF guard: only https + allow-listed hosts pass; http,
    arbitrary hosts, and look-alike domains (evilfbcdn.net) are refused.
  * ALLOWED_CONTENT_TYPES — the allow-list is exactly the four scoreable types.
  * download_media — mocked httpx:
      - oversized stream is aborted (returns None).
      - disallowed content-type is rejected.
      - off-allow-list link is refused before any fetch.
      - a clean PDF link round-trips to DownloadedMedia with the right mime.
  * aptana._extract_media_from_payload — Meta image + document shapes (flat +
    nested envelope); returns None for text; captures both id and link.
  * aptana._payload_shape_keys — emits key names only, never values/bytes.

No real network. httpx is monkeypatched with an async stream/get fake.
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _run(coro):
    return asyncio.run(coro)


class _patch_client:
    """Scope-swap whatsapp_media.httpx.AsyncClient for the duration of a with
    block, restoring it on exit (so the redirect tests don't leak a client
    into the sibling cases that rely on the module-level _FakeAsyncClient)."""

    def __init__(self, client_cls):
        self._client_cls = client_cls

    def __enter__(self):
        import services.whatsapp_media as _wm
        self._orig = _wm.httpx.AsyncClient
        _wm.httpx.AsyncClient = self._client_cls
        return self

    def __exit__(self, *exc):
        import services.whatsapp_media as _wm
        _wm.httpx.AsyncClient = self._orig
        return False


# ---------------------------------------------------------------------------
# whatsapp_media imports `httpx` at module top. Provide a controllable fake so
# the module imports and the download tests can drive responses.
# ---------------------------------------------------------------------------

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


class _FakeGetResponse:
    def __init__(self, *, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Driven by class-level queues the tests set up before calling."""

    stream_response = None       # _FakeStreamResponse to return from .stream()
    get_response = None          # _FakeGetResponse to return from .get()

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
        type(self).last_get_url = url
        return type(self).get_response


# Make the target use _FakeAsyncClient WITHOUT clobbering a real httpx for any
# OTHER test file that runs after this one under pytest (an unconditional
# `sys.modules["httpx"] = fake` left a fake httpx behind that broke
# test_guided_submission.py's use of httpx.TimeoutException). So: only install
# a stub module when httpx is genuinely absent; when httpx is present, just
# point its AsyncClient at our fake (real httpx already has RequestError/
# HTTPError). Either way the target's `import httpx; httpx.AsyncClient(...)`
# resolves to the fake.
try:
    import httpx as _httpx  # real, if installed
    _httpx.AsyncClient = _FakeAsyncClient
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

from services import whatsapp_media as wm  # noqa: E402


class TestHostAllowlist(unittest.TestCase):
    def test_allows_meta_and_aptana(self):
        self.assertTrue(wm._host_allowed("https://graph.facebook.com/v22.0/123"))
        self.assertTrue(wm._host_allowed("https://lookaside.fbsbx.com/whatsapp/x"))
        self.assertTrue(wm._host_allowed("https://scontent.xx.fbcdn.net/v/t/abc"))
        self.assertTrue(wm._host_allowed("https://api-multichannel.aptana.co.id/m/1"))

    def test_refuses_http_and_arbitrary_and_lookalike(self):
        self.assertFalse(wm._host_allowed("http://graph.facebook.com/x"))   # not https
        self.assertFalse(wm._host_allowed("https://evil.example.com/x"))
        self.assertFalse(wm._host_allowed("https://evilfbcdn.net/x"))       # suffix look-alike
        self.assertFalse(wm._host_allowed("https://graph.facebook.com.evil.com/x"))
        self.assertFalse(wm._host_allowed("not-a-url"))


class TestIpDestinationGuard(unittest.TestCase):
    """SSRF-to-internal-network guard (Fix 2): private/loopback/link-local
    addresses are rejected, whether the host is an IP literal or a hostname
    that resolves to such an address."""

    def test_ip_literal_loopback_and_private_and_linklocal_blocked(self):
        # IP-literal hosts are refused outright (not allow-listed AND non-routable).
        self.assertFalse(wm._host_allowed("https://127.0.0.1/x"))
        self.assertFalse(wm._host_allowed("https://10.0.0.5/x"))
        self.assertFalse(wm._host_allowed("https://192.168.1.1/x"))
        self.assertFalse(wm._host_allowed("https://169.254.169.254/latest/meta-data"))
        self.assertFalse(wm._host_allowed("https://[::1]/x"))

    def test_dest_addresses_ok_rejects_nonroutable_literals(self):
        self.assertFalse(wm._dest_addresses_ok("127.0.0.1"))
        self.assertFalse(wm._dest_addresses_ok("169.254.169.254"))
        self.assertFalse(wm._dest_addresses_ok("10.1.2.3"))
        # A normal public literal is fine at the IP layer.
        self.assertTrue(wm._dest_addresses_ok("8.8.8.8"))

    def test_dest_addresses_ok_when_host_resolves_to_loopback(self):
        # A hostname that resolves to a loopback address must be refused even
        # though it is not an IP literal (DNS-rebinding style).
        import socket as _socket
        orig = wm.socket.getaddrinfo
        wm.socket.getaddrinfo = lambda host, *a, **k: [
            (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))
        ]
        try:
            self.assertFalse(wm._dest_addresses_ok("sneaky.example.com"))
        finally:
            wm.socket.getaddrinfo = orig


class TestContentTypeAllowlist(unittest.TestCase):
    def test_exactly_four(self):
        self.assertEqual(
            wm.ALLOWED_CONTENT_TYPES,
            {"image/jpeg", "image/png", "image/webp", "application/pdf"},
        )


class TestDownloadMedia(unittest.TestCase):
    def setUp(self):
        _FakeAsyncClient.stream_response = None
        _FakeAsyncClient.get_response = None

    def test_off_allowlist_link_refused(self):
        media = wm.InboundMedia(kind="document", link="https://evil.example.com/x.pdf")
        # Even if a response were set, the host guard rejects before fetch.
        _FakeAsyncClient.stream_response = _FakeStreamResponse(
            headers={"content-type": "application/pdf"}, chunks=[b"%PDF"],
        )
        self.assertIsNone(_run(wm.download_media(media)))

    def test_oversize_aborted(self):
        # One chunk over the cap → aborted → None.
        big = b"x" * (wm._MAX_MEDIA_BYTES + 1)
        _FakeAsyncClient.stream_response = _FakeStreamResponse(
            headers={"content-type": "application/pdf"}, chunks=[big],
        )
        media = wm.InboundMedia(kind="document", link="https://graph.facebook.com/file.pdf")
        self.assertIsNone(_run(wm.download_media(media)))

    def test_disallowed_content_type_rejected(self):
        _FakeAsyncClient.stream_response = _FakeStreamResponse(
            headers={"content-type": "text/html"}, chunks=[b"<html>"],
        )
        media = wm.InboundMedia(kind="document", link="https://graph.facebook.com/x")
        self.assertIsNone(_run(wm.download_media(media)))

    def test_clean_pdf_link_round_trips(self):
        _FakeAsyncClient.stream_response = _FakeStreamResponse(
            headers={"content-type": "application/pdf"}, chunks=[b"%PDF-1.4 ", b"body"],
        )
        media = wm.InboundMedia(
            kind="document", link="https://lookaside.fbsbx.com/x", filename="surat.pdf",
        )
        out = _run(wm.download_media(media))
        self.assertIsNotNone(out)
        self.assertEqual(out.mime_type, "application/pdf")
        self.assertEqual(out.content, b"%PDF-1.4 body")
        self.assertEqual(out.filename, "surat.pdf")

    def test_opaque_id_resolved_via_graph(self):
        # First the Graph GET resolves to a signed url; then the stream fetches.
        _FakeAsyncClient.get_response = _FakeGetResponse(
            json_data={"url": "https://lookaside.fbsbx.com/signed/abc"},
        )
        _FakeAsyncClient.stream_response = _FakeStreamResponse(
            headers={"content-type": "image/jpeg"}, chunks=[b"\xff\xd8\xff"],
        )
        import os
        os.environ["META_WA_TOKEN"] = "test-token"
        try:
            media = wm.InboundMedia(kind="image", media_id="MEDIA123")
            out = _run(wm.download_media(media))
        finally:
            os.environ.pop("META_WA_TOKEN", None)
        self.assertIsNotNone(out)
        self.assertEqual(out.mime_type, "image/jpeg")
        self.assertEqual(out.content, b"\xff\xd8\xff")


class _SeqAsyncClient:
    """A fake client whose .stream() yields a QUEUE of responses in order, so a
    redirect chain can be driven hop by hop. Set _SeqAsyncClient.queue +
    _SeqAsyncClient.opened before use."""

    queue: list = []
    opened: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, headers=None):
        type(self).opened.append((url, dict(headers or {})))
        return type(self).queue.pop(0)


class TestRedirectAndSniff(unittest.TestCase):
    """Fix 2: redirects are followed manually with per-hop allow-list checks,
    the auth header is dropped on host change, and an absent content-type is
    resolved by magic-byte sniffing (never the payload hint alone)."""

    def setUp(self):
        _SeqAsyncClient.queue = []
        _SeqAsyncClient.opened = []
        # Keep the redirect/allow-list logic hermetic: don't do real DNS in
        # these tests (the IP guard itself is covered in TestIpDestinationGuard).
        self._dest = wm._dest_addresses_ok
        wm._dest_addresses_ok = lambda host: True

    def tearDown(self):
        wm._dest_addresses_ok = self._dest

    def test_redirect_to_offlist_host_is_refused(self):
        # Hop 1: allow-listed host returns 302 → an off-allow-list host. The
        # manual resolver must refuse the next hop and return None (never fetch
        # the off-list body).
        _SeqAsyncClient.queue = [
            _FakeStreamResponse(status_code=302,
                                headers={"location": "https://evil.example.com/x.pdf"}),
        ]
        media = wm.InboundMedia(kind="document", link="https://lookaside.fbsbx.com/x")
        with _patch_client(_SeqAsyncClient):
            self.assertIsNone(_run(wm.download_media(media)))
        # Only the first (allow-listed) hop was ever opened.
        self.assertEqual(len(_SeqAsyncClient.opened), 1)

    def test_redirect_to_internal_ip_is_refused(self):
        # A 302 to the cloud metadata IP must be refused at the next-hop check.
        wm._dest_addresses_ok = self._dest  # use the REAL IP guard for this one
        _SeqAsyncClient.queue = [
            _FakeStreamResponse(status_code=302,
                                headers={"location": "https://169.254.169.254/latest/"}),
        ]
        media = wm.InboundMedia(kind="document", link="https://lookaside.fbsbx.com/x")
        with _patch_client(_SeqAsyncClient):
            self.assertIsNone(_run(wm.download_media(media)))

    def test_redirect_across_allowed_hosts_drops_auth_then_succeeds(self):
        # Hop 1: graph.facebook.com (auth sent) → 302 to fbcdn.net.
        # Hop 2: fbcdn.net serves a clean PDF. Auth must be DROPPED on hop 2.
        _SeqAsyncClient.queue = [
            _FakeStreamResponse(status_code=302,
                                headers={"location": "https://scontent.xx.fbcdn.net/file.pdf"}),
            _FakeStreamResponse(headers={"content-type": "application/pdf"},
                                chunks=[b"%PDF-1.5 ", b"body"]),
        ]
        import os
        os.environ["META_WA_TOKEN"] = "secret-token"
        try:
            media = wm.InboundMedia(kind="document",
                                    link="https://graph.facebook.com/file.pdf")
            with _patch_client(_SeqAsyncClient):
                out = _run(wm.download_media(media))
        finally:
            os.environ.pop("META_WA_TOKEN", None)
        self.assertIsNotNone(out)
        self.assertEqual(out.mime_type, "application/pdf")
        self.assertEqual(out.content, b"%PDF-1.5 body")
        # Hop 1 carried Authorization; hop 2 (different host) must NOT.
        self.assertEqual(len(_SeqAsyncClient.opened), 2)
        _, h1 = _SeqAsyncClient.opened[0]
        _, h2 = _SeqAsyncClient.opened[1]
        self.assertIn("Authorization", h1)
        self.assertNotIn("Authorization", h2)

    def test_redirect_loop_capped(self):
        # A self-referential redirect (same allowed host) is chased only up to
        # the hop cap, then refused.
        loop = _FakeStreamResponse(status_code=302,
                                   headers={"location": "https://lookaside.fbsbx.com/again"})
        _SeqAsyncClient.queue = [loop, loop, loop, loop, loop]
        media = wm.InboundMedia(kind="document", link="https://lookaside.fbsbx.com/start")
        with _patch_client(_SeqAsyncClient):
            self.assertIsNone(_run(wm.download_media(media)))
        # Capped at _MAX_REDIRECT_HOPS hops opened (no infinite chase).
        self.assertLessEqual(len(_SeqAsyncClient.opened), wm._MAX_REDIRECT_HOPS + 1)

    def test_absent_content_type_sniffed_pdf_accepted(self):
        # No content-type header, but the body starts with %PDF- → accepted as
        # application/pdf (the sniff, not the payload hint, is authority).
        _FakeAsyncClient.stream_response = _FakeStreamResponse(
            headers={}, chunks=[b"%PDF-1.4 hello"],
        )
        media = wm.InboundMedia(kind="document", link="https://lookaside.fbsbx.com/x",
                                mime_type="application/pdf")
        out = _run(wm.download_media(media))
        self.assertIsNotNone(out)
        self.assertEqual(out.mime_type, "application/pdf")

    def test_absent_content_type_unrecognized_bytes_rejected(self):
        # No content-type AND unrecognised magic bytes → rejected, even though
        # the payload hint claims application/pdf (hint is not sole authority).
        _FakeAsyncClient.stream_response = _FakeStreamResponse(
            headers={}, chunks=[b"<html>not a pdf"],
        )
        media = wm.InboundMedia(kind="document", link="https://lookaside.fbsbx.com/x",
                                mime_type="application/pdf")
        self.assertIsNone(_run(wm.download_media(media)))


# ---------------------------------------------------------------------------
# Parser tests — import the aptana parser functions. The router pulls heavy
# services at module top; stub them so the module imports light.
# ---------------------------------------------------------------------------

def _ensure_stub(name, attrs=None):
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    for k, v in (attrs or {}).items():
        setattr(mod, k, v)
    sys.modules[name] = mod


_ensure_stub("services.ai_handler", {"generate_ai_response": None, "log_to_backend": None})
_ensure_stub("services.input_sanitizer", {"sanitize_user_input": lambda s: s})
_ensure_stub("services.notifications", {"record_engagement": lambda *a: None,
                                        "record_opt_out": lambda *a: None})
_ensure_stub("services.prompt_injection_detector", {"audit_user_input": lambda *a: None})
_ensure_stub("services.whatsapp_sender", {"acknowledge_received": None, "send_text": None})

# Minimal fastapi stub (APIRouter + the symbols imported at module top).
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


class TestMediaParser(unittest.TestCase):
    def test_image_flat_shape(self):
        payload = {"messages": [{"type": "image",
                                 "image": {"id": "IMG1", "mime_type": "image/jpeg"}}]}
        m = aptana._extract_media_from_payload(payload)
        self.assertIsNotNone(m)
        self.assertEqual(m.kind, "image")
        self.assertEqual(m.media_id, "IMG1")
        self.assertEqual(m.mime_type, "image/jpeg")

    def test_document_with_link_and_filename(self):
        payload = {"messages": [{"type": "document",
                                 "document": {"id": "D1", "filename": "ktp.pdf",
                                              "mime_type": "application/pdf",
                                              "link": "https://graph.facebook.com/d1"}}]}
        m = aptana._extract_media_from_payload(payload)
        self.assertEqual(m.kind, "document")
        self.assertEqual(m.media_id, "D1")
        self.assertEqual(m.link, "https://graph.facebook.com/d1")
        self.assertEqual(m.filename, "ktp.pdf")

    def test_nested_envelope_shape(self):
        payload = {"entry": [{"changes": [{"value": {"messages": [
            {"type": "image", "image": {"id": "IMG9"}}]}}]}]}
        m = aptana._extract_media_from_payload(payload)
        self.assertEqual(m.media_id, "IMG9")

    def test_json_string_payload(self):
        import json
        payload = json.dumps({"messages": [{"type": "image", "image": {"id": "IMGX"}}]})
        m = aptana._extract_media_from_payload(payload)
        self.assertEqual(m.media_id, "IMGX")

    def test_text_message_returns_none(self):
        payload = {"messages": [{"type": "text", "text": {"body": "halo"}}]}
        self.assertIsNone(aptana._extract_media_from_payload(payload))

    def test_no_handle_returns_none(self):
        # Media type but neither id nor link → cannot download → None.
        payload = {"messages": [{"type": "image", "image": {"mime_type": "image/jpeg"}}]}
        self.assertIsNone(aptana._extract_media_from_payload(payload))


class TestPayloadShapeKeys(unittest.TestCase):
    def test_keys_only_no_values(self):
        payload = {"messages": [{"type": "image",
                                 "image": {"id": "SECRET_ID", "mime_type": "image/jpeg"}}]}
        shape = aptana._payload_shape_keys(payload)
        flat = str(shape)
        # Key names appear; the secret media id value does NOT.
        self.assertIn("message_keys", flat)
        self.assertIn("media_keys", flat)
        self.assertIn("image", flat)              # the message type value (safe)
        self.assertNotIn("SECRET_ID", flat)


if __name__ == "__main__":
    unittest.main(verbosity=2)
