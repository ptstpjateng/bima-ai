"""
Tests for the SIAP document seam — `services/siap_document_client.py`.

Run standalone (no pytest needed — matches tests/test_siap_write.py):

    python -m tests.test_siap_document_client   # from ai-engine/
    python tests/test_siap_document_client.py   # also works

What it covers:
  * SiapDocumentClient — every outcome with `httpx.AsyncClient` mocked:
    not-configured no-op (upload / list / download), 2xx success, 401/403/404
    errors, timeout, network error, empty-body download.
  * upload_document — multipart field name `file`, Bearer header, returned
    file_id (ok and fail).
  * list_documents — flat + `data`-nested list shapes, dedupe-friendly output.
  * download_document — raw bytes on 2xx, None on ANY failure (never raises).
  * The bearer token NEVER appears in any log line (secret hygiene).

`httpx` is mocked at the `AsyncClient` boundary so no real network and no SIAP
instance is needed. The client is exercised through its public methods exactly
as guided_submission / officer_bridge call them.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Make `services` importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.siap_document_client import SiapDocumentClient  # noqa: E402


# ---------------------------------------------------------------------------
# httpx mock helpers
# ---------------------------------------------------------------------------


def _mock_json_response(status_code: int, json_body: dict | list | None = None,
                        text: str = "") -> MagicMock:
    """Fake httpx.Response with .json()/.status_code/.text (for POST/list)."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or (str(json_body) if json_body is not None else "")
    if json_body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_body
    return resp


def _mock_stream_response(
    status_code: int,
    chunks: list[bytes] | None = None,
    *,
    content_length: str | None = None,
) -> MagicMock:
    """Fake httpx streaming response for the download path.

    `download_document` now uses `client.stream("GET", ...)` — a SIZE-CAPPED
    streamed read (Content-Length pre-flight + `aiter_bytes()` abort) — so the
    response must expose `.status_code`, `.headers`, and an async `.aiter_bytes()`
    yielding `chunks`. Returned object is itself the async context manager that
    `stream(...)` yields (its __aenter__ returns self).
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-length": content_length} if content_length else {}
    resp.text = ""

    async def _aiter_bytes():
        for c in (chunks or []):
            yield c

    resp.aiter_bytes = _aiter_bytes
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _patch_httpx(post_mock: AsyncMock | None = None,
                 get_mock: AsyncMock | None = None,
                 stream_mock: MagicMock | Exception | None = None):
    """Patch `httpx.AsyncClient` so its async ctx yields a client whose
    `.post`/`.get`/`.stream` are the supplied mocks.

    `stream_mock` may be a response CM (returned from `client.stream(...)`) or an
    Exception instance to raise from the `.stream(...)` call (timeout/network)."""
    fake_client = MagicMock()
    if post_mock is not None:
        fake_client.post = post_mock
    if get_mock is not None:
        fake_client.get = get_mock
    if stream_mock is not None:
        if isinstance(stream_mock, Exception):
            fake_client.stream = MagicMock(side_effect=stream_mock)
        else:
            fake_client.stream = MagicMock(return_value=stream_mock)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=fake_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "services.siap_document_client.httpx.AsyncClient",
        return_value=ctx,
    )


_SECRET_TOKEN = "tok-doc-SECRET-xyz-987"


def _configured_client() -> SiapDocumentClient:
    """A doc client with both base + token set (so is_configured is True)."""
    return SiapDocumentClient(
        base="http://siap.test", token=_SECRET_TOKEN, timeout=5.0
    )


# ===========================================================================
# Configuration / not-configured no-op
# ===========================================================================


class TestConfiguration(unittest.TestCase):

    def test_blank_token_is_not_configured(self):
        c = SiapDocumentClient(base="http://siap.test", token="", timeout=5.0)
        # is_configured is a METHOD (parity with the other SIAP clients).
        self.assertFalse(c.is_configured())

    def test_blank_base_is_not_configured(self):
        c = SiapDocumentClient(base="", token="tok", timeout=5.0)
        self.assertFalse(c.is_configured())

    def test_both_set_is_configured(self):
        self.assertTrue(_configured_client().is_configured())

    def test_upload_not_configured_no_network(self):
        c = SiapDocumentClient(base="", token="", timeout=5.0)
        post = AsyncMock()
        with _patch_httpx(post_mock=post):
            result = asyncio.run(
                c.upload_document(1, "ktp.pdf", b"x", "application/pdf")
            )
        self.assertFalse(result["ok"])
        self.assertFalse(result["configured"])
        self.assertIsNone(result["file_id"])
        self.assertIn("belum dikonfigurasi", result["note"])
        post.assert_not_called()

    def test_list_not_configured_no_network(self):
        c = SiapDocumentClient(base="", token="", timeout=5.0)
        get = AsyncMock()
        with _patch_httpx(get_mock=get):
            result = asyncio.run(c.list_documents(1))
        self.assertFalse(result["ok"])
        self.assertFalse(result["configured"])
        self.assertEqual(result["documents"], [])
        get.assert_not_called()

    def test_download_not_configured_returns_none_no_network(self):
        c = SiapDocumentClient(base="", token="", timeout=5.0)
        stream = _mock_stream_response(200, [b"x"])
        with _patch_httpx(stream_mock=stream) as p:
            result = asyncio.run(c.download_document(1))
        self.assertIsNone(result)
        # Short-circuited on is_configured() → no network at all.
        p  # (patch installed; stream never invoked because we returned early)


# ===========================================================================
# upload_document
# ===========================================================================


class TestUploadDocument(unittest.TestCase):

    def test_upload_success_returns_file_id(self):
        body = {"id": 5521, "filename": "ktp.pdf", "mime": "application/pdf"}
        post = AsyncMock(return_value=_mock_json_response(201, body))
        with _patch_httpx(post_mock=post):
            result = asyncio.run(_configured_client().upload_document(
                77294, "ktp.pdf", b"PDFBYTES", "application/pdf"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["configured"])
        self.assertEqual(result["file_id"], 5521)
        # Call shape: URL, Bearer header, multipart `file` field.
        _, kwargs = post.call_args
        called_url = post.call_args[0][0]
        self.assertTrue(
            called_url.endswith("/api/v1/license-request/77294/documents"))
        self.assertEqual(
            kwargs["headers"]["Authorization"], f"Bearer {_SECRET_TOKEN}")
        # multipart field MUST be named `file`.
        self.assertIn("file", kwargs["files"])
        fname, content, mime = kwargs["files"]["file"]
        self.assertEqual(fname, "ktp.pdf")
        self.assertEqual(content, b"PDFBYTES")
        self.assertEqual(mime, "application/pdf")
        # Content-Type is left to httpx to set (multipart boundary).
        self.assertNotIn("Content-Type", kwargs["headers"])

    def test_upload_success_nested_data(self):
        body = {"data": {"file_id": 9001}}
        post = AsyncMock(return_value=_mock_json_response(200, body))
        with _patch_httpx(post_mock=post):
            result = asyncio.run(_configured_client().upload_document(
                1, "x.pdf", b"b", "application/pdf"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["file_id"], 9001)

    def test_upload_403_explained_no_file_id(self):
        post = AsyncMock(return_value=_mock_json_response(
            403, {"message": "missing ability"}))
        with _patch_httpx(post_mock=post):
            result = asyncio.run(_configured_client().upload_document(
                1, "x.pdf", b"b", "application/pdf"))
        self.assertFalse(result["ok"])
        self.assertTrue(result["configured"])
        self.assertEqual(result["http_status"], 403)
        self.assertIsNone(result["file_id"])
        self.assertIn("tidak memiliki izin", result["note"])

    def test_upload_401_explained(self):
        post = AsyncMock(return_value=_mock_json_response(401, {}))
        with _patch_httpx(post_mock=post):
            result = asyncio.run(_configured_client().upload_document(
                1, "x.pdf", b"b", "application/pdf"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 401)
        self.assertIn("kedaluwarsa", result["note"])

    def test_upload_defaults_blank_filename_and_mime(self):
        post = AsyncMock(return_value=_mock_json_response(201, {"id": 1}))
        with _patch_httpx(post_mock=post):
            asyncio.run(_configured_client().upload_document(
                1, "", b"b", ""))
        _, kwargs = post.call_args
        fname, _content, mime = kwargs["files"]["file"]
        self.assertEqual(fname, "dokumen")
        self.assertEqual(mime, "application/octet-stream")

    def test_upload_timeout(self):
        import httpx
        post = AsyncMock(side_effect=httpx.TimeoutException("slow"))
        with _patch_httpx(post_mock=post):
            result = asyncio.run(_configured_client().upload_document(
                1, "x.pdf", b"b", "application/pdf"))
        self.assertFalse(result["ok"])
        self.assertTrue(result["configured"])
        self.assertIsNone(result["file_id"])
        self.assertIn("tepat waktu", result["note"])

    def test_upload_network_error(self):
        import httpx
        post = AsyncMock(side_effect=httpx.ConnectError("down"))
        with _patch_httpx(post_mock=post):
            result = asyncio.run(_configured_client().upload_document(
                1, "x.pdf", b"b", "application/pdf"))
        self.assertFalse(result["ok"])
        self.assertIn("jaringan", result["note"])

    def test_upload_bad_request_id_rejected_locally(self):
        post = AsyncMock()
        with _patch_httpx(post_mock=post):
            result = asyncio.run(_configured_client().upload_document(
                "not-a-number", "x.pdf", b"b", "application/pdf"))  # type: ignore[arg-type]
        self.assertFalse(result["ok"])
        self.assertIsNone(result["file_id"])
        post.assert_not_called()


# ===========================================================================
# list_documents
# ===========================================================================


class TestListDocuments(unittest.TestCase):

    def test_list_success_real_siap_shape(self):
        # SIAP's DocumentController nests the list under data.documents and uses
        # file_name / file_type / created_on (NOT filename / mime / created_at).
        # The client must NORMALISE these so the officer loader reads a populated
        # filename + mime — the label drives dedupe + _resolve_doc_ref, so an
        # empty one would silently break both.
        body = {
            "status": "success",
            "data": {
                "request_id": 77294,
                "count": 2,
                "documents": [
                    {"file_id": 1, "file_name": "ktp.pdf",
                     "file_type": "application/pdf", "created_on": "2026-05-10"},
                    {"file_id": 2, "file_name": "nib.pdf",
                     "file_type": "application/pdf", "created_on": "2026-05-10"},
                ],
            },
        }
        get = AsyncMock(return_value=_mock_json_response(200, body))
        with _patch_httpx(get_mock=get):
            result = asyncio.run(_configured_client().list_documents(77294))
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["documents"]), 2)
        first = result["documents"][0]
        # Real SIAP keys are surfaced under the loader-facing keys, non-empty.
        self.assertEqual(first["file_id"], 1)
        self.assertEqual(first["filename"], "ktp.pdf")
        self.assertEqual(first["mime"], "application/pdf")
        self.assertEqual(first["created_at"], "2026-05-10")
        called_url = get.call_args[0][0]
        self.assertTrue(
            called_url.endswith("/api/v1/license-request/77294/documents"))
        _, kwargs = get.call_args
        self.assertEqual(
            kwargs["headers"]["Authorization"], f"Bearer {_SECRET_TOKEN}")

    def test_list_success_flat_legacy_keys_fallback(self):
        # Tolerant fallback: an older/flat shape with the historical keys still
        # normalises to a populated filename / mime.
        body = [
            {"id": 1, "filename": "ktp.pdf", "mime": "application/pdf",
             "created_at": "2026-05-10"},
        ]
        get = AsyncMock(return_value=_mock_json_response(200, body))
        with _patch_httpx(get_mock=get):
            result = asyncio.run(_configured_client().list_documents(1))
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["documents"]), 1)
        self.assertEqual(result["documents"][0]["file_id"], 1)
        self.assertEqual(result["documents"][0]["filename"], "ktp.pdf")
        self.assertEqual(result["documents"][0]["mime"], "application/pdf")

    def test_list_success_nested_data_list(self):
        # `data` as a bare list (simpler shape) still works and normalises.
        body = {"data": [{"file_id": 7, "file_name": "surat.pdf",
                          "file_type": "application/pdf"}]}
        get = AsyncMock(return_value=_mock_json_response(200, body))
        with _patch_httpx(get_mock=get):
            result = asyncio.run(_configured_client().list_documents(1))
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["documents"]), 1)
        self.assertEqual(result["documents"][0]["file_id"], 7)
        self.assertEqual(result["documents"][0]["filename"], "surat.pdf")

    def test_list_403(self):
        get = AsyncMock(return_value=_mock_json_response(403, {}))
        with _patch_httpx(get_mock=get):
            result = asyncio.run(_configured_client().list_documents(1))
        self.assertFalse(result["ok"])
        self.assertEqual(result["documents"], [])
        self.assertIn("tidak memiliki izin", result["note"])

    def test_list_404(self):
        get = AsyncMock(return_value=_mock_json_response(404, {}))
        with _patch_httpx(get_mock=get):
            result = asyncio.run(_configured_client().list_documents(1))
        self.assertFalse(result["ok"])
        self.assertIn("tidak menemukan", result["note"])

    def test_list_timeout(self):
        import httpx
        get = AsyncMock(side_effect=httpx.TimeoutException("slow"))
        with _patch_httpx(get_mock=get):
            result = asyncio.run(_configured_client().list_documents(1))
        self.assertFalse(result["ok"])
        self.assertEqual(result["documents"], [])
        self.assertIn("tepat waktu", result["note"])

    def test_list_non_list_body_yields_empty(self):
        # A 2xx that isn't a list (unexpected) → empty documents, still ok.
        get = AsyncMock(return_value=_mock_json_response(200, {"weird": True}))
        with _patch_httpx(get_mock=get):
            result = asyncio.run(_configured_client().list_documents(1))
        self.assertTrue(result["ok"])
        self.assertEqual(result["documents"], [])


# ===========================================================================
# download_document
# ===========================================================================


class TestDownloadDocument(unittest.TestCase):

    def test_download_success_returns_bytes(self):
        # Body streamed across multiple chunks — accumulated + returned whole.
        stream = _mock_stream_response(200, [b"RAWFILE", b"BYTES"])
        fake_stream = MagicMock(return_value=stream)
        fake_client = MagicMock()
        fake_client.stream = fake_stream
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=fake_client)
        ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("services.siap_document_client.httpx.AsyncClient",
                   return_value=ctx):
            result = asyncio.run(_configured_client().download_document(5521))
        self.assertEqual(result, b"RAWFILEBYTES")
        # stream("GET", url, headers=...) — verify URL + Bearer header.
        method, url = fake_stream.call_args[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/api/v1/documents/5521/download"))
        self.assertEqual(
            fake_stream.call_args.kwargs["headers"]["Authorization"],
            f"Bearer {_SECRET_TOKEN}")

    def test_download_empty_body_returns_none(self):
        stream = _mock_stream_response(200, [])
        with _patch_httpx(stream_mock=stream):
            result = asyncio.run(_configured_client().download_document(1))
        self.assertIsNone(result)

    def test_download_404_returns_none(self):
        stream = _mock_stream_response(404, [])
        with _patch_httpx(stream_mock=stream):
            result = asyncio.run(_configured_client().download_document(1))
        self.assertIsNone(result)

    def test_download_403_returns_none(self):
        stream = _mock_stream_response(403, [])
        with _patch_httpx(stream_mock=stream):
            result = asyncio.run(_configured_client().download_document(1))
        self.assertIsNone(result)

    def test_download_timeout_returns_none(self):
        import httpx
        with _patch_httpx(stream_mock=httpx.TimeoutException("slow")):
            result = asyncio.run(_configured_client().download_document(1))
        self.assertIsNone(result)

    def test_download_network_error_returns_none(self):
        import httpx
        with _patch_httpx(stream_mock=httpx.ConnectError("down")):
            result = asyncio.run(_configured_client().download_document(1))
        self.assertIsNone(result)

    def test_download_bad_file_id_returns_none_no_network(self):
        stream = _mock_stream_response(200, [b"x"])
        with _patch_httpx(stream_mock=stream):
            result = asyncio.run(
                _configured_client().download_document("nope"))  # type: ignore[arg-type]
        self.assertIsNone(result)

    def test_download_oversize_streamed_body_aborts_returns_none(self):
        # No declared Content-Length, but the streamed body blows past the cap →
        # the accumulate-and-abort guard returns None (the officer loader skips
        # it). Shrink the cap so the test stays fast and byte-light.
        import services.siap_document_client as mod
        big = [b"A" * 32, b"B" * 32]  # 64 bytes total
        stream = _mock_stream_response(200, big)  # no content_length
        with patch.object(mod, "_SIAP_DOCUMENT_MAX_BYTES", 40), \
                _patch_httpx(stream_mock=stream):
            result = asyncio.run(_configured_client().download_document(1))
        self.assertIsNone(result)

    def test_download_oversize_declared_content_length_preflight_returns_none(self):
        # Content-Length declares an oversize body → refused before reading a
        # single chunk (pre-flight).
        import services.siap_document_client as mod
        stream = _mock_stream_response(
            200, [b"should-not-be-read"], content_length="999999")
        with patch.object(mod, "_SIAP_DOCUMENT_MAX_BYTES", 100), \
                _patch_httpx(stream_mock=stream):
            result = asyncio.run(_configured_client().download_document(1))
        self.assertIsNone(result)

    def test_download_at_cap_returns_bytes(self):
        # A body exactly at the cap is fine (abort only on STRICTLY exceeding).
        import services.siap_document_client as mod
        stream = _mock_stream_response(200, [b"C" * 40])
        with patch.object(mod, "_SIAP_DOCUMENT_MAX_BYTES", 40), \
                _patch_httpx(stream_mock=stream):
            result = asyncio.run(_configured_client().download_document(1))
        self.assertEqual(result, b"C" * 40)


# ===========================================================================
# Secret hygiene — the bearer token must NEVER land in a log line.
# ===========================================================================


class TestTokenNeverLogged(unittest.TestCase):

    def _run_all_ops_capturing_logs(self) -> str:
        """Exercise upload/list/download (success + error) capturing every log
        record emitted by the client's logger, and return the joined text."""
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("bima_ai.siap_document")
        handler = _Capture()
        prev_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        # Ensure records actually flow even if root disabled elsewhere.
        logging.disable(logging.NOTSET)
        try:
            c = _configured_client()
            post_ok = AsyncMock(return_value=_mock_json_response(201, {"id": 1}))
            with _patch_httpx(post_mock=post_ok):
                asyncio.run(c.upload_document(77294, "ktp.pdf", b"b", "application/pdf"))
            post_403 = AsyncMock(return_value=_mock_json_response(403, {"message": "x"}))
            with _patch_httpx(post_mock=post_403):
                asyncio.run(c.upload_document(1, "x.pdf", b"b", "application/pdf"))
            get_list = AsyncMock(return_value=_mock_json_response(
                200, {"data": {"documents": [{"file_id": 1,
                                              "file_name": "ktp.pdf",
                                              "file_type": "application/pdf"}]}}))
            with _patch_httpx(get_mock=get_list):
                asyncio.run(c.list_documents(77294))
            stream_dl = _mock_stream_response(200, [b"z"])
            with _patch_httpx(stream_mock=stream_dl):
                asyncio.run(c.download_document(5521))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prev_level)
        # Render each record the way logging would (message + args).
        return "\n".join(r.getMessage() for r in records)

    def test_token_absent_from_all_log_messages(self):
        text = self._run_all_ops_capturing_logs()
        self.assertNotIn(_SECRET_TOKEN, text)
        self.assertNotIn("Bearer", text)


if __name__ == "__main__":
    # Quiet the module loggers so test output stays readable (the token-hygiene
    # test re-enables logging locally via its own capture handler).
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
