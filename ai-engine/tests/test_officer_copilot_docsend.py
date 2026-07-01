"""
Tests for the officer-copilot document capabilities added in Task G + F:

  * send_document (Task G) — resolves a doc_ref (label / filename / file_id)
    against the in-session doc context, records the resolved file_id on the
    out-of-band send queue, and returns a confirmation string. Unknown ref →
    not-found string + records nothing.
  * documents_digest (Task F) — the per-doc read digest carried on the
    validation context is surfaced by get_validation_summary and get_case_full
    as `documents_read`.

Run standalone (no pytest — matches tests/test_officer_bridge.py):

    python -m tests.test_officer_copilot_docsend     # from ai-engine/
    python tests/test_officer_copilot_docsend.py     # also works

The copilot's heavy deps (chromadb via rag_service, SIAP clients) are stubbed
so the module imports without them. The tools are pure functions that read
ContextVars, which the tests set directly — no Gemini, no network.
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path

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


_ensure_stub("dotenv", {"load_dotenv": lambda *a, **k: None})
_ensure_stub("httpx")
_ensure_stub("services.rag_service", {"query_regulations": lambda *a, **k: []})
_ensure_stub("services.siap_client", {"get_siap_client": lambda: None})
_ensure_stub("services.siap_tools", {"siap_get_status_timeline": None})
_ensure_stub("services.siap_write_client", {"get_siap_write_client": lambda: None})
_ensure_stub("services.agents.validator", {"_normalize_name": lambda s: s})

from services.agents import officer_copilot as oc  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _doc_ctx():
    """A representative in-session doc context (as officer_bridge injects it)."""
    return {
        "doc-1": {
            "filename": "ktp_budi.jpg",
            "mime_type": "image/jpeg",
            "content": b"ktp-bytes",
            "claimed_type": "KTP",
        },
        "doc-2": {
            "filename": "surat_pesanan.pdf",
            "mime_type": "application/pdf",
            "content": b"sp-bytes",
            "claimed_type": "Surat_Pesanan",
        },
    }


class TestSendDocumentTool(unittest.TestCase):
    """send_document resolves + records; it must NOT send the file itself."""

    def _with_ctx(self, fn):
        """Run `fn()` with a fresh doc context + send queue bound, and return
        (result, queue_after)."""
        doc_token = oc._doc_context.set(_doc_ctx())
        queue: list = []
        send_token = oc._docs_to_send_context.set(queue)
        try:
            out = fn()
        finally:
            oc._doc_context.reset(doc_token)
            oc._docs_to_send_context.reset(send_token)
        return out, queue

    def test_resolve_by_type_label(self):
        out, queue = self._with_ctx(lambda: oc.send_document("KTP"))
        self.assertIn("kirimkan", out.lower())
        self.assertIn("ktp_budi.jpg", out)
        self.assertEqual(queue, ["doc-1"])

    def test_resolve_by_label_space_vs_underscore(self):
        # Officer types "surat pesanan"; the doc's claimed_type is
        # "Surat_Pesanan" — the space/underscore-agnostic match must hit.
        out, queue = self._with_ctx(lambda: oc.send_document("surat pesanan"))
        self.assertIn("surat_pesanan.pdf", out)
        self.assertEqual(queue, ["doc-2"])

    def test_resolve_by_filename_substring(self):
        out, queue = self._with_ctx(lambda: oc.send_document("budi"))
        self.assertEqual(queue, ["doc-1"])

    def test_resolve_by_literal_file_id(self):
        out, queue = self._with_ctx(lambda: oc.send_document("doc-2"))
        self.assertEqual(queue, ["doc-2"])

    def test_unknown_ref_records_nothing(self):
        out, queue = self._with_ctx(lambda: oc.send_document("NPWP"))
        self.assertIn("tidak ditemukan", out.lower())
        self.assertEqual(queue, [])

    def test_no_doc_context_bound(self):
        # No in-session bytes (admin dashboard path) → nothing to send, queue
        # stays empty and the tool says so.
        doc_token = oc._doc_context.set(None)
        queue: list = []
        send_token = oc._docs_to_send_context.set(queue)
        try:
            out = oc.send_document("KTP")
        finally:
            oc._doc_context.reset(doc_token)
            oc._docs_to_send_context.reset(send_token)
        self.assertIn("tidak", out.lower())
        self.assertEqual(queue, [])

    def test_no_duplicate_file_id_in_queue(self):
        doc_token = oc._doc_context.set(_doc_ctx())
        queue: list = []
        send_token = oc._docs_to_send_context.set(queue)
        try:
            oc.send_document("KTP")
            oc.send_document("ktp_budi.jpg")  # same doc, different ref
        finally:
            oc._doc_context.reset(doc_token)
            oc._docs_to_send_context.reset(send_token)
        self.assertEqual(queue, ["doc-1"])


class TestDocumentsDigestSurfacing(unittest.TestCase):
    """Task F — the digest on the validation context appears as
    `documents_read` in get_validation_summary and get_case_full."""

    _DIGEST = [
        {
            "filename": "ktp_budi.jpg",
            "detected_type": "KTP",
            "claimed_type": "KTP",
            "has_meterai": None,
            "confidence": 0.97,
            "matches": True,
        },
        {
            "filename": "surat_pesanan.pdf",
            "detected_type": "Surat_Pesanan",
            "claimed_type": "Surat_Pesanan",
            "has_meterai": True,
            "confidence": 0.88,
            "matches": True,
        },
    ]

    def _validation_ctx(self):
        return {
            "score_percent": 82,
            "status": "minor_issues",
            "summary": "ringkasan",
            "issues": [],
            "documents_digest": self._DIGEST,
        }

    def test_validation_summary_includes_documents_read(self):
        token = oc._validation_context.set(self._validation_ctx())
        try:
            out = oc.get_validation_summary()
        finally:
            oc._validation_context.reset(token)
        self.assertTrue(out["available"])
        read = out["documents_read"]
        self.assertEqual(len(read), 2)
        self.assertEqual(read[0]["detected_type"], "KTP")
        # meterai visibility survives per-doc.
        self.assertEqual(read[1]["has_meterai"], True)
        self.assertEqual(read[1]["detected_type"], "Surat_Pesanan")

    def test_validation_summary_no_digest_is_empty_list(self):
        ctx = self._validation_ctx()
        del ctx["documents_digest"]
        token = oc._validation_context.set(ctx)
        try:
            out = oc.get_validation_summary()
        finally:
            oc._validation_context.reset(token)
        self.assertEqual(out["documents_read"], [])

    def test_get_case_full_attaches_documents_read_on_miss(self):
        # Stub the SIAP client so get_case_full returns the "not found" shape,
        # and assert the digest still rides along.
        class _Client:
            async def get_status_by_ticket(self, ticket):
                return None

        val_token = oc._validation_context.set(self._validation_ctx())
        try:
            with unittest_patch(oc, "get_siap_client", lambda: _Client()):
                out = _run(oc.get_case_full("000123456"))
        finally:
            oc._validation_context.reset(val_token)
        self.assertFalse(out["found"])
        self.assertEqual(len(out["documents_read"]), 2)
        self.assertEqual(out["documents_read"][0]["detected_type"], "KTP")

    def test_get_case_full_attaches_documents_read_on_hit(self):
        class _Client:
            async def get_status_by_ticket(self, ticket):
                return {"ticket": ticket, "status": "VERIFIKASI"}

        val_token = oc._validation_context.set(self._validation_ctx())
        try:
            with unittest_patch(oc, "get_siap_client", lambda: _Client()):
                out = _run(oc.get_case_full("000123456"))
        finally:
            oc._validation_context.reset(val_token)
        self.assertTrue(out["found"])
        self.assertEqual(out["status"], "VERIFIKASI")
        self.assertEqual(len(out["documents_read"]), 2)


class _unittest_patch:
    """Tiny context-manager setattr patch (avoids importing unittest.mock just
    for a one-attribute swap; keeps the file's no-network promise obvious)."""

    def __init__(self, target, attr, value):
        self.target, self.attr, self.value = target, attr, value

    def __enter__(self):
        self.old = getattr(self.target, self.attr)
        setattr(self.target, self.attr, self.value)
        return self.value

    def __exit__(self, *exc):
        setattr(self.target, self.attr, self.old)
        return False


def unittest_patch(target, attr, value):
    return _unittest_patch(target, attr, value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
