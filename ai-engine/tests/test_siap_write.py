"""
Tests for the SIAP write seam — `services/siap_write_client.py` and the
officer-copilot write tools (`forward_case` / `record_decision`).

Run standalone (no pytest needed — matches data-pipeline/test_agents.py):

    python -m tests.test_siap_write       # from ai-engine/
    python tests/test_siap_write.py       # also works

What it covers:
  * SiapWriteClient — every outcome with `httpx.AsyncClient` mocked:
    not-configured, 2xx success, 403/404/409/422 errors, timeout,
    network error, note/decision validation.
  * The OFFICER-IN-THE-LOOP guard — `forward_case` / `record_decision`
    perform NO write unless `confirmed is True`; with confirmed
    false/absent they return a draft and never touch the write client.

`httpx` is mocked at the `AsyncClient` boundary so no real network and no
SIAP instance is needed. The write client is exercised through its public
methods exactly as the copilot calls them.
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Make `services` importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Stub the heavy ML / DB dependencies the officer-copilot module imports
# transitively (chromadb, transformers, sentence_transformers, asyncpg). They
# are not relevant to the write-seam logic under test, and installing the full
# ML stack just to import one module is wasteful. The stubs only need to make
# `import` succeed — every code path that would actually USE them is mocked in
# the individual tests. If a real dep is installed it is left untouched.
# ---------------------------------------------------------------------------


def _ensure_stub(name: str, attrs: dict | None = None) -> None:
    if name in sys.modules:
        return
    try:  # prefer the real module if it happens to be installed
        __import__(name)
        return
    except ImportError:
        pass
    mod = types.ModuleType(name)
    for attr, value in (attrs or {}).items():
        setattr(mod, attr, value)
    sys.modules[name] = mod


_ensure_stub("chromadb")
_ensure_stub("chromadb.config", {"Settings": object})
_ensure_stub("asyncpg")
_ensure_stub("sentence_transformers", {"SentenceTransformer": object})
_ensure_stub("transformers")

from services.siap_write_client import SiapWriteClient  # noqa: E402


# ---------------------------------------------------------------------------
# httpx mock helpers
# ---------------------------------------------------------------------------


def _mock_response(status_code: int, json_body: dict | None = None,
                   text: str = "") -> MagicMock:
    """Build a fake httpx.Response with .json()/.status_code/.text."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or (str(json_body) if json_body is not None else "")
    if json_body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_body
    return resp


def _patch_httpx_post(post_mock: AsyncMock):
    """
    Return a patch context for `httpx.AsyncClient` whose async context
    manager yields an object whose `.post` is `post_mock`.
    """
    fake_client = MagicMock()
    fake_client.post = post_mock
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=fake_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "services.siap_write_client.httpx.AsyncClient",
        return_value=ctx,
    )


def _configured_client() -> SiapWriteClient:
    """A write client with both base + token set (so is_configured() is True)."""
    return SiapWriteClient(
        base="http://siap.test", token="tok-write-abc", timeout=5.0
    )


# ===========================================================================
# SiapWriteClient — configuration
# ===========================================================================


class TestWriteClientConfiguration(unittest.TestCase):

    def test_blank_token_is_not_configured(self):
        c = SiapWriteClient(base="http://siap.test", token="", timeout=5.0)
        self.assertFalse(c.is_configured())

    def test_blank_base_is_not_configured(self):
        c = SiapWriteClient(base="", token="tok", timeout=5.0)
        self.assertFalse(c.is_configured())

    def test_both_set_is_configured(self):
        self.assertTrue(_configured_client().is_configured())

    def test_forward_not_configured_returns_structured_result(self):
        c = SiapWriteClient(base="", token="", timeout=5.0)
        result = asyncio.run(c.forward_request(123, note="x"))
        self.assertFalse(result["ok"])
        self.assertFalse(result["configured"])
        self.assertIn("belum dikonfigurasi", result["note"])

    def test_decision_not_configured_returns_structured_result(self):
        c = SiapWriteClient(base="", token="", timeout=5.0)
        result = asyncio.run(
            c.record_decision(123, decision="approved", notes="ok")
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["configured"])


# ===========================================================================
# SiapWriteClient.forward_request
# ===========================================================================


class TestForwardRequest(unittest.TestCase):

    def test_forward_success(self):
        body = {
            "status": "success",
            "data": {
                "request_id": 77294,
                "previous_step_id": 3664,
                "approval_step_id": 3665,
                "approval_step_status": "VERIFIKASI",
                "status": "APPROVED",
            },
        }
        post = AsyncMock(return_value=_mock_response(200, body))
        with _patch_httpx_post(post):
            result = asyncio.run(
                _configured_client().forward_request(77294, note="lengkap")
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["approval_step_id"], 3665)
        # Verify the call shape: URL, Bearer header, JSON body.
        _, kwargs = post.call_args
        called_url = post.call_args[0][0]
        self.assertTrue(called_url.endswith("/api/v1/license-request/77294/forward"))
        self.assertEqual(
            kwargs["headers"]["Authorization"], "Bearer tok-write-abc"
        )
        self.assertEqual(kwargs["json"], {"note": "lengkap"})

    def test_forward_omits_empty_note_from_body(self):
        post = AsyncMock(return_value=_mock_response(200, {"data": {}}))
        with _patch_httpx_post(post):
            asyncio.run(_configured_client().forward_request(1, note=""))
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"], {})  # no "note" key when blank

    def test_forward_note_too_long_rejected_locally(self):
        post = AsyncMock()
        with _patch_httpx_post(post):
            result = asyncio.run(
                _configured_client().forward_request(1, note="x" * 1001)
            )
        self.assertFalse(result["ok"])
        self.assertIn("terlalu panjang", result["note"])
        post.assert_not_called()  # never hit the network

    def test_forward_403_explained(self):
        post = AsyncMock(return_value=_mock_response(
            403, {"message": "missing ability"}))
        with _patch_httpx_post(post):
            result = asyncio.run(_configured_client().forward_request(1))
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 403)
        self.assertIn("tidak memiliki izin", result["note"])

    def test_forward_409_explained(self):
        post = AsyncMock(return_value=_mock_response(
            409, {"message": "last step"}))
        with _patch_httpx_post(post):
            result = asyncio.run(_configured_client().forward_request(1))
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 409)
        self.assertIn("alur kerja", result["note"])

    def test_forward_timeout(self):
        import httpx
        post = AsyncMock(side_effect=httpx.TimeoutException("slow"))
        with _patch_httpx_post(post):
            result = asyncio.run(_configured_client().forward_request(1))
        self.assertFalse(result["ok"])
        self.assertTrue(result["configured"])
        self.assertIn("tepat waktu", result["note"])

    def test_forward_network_error(self):
        import httpx
        post = AsyncMock(side_effect=httpx.ConnectError("down"))
        with _patch_httpx_post(post):
            result = asyncio.run(_configured_client().forward_request(1))
        self.assertFalse(result["ok"])
        self.assertIn("jaringan", result["note"])


# ===========================================================================
# SiapWriteClient.record_decision
# ===========================================================================


class TestRecordDecision(unittest.TestCase):

    def test_decision_approved_success(self):
        body = {"data": {
            "request_id": 77294, "decision": "approved",
            "status": "APPROVED", "approval_step_id": 3665, "routed_back": False,
        }}
        post = AsyncMock(return_value=_mock_response(200, body))
        with _patch_httpx_post(post):
            result = asyncio.run(_configured_client().record_decision(
                77294, decision="approved", notes="dokumen lengkap"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["decision"], "approved")
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"], {
            "decision": "approved", "notes": "dokumen lengkap"})
        self.assertTrue(
            post.call_args[0][0].endswith(
                "/api/v1/license-request/77294/decision"))

    def test_decision_rejected_routes_back(self):
        body = {"data": {
            "request_id": 1, "decision": "rejected",
            "status": "REJECTED", "approval_step_id": 3662, "routed_back": True,
        }}
        post = AsyncMock(return_value=_mock_response(200, body))
        with _patch_httpx_post(post):
            result = asyncio.run(_configured_client().record_decision(
                1, decision="rejected", notes="NIK tidak cocok"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["routed_back"])

    def test_decision_invalid_value_rejected_locally(self):
        post = AsyncMock()
        with _patch_httpx_post(post):
            result = asyncio.run(_configured_client().record_decision(
                1, decision="maybe", notes="x"))
        self.assertFalse(result["ok"])
        self.assertIn("tidak valid", result["note"])
        post.assert_not_called()

    def test_decision_empty_notes_rejected_locally(self):
        post = AsyncMock()
        with _patch_httpx_post(post):
            result = asyncio.run(_configured_client().record_decision(
                1, decision="approved", notes="   "))
        self.assertFalse(result["ok"])
        self.assertIn("wajib diisi", result["note"])
        post.assert_not_called()

    def test_decision_notes_too_long_rejected_locally(self):
        post = AsyncMock()
        with _patch_httpx_post(post):
            result = asyncio.run(_configured_client().record_decision(
                1, decision="approved", notes="y" * 1001))
        self.assertFalse(result["ok"])
        self.assertIn("terlalu panjang", result["note"])
        post.assert_not_called()

    def test_decision_case_insensitive(self):
        post = AsyncMock(return_value=_mock_response(200, {"data": {}}))
        with _patch_httpx_post(post):
            asyncio.run(_configured_client().record_decision(
                1, decision="  APPROVED ", notes="ok"))
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["decision"], "approved")


# ===========================================================================
# Officer copilot write tools — OFFICER-IN-THE-LOOP guard
#
# The critical contract: forward_case / record_decision must NEVER write
# unless `confirmed is True`. These tests assert that the write client is
# not even constructed/called on the draft path.
# ===========================================================================


class TestOfficerInTheLoopGuard(unittest.IsolatedAsyncioTestCase):

    async def test_forward_case_unconfirmed_returns_draft_no_write(self):
        from services.agents import officer_copilot

        with patch.object(
            officer_copilot, "get_siap_write_client"
        ) as mock_get_client:
            result = await officer_copilot.forward_case(
                ticket="000077591", note="berkas lengkap"
            )
        self.assertFalse(result["executed"])
        self.assertTrue(result["needs_confirmation"])
        self.assertEqual(result["draft"]["action"], "forward_case")
        self.assertEqual(result["draft"]["ticket"], "000077591")
        # The write client must never even be fetched on the draft path.
        mock_get_client.assert_not_called()

    async def test_forward_case_confirmed_false_explicit_is_draft(self):
        from services.agents import officer_copilot

        with patch.object(
            officer_copilot, "get_siap_write_client"
        ) as mock_get_client:
            result = await officer_copilot.forward_case(
                ticket="000077591", confirmed=False
            )
        self.assertFalse(result["executed"])
        mock_get_client.assert_not_called()

    async def test_record_decision_unconfirmed_returns_draft_no_write(self):
        from services.agents import officer_copilot

        with patch.object(
            officer_copilot, "get_siap_write_client"
        ) as mock_get_client:
            result = await officer_copilot.record_decision(
                ticket="000077591", decision="rejected", notes="NIK salah"
            )
        self.assertFalse(result["executed"])
        self.assertTrue(result["needs_confirmation"])
        self.assertEqual(result["draft"]["decision"], "rejected")
        mock_get_client.assert_not_called()

    async def test_forward_case_confirmed_calls_write_client(self):
        """confirmed=True is the ONLY path that reaches the write client."""
        from services.agents import officer_copilot

        fake_write = MagicMock()
        fake_write.is_configured.return_value = True
        fake_write.forward_request = AsyncMock(return_value={
            "ok": True, "configured": True, "data": {"status": "APPROVED"},
        })
        # request_id resolution is mocked so no SIAP DB is needed.
        with patch.object(
            officer_copilot, "get_siap_write_client", return_value=fake_write
        ), patch.object(
            officer_copilot, "_resolve_request_id",
            new=AsyncMock(return_value=(77591, None)),
        ):
            result = await officer_copilot.forward_case(
                ticket="000077591", note="ok", confirmed=True
            )
        self.assertTrue(result["executed"])
        self.assertTrue(result["ok"])
        fake_write.forward_request.assert_awaited_once()
        self.assertEqual(fake_write.forward_request.call_args[0][0], 77591)

    async def test_record_decision_confirmed_calls_write_client(self):
        from services.agents import officer_copilot

        fake_write = MagicMock()
        fake_write.is_configured.return_value = True
        fake_write.record_decision = AsyncMock(return_value={
            "ok": True, "configured": True,
            "data": {"decision": "approved", "routed_back": False},
        })
        with patch.object(
            officer_copilot, "get_siap_write_client", return_value=fake_write
        ), patch.object(
            officer_copilot, "_resolve_request_id",
            new=AsyncMock(return_value=(77591, None)),
        ):
            result = await officer_copilot.record_decision(
                ticket="000077591", decision="approved",
                notes="dokumen lengkap", confirmed=True,
            )
        self.assertTrue(result["executed"])
        self.assertTrue(result["ok"])
        fake_write.record_decision.assert_awaited_once()

    async def test_confirmed_write_degrades_when_token_missing(self):
        """confirmed=True but write client not configured → no crash, no call."""
        from services.agents import officer_copilot

        fake_write = MagicMock()
        fake_write.is_configured.return_value = False
        fake_write.forward_request = AsyncMock()
        with patch.object(
            officer_copilot, "get_siap_write_client", return_value=fake_write
        ):
            result = await officer_copilot.forward_case(
                ticket="000077591", confirmed=True
            )
        self.assertFalse(result["ok"])
        self.assertIn("belum dikonfigurasi", result["note"])
        fake_write.forward_request.assert_not_awaited()

    async def test_confirmed_write_handles_unresolvable_ticket(self):
        from services.agents import officer_copilot

        fake_write = MagicMock()
        fake_write.is_configured.return_value = True
        fake_write.forward_request = AsyncMock()
        with patch.object(
            officer_copilot, "get_siap_write_client", return_value=fake_write
        ), patch.object(
            officer_copilot, "_resolve_request_id",
            new=AsyncMock(return_value=(None, "Tiket tidak ditemukan.")),
        ):
            result = await officer_copilot.forward_case(
                ticket="000000000", confirmed=True
            )
        self.assertTrue(result["executed"])
        self.assertFalse(result["ok"])
        self.assertIn("tidak ditemukan", result["note"])
        fake_write.forward_request.assert_not_awaited()


# ===========================================================================
# get_case_log_notes — chained context (Vision req #11)
# ===========================================================================


class TestChainedContext(unittest.IsolatedAsyncioTestCase):

    async def test_log_notes_surfaces_prior_stage_notes(self):
        from services.agents import officer_copilot

        fake_timeline = {
            "found": True,
            "no_tiket": "000077591",
            "request_id": 77591,
            "steps": [
                {"step": "Berkas diterima Front Office", "status": "SUBMITTED",
                 "owner_desk": "Front Office PTSP", "entered_on": "2026-05-10"},
                {"step": "Disetujui verifikator: dokumen lengkap",
                 "status": "APPROVED", "owner_desk": "Verifikasi",
                 "entered_on": "2026-05-12"},
                {"step": "Tahapan", "status": "", "owner_desk": None,
                 "entered_on": None},  # structural row — must be dropped
            ],
        }
        with patch.object(
            officer_copilot, "siap_get_status_timeline",
            new=AsyncMock(return_value=fake_timeline),
        ):
            result = await officer_copilot.get_case_log_notes("000077591")
        self.assertTrue(result["found"])
        self.assertEqual(result["request_id"], 77591)
        self.assertEqual(result["note_count"], 2)  # structural row dropped
        self.assertEqual(
            result["notes"][1]["note"], "Disetujui verifikator: dokumen lengkap")
        self.assertEqual(result["notes"][1]["owner_desk"], "Verifikasi")

    async def test_log_notes_miss_returns_structured_not_found(self):
        from services.agents import officer_copilot

        with patch.object(
            officer_copilot, "siap_get_status_timeline",
            new=AsyncMock(return_value={"found": False, "note": "tidak ada"}),
        ):
            result = await officer_copilot.get_case_log_notes("000000000")
        self.assertFalse(result["found"])
        self.assertIn("tidak ada", result["note"])


if __name__ == "__main__":
    # Quiet the module loggers so test output stays readable.
    import logging
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
