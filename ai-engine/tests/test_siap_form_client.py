"""
Tests for the SIAP form seam — `services/siap_form_client.py`.

Run standalone (no pytest needed — matches tests/test_siap_profile_client.py):

    python -m tests.test_siap_form_client   # from ai-engine/
    python tests/test_siap_form_client.py   # also works

What it covers:
  * SiapFormClient — every outcome with `httpx.AsyncClient` mocked:
    not-configured no-op, 200 (updated), 201 (created), 422 (bad field/slot),
    401/403 errors, timeout, network error, bad request_id/form_id — all WITHOUT
    touching the network.
  * upsert_form — Bearer header, JSON body shape (form_id + fields + files,
    blank text values dropped, non-int file_ids dropped), returned form_value_id
    + fields_set + files_attached.
  * PII hygiene — the bearer token and the applicant field VALUES NEVER appear in
    any log line; a 4xx body that echoes a NIK is scrubbed.

`httpx` is mocked at the `AsyncClient` boundary so no real network and no SIAP
instance is needed. The client is exercised through its public method exactly as
the officer copilot's fill_siap_form tool calls it.
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

from services.siap_form_client import SiapFormClient  # noqa: E402


# ---------------------------------------------------------------------------
# httpx mock helpers
# ---------------------------------------------------------------------------


def _mock_json_response(status_code: int, json_body: dict | list | None = None,
                        text: str = "") -> MagicMock:
    """Fake httpx.Response with .json()/.status_code/.text."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or (str(json_body) if json_body is not None else "")
    if json_body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_body
    return resp


def _patch_httpx(post_mock: AsyncMock):
    """Patch `httpx.AsyncClient` so its async ctx yields a client whose `.post`
    is the supplied mock."""
    fake_client = MagicMock()
    fake_client.post = post_mock
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=fake_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "services.siap_form_client.httpx.AsyncClient",
        return_value=ctx,
    )


def _timeout_exc():
    import httpx
    return httpx.TimeoutException("timed out")


def _network_exc():
    import httpx
    return httpx.ConnectError("boom")


_SECRET_TOKEN = "tok-form-SECRET-xyz-987"
_REQUEST_ID = 8123
_FORM_ID = 560
# A field-value that carries an applicant NIK — must never land raw in a log.
_NIK_VALUE = "3374012345678901"
_FIELDS = {"nama_pemohon": "Budi Santoso", "no_siup": _NIK_VALUE, "alamat": "  "}
_FILES = {"up_ktp": 991, "up_permohonan": 992}


def _configured_client() -> SiapFormClient:
    return SiapFormClient(base="http://siap.test", token=_SECRET_TOKEN, timeout=5.0)


# ===========================================================================
# Configuration / not-configured no-op
# ===========================================================================


class TestConfiguration(unittest.TestCase):

    def test_blank_token_is_not_configured(self):
        c = SiapFormClient(base="http://siap.test", token="", timeout=5.0)
        self.assertFalse(c.is_configured())

    def test_blank_base_is_not_configured(self):
        c = SiapFormClient(base="", token="tok", timeout=5.0)
        self.assertFalse(c.is_configured())

    def test_both_set_is_configured(self):
        self.assertTrue(_configured_client().is_configured())

    def test_upsert_not_configured_no_network(self):
        c = SiapFormClient(base="", token="", timeout=5.0)
        post = AsyncMock()
        with _patch_httpx(post):
            result = asyncio.run(
                c.upsert_form(_REQUEST_ID, _FORM_ID, _FIELDS, _FILES)
            )
        self.assertFalse(result["ok"])
        self.assertFalse(result["configured"])
        self.assertIsNone(result["form_value_id"])
        self.assertEqual(result["fields_set"], [])
        self.assertEqual(result["files_attached"], [])
        self.assertIn("belum dikonfigurasi", result["note"])
        post.assert_not_called()


# ===========================================================================
# upsert_form — success shapes
# ===========================================================================


class TestUpsertSuccess(unittest.TestCase):

    def test_created_201_returns_ids(self):
        body = {"status": "success", "data": {
            "form_value_id": 71234, "request_id": _REQUEST_ID, "form_id": _FORM_ID,
            "fields_set": ["nama_pemohon", "no_siup"],
            "files_attached": ["up_ktp", "up_permohonan"],
        }}
        post = AsyncMock(return_value=_mock_json_response(201, body))
        with _patch_httpx(post):
            result = asyncio.run(
                _configured_client().upsert_form(_REQUEST_ID, _FORM_ID, _FIELDS, _FILES)
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["configured"])
        self.assertEqual(result["http_status"], 201)
        self.assertEqual(result["form_value_id"], 71234)
        self.assertEqual(result["fields_set"], ["nama_pemohon", "no_siup"])
        self.assertEqual(result["files_attached"], ["up_ktp", "up_permohonan"])
        self.assertEqual(result["note"], "")

    def test_updated_200_returns_ids(self):
        body = {"data": {"form_value_id": 71234, "fields_set": [], "files_attached": []}}
        post = AsyncMock(return_value=_mock_json_response(200, body))
        with _patch_httpx(post):
            result = asyncio.run(
                _configured_client().upsert_form(_REQUEST_ID, _FORM_ID, {}, {})
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["form_value_id"], 71234)

    def test_request_shape_bearer_and_json_body(self):
        post = AsyncMock(return_value=_mock_json_response(
            200, {"data": {"form_value_id": 1}}))
        with _patch_httpx(post):
            asyncio.run(
                _configured_client().upsert_form(_REQUEST_ID, _FORM_ID, _FIELDS, _FILES)
            )
        args, kwargs = post.call_args
        # URL addresses the request's form-value endpoint.
        url = args[0] if args else kwargs.get("url")
        self.assertEqual(
            url, f"http://siap.test/api/v1/license-request/{_REQUEST_ID}/form-value"
        )
        # Bearer + Accept headers.
        self.assertEqual(
            kwargs["headers"]["Authorization"], f"Bearer {_SECRET_TOKEN}")
        self.assertEqual(kwargs["headers"]["Accept"], "application/json")
        # JSON body: form_id + fields + files. The blank alamat value is DROPPED
        # (SIAP trims to "" anyway); the two real fields stay.
        body = kwargs["json"]
        self.assertEqual(body["form_id"], _FORM_ID)
        self.assertEqual(
            set(body["fields"].keys()), {"nama_pemohon", "no_siup"})
        self.assertNotIn("alamat", body["fields"])  # blank dropped
        # Files coerced to {str: int}.
        self.assertEqual(body["files"], {"up_ktp": 991, "up_permohonan": 992})

    def test_non_int_file_id_dropped_not_422(self):
        # A slot whose file_id is not an int is dropped locally, not sent.
        post = AsyncMock(return_value=_mock_json_response(
            200, {"data": {"form_value_id": 1}}))
        with _patch_httpx(post):
            asyncio.run(_configured_client().upsert_form(
                _REQUEST_ID, _FORM_ID, {},
                {"up_ktp": "not-an-int", "up_permohonan": 992}))
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["files"], {"up_permohonan": 992})


# ===========================================================================
# upsert_form — failure shapes
# ===========================================================================


class TestUpsertFailures(unittest.TestCase):

    def test_422_bad_field_or_slot(self):
        body = {"status": "error", "message": "The given data was invalid.",
                "errors": {"files": ["Unknown or non-file slot(s): up_bogus"]}}
        post = AsyncMock(return_value=_mock_json_response(422, body))
        with _patch_httpx(post):
            result = asyncio.run(
                _configured_client().upsert_form(_REQUEST_ID, _FORM_ID, _FIELDS, _FILES)
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["configured"])
        self.assertEqual(result["http_status"], 422)
        self.assertIsNone(result["form_value_id"])
        self.assertIn("tidak valid", result["note"])

    def test_401_unauthorized(self):
        post = AsyncMock(return_value=_mock_json_response(401, {"message": "no"}))
        with _patch_httpx(post):
            result = asyncio.run(
                _configured_client().upsert_form(_REQUEST_ID, _FORM_ID, _FIELDS, _FILES)
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 401)
        self.assertIn("kedaluwarsa", result["note"])

    def test_403_forbidden_mentions_ability(self):
        post = AsyncMock(return_value=_mock_json_response(403, {"message": "x"}))
        with _patch_httpx(post):
            result = asyncio.run(
                _configured_client().upsert_form(_REQUEST_ID, _FORM_ID, _FIELDS, _FILES)
            )
        self.assertFalse(result["ok"])
        self.assertIn("form:write", result["note"])

    def test_404_request_not_found(self):
        post = AsyncMock(return_value=_mock_json_response(404, {"message": "gone"}))
        with _patch_httpx(post):
            result = asyncio.run(
                _configured_client().upsert_form(_REQUEST_ID, _FORM_ID, _FIELDS, _FILES)
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 404)

    def test_timeout_degrades(self):
        post = AsyncMock(side_effect=_timeout_exc())
        with _patch_httpx(post):
            result = asyncio.run(
                _configured_client().upsert_form(_REQUEST_ID, _FORM_ID, _FIELDS, _FILES)
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["configured"])
        self.assertIn("tepat waktu", result["note"])

    def test_network_error_degrades(self):
        post = AsyncMock(side_effect=_network_exc())
        with _patch_httpx(post):
            result = asyncio.run(
                _configured_client().upsert_form(_REQUEST_ID, _FORM_ID, _FIELDS, _FILES)
            )
        self.assertFalse(result["ok"])
        self.assertIn("jaringan", result["note"])

    def test_bad_request_id_rejected_locally(self):
        post = AsyncMock()
        with _patch_httpx(post):
            result = asyncio.run(
                _configured_client().upsert_form("abc", _FORM_ID, _FIELDS, _FILES)
            )
        self.assertFalse(result["ok"])
        self.assertIn("request_id", result["note"])
        post.assert_not_called()

    def test_bad_form_id_rejected_locally(self):
        post = AsyncMock()
        with _patch_httpx(post):
            result = asyncio.run(
                _configured_client().upsert_form(_REQUEST_ID, None, _FIELDS, _FILES)
            )
        self.assertFalse(result["ok"])
        self.assertIn("form_id", result["note"])
        post.assert_not_called()


# ===========================================================================
# PII hygiene — token + field VALUES must NEVER land in a log line.
# ===========================================================================


class TestPiiNeverLogged(unittest.TestCase):

    def _capture(self):
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("bima_ai.siap_form")
        handler = _Capture()
        prev_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logging.disable(logging.NOTSET)
        return logger, handler, prev_level, records

    def test_token_and_field_values_absent_from_logs(self):
        logger, handler, prev_level, records = self._capture()
        try:
            c = _configured_client()
            ok = AsyncMock(return_value=_mock_json_response(
                200, {"data": {"form_value_id": 1,
                               "fields_set": ["nama_pemohon", "no_siup"],
                               "files_attached": ["up_ktp"]}}))
            with _patch_httpx(ok):
                asyncio.run(c.upsert_form(_REQUEST_ID, _FORM_ID, _FIELDS, _FILES))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prev_level)
        text = "\n".join(r.getMessage() for r in records)
        self.assertTrue(text)  # something WAS logged (keys + counts)
        # Secret token never logged.
        self.assertNotIn(_SECRET_TOKEN, text)
        self.assertNotIn("Bearer", text)
        # Field VALUES never logged — the applicant name and the NIK value.
        self.assertNotIn("Budi Santoso", text)
        self.assertNotIn(_NIK_VALUE, text)
        # But the field-NAME keys ARE fine to log (they're not PII).
        self.assertIn("nama_pemohon", text)
        self.assertIn("up_ktp", text)

    def test_4xx_echo_of_nik_is_scrubbed(self):
        # A Laravel 422 body can echo an offending field VALUE (a NIK) back. The
        # non-2xx branch must scrub the parsed message before logging it.
        logger, handler, prev_level, records = self._capture()
        try:
            echo_body = {"message": f"value {_NIK_VALUE} rejected"}
            resp = _mock_json_response(
                422, echo_body,
                text=f'{{"no_siup":"{_NIK_VALUE}"}}')  # raw body echoes it too
            post = AsyncMock(return_value=resp)
            with _patch_httpx(post):
                result = asyncio.run(
                    _configured_client().upsert_form(_REQUEST_ID, _FORM_ID, _FIELDS, _FILES)
                )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prev_level)
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 422)
        text = "\n".join(r.getMessage() for r in records)
        # The raw NIK is NOT in any log line (masked at most), and the raw
        # resp.text body that echoed it is not logged.
        self.assertNotIn(_NIK_VALUE, text)
        self.assertNotIn('"no_siup"', text)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
