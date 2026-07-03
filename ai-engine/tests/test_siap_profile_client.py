"""
Tests for the SIAP profile seam — `services/siap_profile_client.py`.

Run standalone (no pytest needed — matches tests/test_siap_document_client.py):

    python -m tests.test_siap_profile_client   # from ai-engine/
    python tests/test_siap_profile_client.py   # also works

What it covers:
  * SiapProfileClient — every outcome with `httpx.AsyncClient` mocked:
    not-configured no-op, 200 (found existing), 201 (created), 422 bad NIK,
    401/403 errors, timeout, network error, locally-rejected NIK (not 16 digits)
    and missing full_name — all WITHOUT touching the network.
  * upsert_profile — Bearer header, JSON body shape (identity_number +
    full_name + whitelisted optional fields, blanks dropped), returned
    profile_id + created flag (200 vs 201).
  * PII hygiene — the bearer token, the raw 16-digit NIK, and the full name
    NEVER appear in any log line.

`httpx` is mocked at the `AsyncClient` boundary so no real network and no SIAP
instance is needed. The client is exercised through its public method exactly as
guided_submission calls it.
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

from services.siap_profile_client import SiapProfileClient  # noqa: E402


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
        "services.siap_profile_client.httpx.AsyncClient",
        return_value=ctx,
    )


_SECRET_TOKEN = "tok-profile-SECRET-abc-321"
_VALID_NIK = "3374012345678901"          # exactly 16 digits
_FULL_NAME = "Budi Santoso Wijaya"


def _configured_client() -> SiapProfileClient:
    """A profile client with both base + token set (is_configured True)."""
    return SiapProfileClient(
        base="http://siap.test", token=_SECRET_TOKEN, timeout=5.0
    )


# ===========================================================================
# Configuration / not-configured no-op
# ===========================================================================


class TestConfiguration(unittest.TestCase):

    def test_blank_token_is_not_configured(self):
        c = SiapProfileClient(base="http://siap.test", token="", timeout=5.0)
        # is_configured is a METHOD (parity with the other SIAP clients).
        self.assertFalse(c.is_configured())

    def test_blank_base_is_not_configured(self):
        c = SiapProfileClient(base="", token="tok", timeout=5.0)
        self.assertFalse(c.is_configured())

    def test_both_set_is_configured(self):
        self.assertTrue(_configured_client().is_configured())

    def test_upsert_not_configured_no_network(self):
        c = SiapProfileClient(base="", token="", timeout=5.0)
        post = AsyncMock()
        with _patch_httpx(post):
            result = asyncio.run(
                c.upsert_profile(identity_number=_VALID_NIK, full_name=_FULL_NAME)
            )
        self.assertFalse(result["ok"])
        self.assertFalse(result["configured"])
        self.assertIsNone(result["profile_id"])
        self.assertIsNone(result["created"])
        self.assertIn("belum dikonfigurasi", result["note"])
        post.assert_not_called()


# ===========================================================================
# upsert_profile — success shapes
# ===========================================================================


class TestUpsertSuccess(unittest.TestCase):

    def test_found_existing_returns_profile_id_created_false(self):
        # 200 → resolved an existing profile (created:false).
        body = {"status": "success", "data": {"profile_id": 40912, "created": False}}
        post = AsyncMock(return_value=_mock_json_response(200, body))
        with _patch_httpx(post):
            result = asyncio.run(_configured_client().upsert_profile(
                identity_number=_VALID_NIK, full_name=_FULL_NAME))
        self.assertTrue(result["ok"])
        self.assertTrue(result["configured"])
        self.assertEqual(result["profile_id"], 40912)
        self.assertIs(result["created"], False)
        self.assertEqual(result["http_status"], 200)
        # Call shape: URL, Bearer header, JSON body with identity_number + name.
        called_url = post.call_args[0][0]
        self.assertTrue(called_url.endswith("/api/v1/person-profile"))
        _, kwargs = post.call_args
        self.assertEqual(
            kwargs["headers"]["Authorization"], f"Bearer {_SECRET_TOKEN}")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.assertEqual(kwargs["json"]["identity_number"], _VALID_NIK)
        self.assertEqual(kwargs["json"]["full_name"], _FULL_NAME)

    def test_created_returns_profile_id_created_true(self):
        # 201 → newly created (created:true).
        body = {"status": "success", "data": {"profile_id": 55123, "created": True}}
        post = AsyncMock(return_value=_mock_json_response(201, body))
        with _patch_httpx(post):
            result = asyncio.run(_configured_client().upsert_profile(
                identity_number=_VALID_NIK, full_name=_FULL_NAME))
        self.assertTrue(result["ok"])
        self.assertEqual(result["profile_id"], 55123)
        self.assertIs(result["created"], True)
        self.assertEqual(result["http_status"], 201)

    def test_optional_fields_forwarded_and_blanks_dropped(self):
        body = {"data": {"profile_id": 1, "created": False}}
        post = AsyncMock(return_value=_mock_json_response(200, body))
        with _patch_httpx(post):
            asyncio.run(_configured_client().upsert_profile(
                identity_number=_VALID_NIK,
                full_name=_FULL_NAME,
                address="Jl. Merdeka No. 1",
                gender="LAKI-LAKI",
                mobile_phone="6281234567890",
                email="",          # blank → dropped
                birth_place=None,  # None → dropped
                unknown_field="x",  # not whitelisted → dropped
            ))
        _, kwargs = post.call_args
        sent = kwargs["json"]
        self.assertEqual(sent["address"], "Jl. Merdeka No. 1")
        self.assertEqual(sent["gender"], "LAKI-LAKI")
        self.assertEqual(sent["mobile_phone"], "6281234567890")
        self.assertNotIn("email", sent)          # blank dropped
        self.assertNotIn("birth_place", sent)    # None dropped
        self.assertNotIn("unknown_field", sent)  # non-whitelisted dropped

    def test_flat_shape_profile_id_at_top_level(self):
        # A flatter 2xx (no `data` wrapper) still yields the profile_id.
        body = {"profile_id": 777}
        post = AsyncMock(return_value=_mock_json_response(200, body))
        with _patch_httpx(post):
            result = asyncio.run(_configured_client().upsert_profile(
                identity_number=_VALID_NIK, full_name=_FULL_NAME))
        self.assertTrue(result["ok"])
        self.assertEqual(result["profile_id"], 777)


# ===========================================================================
# upsert_profile — local NIK / name validation (no network)
# ===========================================================================


class TestLocalValidation(unittest.TestCase):

    def test_nik_too_short_rejected_locally(self):
        post = AsyncMock()
        with _patch_httpx(post):
            result = asyncio.run(_configured_client().upsert_profile(
                identity_number="12345", full_name=_FULL_NAME))
        self.assertFalse(result["ok"])
        self.assertTrue(result["configured"])
        self.assertIsNone(result["profile_id"])
        self.assertIn("16 digit", result["note"])
        post.assert_not_called()

    def test_nik_non_digit_rejected_locally(self):
        post = AsyncMock()
        with _patch_httpx(post):
            result = asyncio.run(_configured_client().upsert_profile(
                identity_number="33740123456789ab", full_name=_FULL_NAME))
        self.assertFalse(result["ok"])
        self.assertIsNone(result["profile_id"])
        post.assert_not_called()

    def test_non_ascii_fullwidth_digits_rejected_locally(self):
        # Full-width numerals ("３３…") pass str.isdigit() but SIAP's `[0-9]{16}`
        # regex 422-rejects them — the ASCII gate must catch this class LOCALLY
        # so we fall back instead of sending a bad NIK to SIAP.
        fullwidth = "３３７４０１２３４５６７８９０１"  # 16 full-width digits
        self.assertTrue(fullwidth.isdigit())        # would slip the old gate
        self.assertEqual(len(fullwidth), 16)
        post = AsyncMock()
        with _patch_httpx(post):
            result = asyncio.run(_configured_client().upsert_profile(
                identity_number=fullwidth, full_name=_FULL_NAME))
        self.assertFalse(result["ok"])
        self.assertTrue(result["configured"])
        self.assertIsNone(result["profile_id"])
        self.assertIn("16 digit", result["note"])
        post.assert_not_called()                    # NEVER sent to SIAP

    def test_blank_nik_rejected_locally(self):
        post = AsyncMock()
        with _patch_httpx(post):
            result = asyncio.run(_configured_client().upsert_profile(
                identity_number="", full_name=_FULL_NAME))
        self.assertFalse(result["ok"])
        post.assert_not_called()

    def test_blank_full_name_rejected_locally(self):
        post = AsyncMock()
        with _patch_httpx(post):
            result = asyncio.run(_configured_client().upsert_profile(
                identity_number=_VALID_NIK, full_name="   "))
        self.assertFalse(result["ok"])
        self.assertIsNone(result["profile_id"])
        post.assert_not_called()


# ===========================================================================
# upsert_profile — error outcomes
# ===========================================================================


class TestUpsertErrors(unittest.TestCase):

    def test_422_bad_nik_from_siap_explained(self):
        post = AsyncMock(return_value=_mock_json_response(
            422, {"message": "identity_number invalid"}))
        with _patch_httpx(post):
            result = asyncio.run(_configured_client().upsert_profile(
                identity_number=_VALID_NIK, full_name=_FULL_NAME))
        self.assertFalse(result["ok"])
        self.assertTrue(result["configured"])
        self.assertEqual(result["http_status"], 422)
        self.assertIsNone(result["profile_id"])
        self.assertIn("tidak valid", result["note"])

    def test_401_explained(self):
        post = AsyncMock(return_value=_mock_json_response(401, {}))
        with _patch_httpx(post):
            result = asyncio.run(_configured_client().upsert_profile(
                identity_number=_VALID_NIK, full_name=_FULL_NAME))
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 401)
        self.assertIn("kedaluwarsa", result["note"])

    def test_403_explained(self):
        post = AsyncMock(return_value=_mock_json_response(
            403, {"message": "missing ability"}))
        with _patch_httpx(post):
            result = asyncio.run(_configured_client().upsert_profile(
                identity_number=_VALID_NIK, full_name=_FULL_NAME))
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 403)
        self.assertIn("tidak memiliki izin", result["note"])

    def test_timeout(self):
        import httpx
        post = AsyncMock(side_effect=httpx.TimeoutException("slow"))
        with _patch_httpx(post):
            result = asyncio.run(_configured_client().upsert_profile(
                identity_number=_VALID_NIK, full_name=_FULL_NAME))
        self.assertFalse(result["ok"])
        self.assertTrue(result["configured"])
        self.assertIsNone(result["profile_id"])
        self.assertIn("tepat waktu", result["note"])

    def test_network_error(self):
        import httpx
        post = AsyncMock(side_effect=httpx.ConnectError("down"))
        with _patch_httpx(post):
            result = asyncio.run(_configured_client().upsert_profile(
                identity_number=_VALID_NIK, full_name=_FULL_NAME))
        self.assertFalse(result["ok"])
        self.assertIn("jaringan", result["note"])


# ===========================================================================
# PII hygiene — token, raw NIK, and full name must NEVER land in a log line.
# ===========================================================================


class TestPiiNeverLogged(unittest.TestCase):

    def _run_all_ops_capturing_logs(self) -> str:
        """Exercise upsert (success + local-reject + error) capturing every log
        record emitted by the client's logger, and return the joined text."""
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("bima_ai.siap_profile")
        handler = _Capture()
        prev_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logging.disable(logging.NOTSET)
        try:
            c = _configured_client()
            ok = AsyncMock(return_value=_mock_json_response(
                200, {"data": {"profile_id": 40912, "created": False}}))
            with _patch_httpx(ok):
                asyncio.run(c.upsert_profile(
                    identity_number=_VALID_NIK, full_name=_FULL_NAME,
                    address="Jl. Rahasia No. 9"))
            # Local reject (short NIK) — must log a masked NIK, never the input.
            with _patch_httpx(AsyncMock()):
                asyncio.run(c.upsert_profile(
                    identity_number="12345", full_name=_FULL_NAME))
            err = AsyncMock(return_value=_mock_json_response(403, {"message": "x"}))
            with _patch_httpx(err):
                asyncio.run(c.upsert_profile(
                    identity_number=_VALID_NIK, full_name=_FULL_NAME))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prev_level)
        return "\n".join(r.getMessage() for r in records)

    def test_token_nik_and_name_absent_from_all_log_messages(self):
        text = self._run_all_ops_capturing_logs()
        # Secret token never logged.
        self.assertNotIn(_SECRET_TOKEN, text)
        self.assertNotIn("Bearer", text)
        # Raw 16-digit NIK never logged (only a masked last-4 form may appear).
        self.assertNotIn(_VALID_NIK, text)
        # Full name never logged at all.
        self.assertNotIn(_FULL_NAME, text)
        self.assertNotIn("Budi", text)

    def test_error_log_path_never_contains_nik_even_when_body_echoes_it(self):
        # The non-2xx branch must NOT log the raw resp.text — a Laravel 422 body
        # frequently echoes the offending field value (here the raw NIK + name)
        # right back. Simulate exactly that and assert neither the raw NIK nor
        # the full name lands in any log line (only the masked NIK may appear).
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("bima_ai.siap_profile")
        handler = _Capture()
        prev_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logging.disable(logging.NOTSET)
        try:
            # 422 whose JSON message + raw text body echo the raw NIK and name.
            echo_body = {
                "message": f"identity_number {_VALID_NIK} already bound",
                "errors": {"full_name": [f"{_FULL_NAME} conflict"]},
            }
            resp = _mock_json_response(
                422, echo_body,
                text=(f'{{"identity_number":"{_VALID_NIK}",'
                      f'"full_name":"{_FULL_NAME}"}}'),
            )
            post = AsyncMock(return_value=resp)
            with _patch_httpx(post):
                result = asyncio.run(_configured_client().upsert_profile(
                    identity_number=_VALID_NIK, full_name=_FULL_NAME))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prev_level)

        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 422)
        text = "\n".join(r.getMessage() for r in records)
        # The error-log path emitted at least one line...
        self.assertTrue(text)
        # ...but the raw NIK and the full name are NOT in it (FIX 1: the branch
        # logs only the masked NIK + status + parsed siap_message, never the raw
        # resp.text body that echoed them). A masked last-4 form may still show.
        self.assertNotIn(_VALID_NIK, text)
        self.assertNotIn(_FULL_NAME, text)
        self.assertNotIn("Budi", text)
        # Sanity: the raw resp.text (the body that echoed the NIK) is not logged.
        self.assertNotIn('"identity_number"', text)


if __name__ == "__main__":
    # Quiet the module loggers so test output stays readable (the PII-hygiene
    # test re-enables logging locally via its own capture handler).
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
