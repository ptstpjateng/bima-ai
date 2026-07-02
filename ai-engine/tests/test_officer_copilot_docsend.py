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


# Stub only the leaf deps the copilot module can't pull in a test process
# (chromadb via rag_service, the DB driver, the name-normaliser's Vision dep).
# We do NOT stub services.siap_tools / siap_client / siap_write_client: their
# real modules import cleanly once `asyncpg` is stubbed, and stubbing them thin
# would poison sys.modules for a sibling suite in the same process (e.g.
# test_officer_bridge, which imports the real suitability_judge → real
# siap_tools). Sharing the real SIAP modules keeps the two suites consistent
# regardless of run order; the copilot's networked tools are never invoked
# here (the tests exercise the pure ContextVar-reading functions directly).
_ensure_stub("asyncpg")
_ensure_stub("dotenv", {"load_dotenv": lambda *a, **k: None})
_ensure_stub("httpx")
_ensure_stub("services.rag_service", {"query_regulations": lambda *a, **k: []})
_ensure_stub("services.agents.validator", {"_normalize_name": lambda s: s})

from services.agents import officer_copilot as oc  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _doc_ctx():
    """A representative in-session doc context (as officer_bridge injects it).

    doc-3 is deliberately MISLABELLED by the citizen (claimed 'Lampiran') but
    read by BIMA as 'Surat_Permohonan' — so the detected_type match is what the
    officer's "surat permohonan" must hit.
    """
    return {
        "doc-1": {
            "filename": "ktp_budi.jpg",
            "mime_type": "image/jpeg",
            "content": b"ktp-bytes",
            "claimed_type": "KTP",
            "detected_type": "KTP",
        },
        "doc-2": {
            "filename": "surat_pesanan.pdf",
            "mime_type": "application/pdf",
            "content": b"sp-bytes",
            "claimed_type": "Surat_Pesanan",
            "detected_type": "Surat_Pesanan",
        },
        "doc-3": {
            "filename": "scan001.pdf",
            "mime_type": "application/pdf",
            "content": b"perm-bytes",
            "claimed_type": "Lampiran",
            "detected_type": "Surat_Permohonan",
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

    def test_resolve_by_detected_type_when_citizen_mislabelled(self):
        # doc-3 is claimed 'Lampiran' but BIMA READ it as 'Surat_Permohonan'.
        # The officer says "surat permohonan" — the detected_type match wins.
        out, queue = self._with_ctx(lambda: oc.send_document("surat permohonan"))
        self.assertEqual(queue, ["doc-3"])

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

    def test_resolved_doc_with_empty_bytes_reports_gone_not_send(self):
        # Edge 3: a doc resolves in the context but its bytes were dropped on a
        # rehydrate (content=b""). send_document must NOT queue a phantom send;
        # it tells the officer the file is no longer stored (not "tidak
        # ditemukan", which would falsely deny the doc ever existed).
        ctx = {
            "doc-9": {
                "filename": "surat_permohonan.pdf", "mime_type": "application/pdf",
                "content": b"", "claimed_type": "Surat_Permohonan",
                "detected_type": "Surat_Permohonan",
            }
        }
        doc_token = oc._doc_context.set(ctx)
        queue: list = []
        send_token = oc._docs_to_send_context.set(queue)
        try:
            out = oc.send_document("surat permohonan")
        finally:
            oc._doc_context.reset(doc_token)
            oc._docs_to_send_context.reset(send_token)
        self.assertEqual(queue, [])                       # nothing queued
        self.assertNotIn("tidak ditemukan", out.lower())  # not a false denial
        self.assertIn("tidak lagi tersimpan", out.lower())

    def test_ref_only_in_digest_reports_gone_not_dead_end(self):
        # The doc isn't in the (bytes-carrying) context at all, but the retained
        # read-digest remembers it → honest "file gone", never generic not-found.
        digest = [{
            "file_id": "doc-3", "filename": "surat_permohonan.pdf",
            "detected_type": "Surat_Permohonan", "claimed_type": "Lampiran",
            "has_meterai": True, "confidence": 0.9, "matches": False,
        }]
        doc_token = oc._doc_context.set({})  # empty context (bytes gone)
        val_token = oc._validation_context.set({"documents_digest": digest})
        queue: list = []
        send_token = oc._docs_to_send_context.set(queue)
        try:
            out = oc.send_document("surat permohonan")
        finally:
            oc._doc_context.reset(doc_token)
            oc._validation_context.reset(val_token)
            oc._docs_to_send_context.reset(send_token)
        self.assertEqual(queue, [])
        self.assertIn("tidak lagi tersimpan", out.lower())
        self.assertIn("surat_permohonan.pdf", out)


class TestCompareIdentityTool(unittest.TestCase):
    """Edge 2 — compare_identity reads two in-session docs' NIK/name via Vision
    and compares them. The headline verificator ask; NIK masked, never a
    dead-end."""

    def _stub_vision(self, by_bytes: dict, *, configured=True):
        """Install a fake services.gemini_vision whose extract_structured
        returns the identity dict keyed by the doc's bytes."""
        mod = types.ModuleType("services.gemini_vision")

        async def _extract_structured(*, image_bytes, mime_type, prompt, response_schema):
            return by_bytes.get(image_bytes)

        mod.extract_structured = _extract_structured
        mod.is_configured = lambda: configured
        sys.modules["services.gemini_vision"] = mod
        return mod

    def _run_compare(self, doc_a, doc_b, field, *, doc_ctx=None, validation=None):
        doc_token = oc._doc_context.set(doc_ctx)
        val_token = oc._validation_context.set(validation)
        try:
            return _run(oc.compare_identity(doc_a, doc_b, field))
        finally:
            oc._doc_context.reset(doc_token)
            oc._validation_context.reset(val_token)

    def test_nik_matches_across_ktp_and_surat_permohonan(self):
        # Both docs carry the SAME NIK → equal, and the NIK is masked in output.
        nik = "3374012345678901"
        self._stub_vision({
            b"ktp-bytes": {"nik": nik, "nama": "Budi Santoso"},
            b"perm-bytes": {"nik": nik, "nama": "Budi Santoso"},
        })
        out = self._run_compare("KTP", "surat permohonan", "NIK", doc_ctx=_doc_ctx())
        self.assertTrue(out["available"])
        self.assertTrue(out["equal"])
        self.assertEqual(out["field"], "nik")
        # Full NIK never surfaced; masked form present.
        self.assertNotIn(nik, repr(out))
        self.assertIn("33", out["value_a"])
        self.assertIn("COCOK", out["note"])

    def test_nik_mismatch_reported_with_similarity(self):
        self._stub_vision({
            b"ktp-bytes": {"nik": "3374012345678901", "nama": "Budi"},
            b"perm-bytes": {"nik": "3374019999999999", "nama": "Budi"},
        })
        out = self._run_compare("KTP", "surat permohonan", "nik", doc_ctx=_doc_ctx())
        self.assertTrue(out["available"])
        self.assertFalse(out["equal"])
        self.assertNotIn("3374012345678901", repr(out))
        self.assertIn("TIDAK", out["note"].upper())

    def test_name_compare_uses_validator_normalisation(self):
        # Honorific difference must still compare equal (validator-aligned).
        # The docsend suite stubs services.agents.validator._normalize_name as
        # identity, so assert on the tool WIRING (field routing + availability),
        # not the honorific-stripping itself (covered in the validator suite).
        self._stub_vision({
            b"ktp-bytes": {"nik": "", "nama": "BUDI SANTOSO"},
            b"perm-bytes": {"nik": "", "nama": "BUDI SANTOSO"},
        })
        out = self._run_compare("KTP", "surat permohonan", "nama pemohon",
                                doc_ctx=_doc_ctx())
        self.assertEqual(out["field"], "nama")
        self.assertTrue(out["available"])
        self.assertTrue(out["equal"])
        # Names are shown to the authorized officer (not masked).
        self.assertIn("BUDI SANTOSO", out["value_a"])

    def test_unknown_doc_ref_is_not_available_lists_docs(self):
        self._stub_vision({})
        out = self._run_compare("KTP", "NPWP", "nik", doc_ctx=_doc_ctx())
        self.assertFalse(out["available"])
        # Names the missing ref + lists what IS available (human labels).
        self.assertIn("NPWP", out["note"])
        self.assertIn("ktp_budi.jpg", out["note"])

    def test_no_doc_context_degrades_gracefully(self):
        # Admin-dashboard path — no in-session bytes. Not a crash, not a
        # dead-end: points the officer at the validation findings.
        out = self._run_compare("KTP", "surat permohonan", "nik",
                                doc_ctx=None, validation=None)
        self.assertFalse(out["available"])
        self.assertIn("validasi", out["note"].lower())

    def test_vision_unreadable_field_reports_not_dead_end(self):
        # Vision returns None for one doc (unreadable) → available False with a
        # clear message, never a raise.
        self._stub_vision({
            b"ktp-bytes": {"nik": "3374012345678901", "nama": "Budi"},
            # perm-bytes deliberately absent → extract returns None
        })
        out = self._run_compare("KTP", "surat permohonan", "nik", doc_ctx=_doc_ctx())
        self.assertFalse(out["available"])
        self.assertIn("tidak dapat membaca", out["note"].lower())


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


class TestGetDocSummary(unittest.TestCase):
    """get_doc_summary hardening — resolve by name/type/file_id, summarise from
    real bytes when present, and DEGRADE to the retained read-digest (never the
    'tidak memiliki ID file' dead-end) when the bytes are gone."""

    _DIGEST = [
        {
            "file_id": "doc-1",
            "filename": "ktp_budi.jpg",
            "detected_type": "KTP",
            "claimed_type": "KTP",
            "has_meterai": None,
            "confidence": 0.97,
            "matches": True,
        },
        {
            "file_id": "doc-3",
            "filename": "scan001.pdf",
            "detected_type": "Surat_Permohonan",
            "claimed_type": "Lampiran",
            "has_meterai": True,
            "confidence": 0.9,
            "matches": False,
        },
    ]

    def _stub_vision(self, *, summary="ISI DOKUMEN RINGKAS", configured=True):
        """Install a fake services.gemini_vision (imported lazily in the tool).
        Returns the module so the test can inspect calls."""
        mod = types.ModuleType("services.gemini_vision")
        calls: list = []

        async def _extract_structured(*, image_bytes, mime_type, prompt, response_schema):
            calls.append({"bytes": image_bytes, "mime_type": mime_type})
            return {"summary": summary} if summary is not None else None

        mod.extract_structured = _extract_structured
        mod.is_configured = lambda: configured
        mod._calls = calls
        sys.modules["services.gemini_vision"] = mod
        return mod

    def _run_summary(self, doc_ref, *, doc_ctx=None, validation=None):
        doc_token = oc._doc_context.set(doc_ctx)
        val_token = oc._validation_context.set(validation)
        try:
            return _run(oc.get_doc_summary(doc_ref))
        finally:
            oc._doc_context.reset(doc_token)
            oc._validation_context.reset(val_token)

    # --- summarise from real bytes, resolving the officer's plain name -------

    def test_summarises_bytes_resolved_by_type_name(self):
        vision = self._stub_vision(summary="KTP atas nama pemohon, lengkap.")
        out = self._run_summary("KTP", doc_ctx=_doc_ctx())
        self.assertIn("ktp_budi.jpg", out)
        self.assertIn("lengkap", out.lower())
        # The KTP bytes (not another doc) went to Vision.
        self.assertEqual(vision._calls[0]["bytes"], b"ktp-bytes")

    def test_summarises_bytes_resolved_by_detected_type(self):
        # Officer says "surat permohonan"; citizen mislabelled it 'Lampiran'.
        vision = self._stub_vision(summary="Surat permohonan izin, bertanda tangan.")
        out = self._run_summary("surat permohonan", doc_ctx=_doc_ctx())
        self.assertIn("scan001.pdf", out)
        self.assertEqual(vision._calls[0]["bytes"], b"perm-bytes")

    def test_summarises_bytes_resolved_by_literal_file_id(self):
        vision = self._stub_vision(summary="ok")
        out = self._run_summary("doc-2", doc_ctx=_doc_ctx())
        self.assertEqual(vision._calls[0]["bytes"], b"sp-bytes")

    # --- graceful degrade: bytes gone, digest retained ----------------------

    def test_degrades_to_digest_when_bytes_absent(self):
        # THE live symptom: officer has the digest but not the bytes/file id.
        self._stub_vision()  # would be used if bytes were present — they aren't
        validation = {"documents_digest": self._DIGEST}
        out = self._run_summary("KTP", doc_ctx=None, validation=validation)
        # Never the dead-end message.
        self.assertNotIn("tidak memiliki ID file", out)
        # It surfaces what BIMA read from the digest instead.
        self.assertIn("KTP", out)
        self.assertIn("97%", out)  # confidence from the digest
        self.assertIn("ktp_budi.jpg", out)

    def test_degrades_to_digest_when_doc_ctx_has_no_bytes(self):
        # Doc context resolves the ref but the doc's bytes are empty (rehydrate
        # dropped them) → still summarise from the digest, not a dead-end.
        ctx = {
            "doc-3": {
                "filename": "scan001.pdf", "mime_type": "application/pdf",
                "content": b"", "claimed_type": "Lampiran",
                "detected_type": "Surat_Permohonan",
            }
        }
        validation = {"documents_digest": self._DIGEST}
        out = self._run_summary("surat permohonan", doc_ctx=ctx, validation=validation)
        self.assertNotIn("tidak memiliki ID file", out)
        self.assertIn("Surat_Permohonan", out)
        self.assertIn("materai terdeteksi", out)

    def test_degrades_via_validation_context_with_no_doc_ctx(self):
        # No doc context at all (admin path) but a digest is bound → degrade.
        validation = {"documents_digest": self._DIGEST}
        out = self._run_summary("surat permohonan", doc_ctx=None, validation=validation)
        self.assertIn("Surat_Permohonan", out)
        self.assertNotIn("belum tersedia di jalur ini", out)

    def test_single_doc_digest_matches_loose_ref(self):
        # A one-doc submission: even a vague ref should yield the digest summary.
        one = [self._DIGEST[0]]
        out = self._run_summary("dokumennya", doc_ctx=None,
                                validation={"documents_digest": one})
        self.assertIn("ktp_budi.jpg", out)

    # --- honest not-found (still no dead-end wording) ------------------------

    def test_unknown_ref_lists_available_docs_not_dead_end(self):
        vision = self._stub_vision()
        out = self._run_summary("NPWP", doc_ctx=_doc_ctx())
        self.assertNotIn("tidak memiliki ID file", out)
        self.assertIn("tidak ditemukan", out.lower())
        # Lists human labels (filenames), not opaque ids.
        self.assertIn("ktp_budi.jpg", out)
        # No Vision call for an unresolved ref.
        self.assertEqual(vision._calls, [])

    def test_no_context_and_no_digest_returns_placeholder(self):
        out = self._run_summary("KTP", doc_ctx=None, validation=None)
        self.assertIn("belum tersedia di jalur ini", out)

    def test_file_id_kwarg_alias_still_resolves(self):
        # Backward-compat: a model emitting the old `file_id` param must work.
        vision = self._stub_vision(summary="ok")
        doc_token = oc._doc_context.set(_doc_ctx())
        try:
            out = _run(oc.get_doc_summary(file_id="doc-1"))
        finally:
            oc._doc_context.reset(doc_token)
        self.assertEqual(vision._calls[0]["bytes"], b"ktp-bytes")

    def test_empty_ref_does_not_crash(self):
        self._stub_vision()
        out = self._run_summary("", doc_ctx=_doc_ctx())
        self.assertIsInstance(out, str)
        self.assertNotIn("tidak memiliki ID file", out)


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
