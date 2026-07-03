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


class TestDraftSkTool(unittest.TestCase):
    """draft_sk — the officer deputy renders SIAP's PKPP approval-letter (SK)
    template, queues the .docx for delivery, and narrates filled vs blank
    fields. NOT confirmation-gated; degrades gracefully on every miss."""

    def _stub_siap_templates(self, *, rendered, data=None, pdf_view="_default"):
        """Install a fake services.siap_templates whose build_sk_data returns
        `data`, render_sk_docx returns `rendered` ((bytes, name) or None), and
        render_pdf_view_from_docx returns `pdf_view`.

        `pdf_view` default "_default" mirrors the real PDF view path: it derives
        a (pdf_bytes, "<stem>.pdf") tuple from whatever `rendered` produced, so a
        successful .docx render also yields a PDF view copy (the P3 fix). Pass
        `pdf_view=None` to simulate a PDF-render miss (ships .docx alone).

        draft_sk resolves the module via `from services import siap_templates`,
        which reads the ATTRIBUTE on the already-imported `services` package —
        not sys.modules — so we patch both, and record the originals so
        _run_draft can restore them (avoids poisoning a sibling suite that ran
        the REAL siap_templates first, e.g. test_siap_templates in-process)."""
        import services as _services_pkg

        mod = types.ModuleType("services.siap_templates")
        _data = data if data is not None else {}

        async def _build_sk_data(*, applicant_name, alamat, license_name, documents):
            mod._build_args = {
                "applicant_name": applicant_name, "alamat": alamat,
                "license_name": license_name, "documents": documents,
            }
            return dict(_data)

        async def _render_sk_docx(license_id, d, *, ticket=None):
            mod._render_args = {"license_id": license_id, "ticket": ticket, "data": d}
            return rendered

        def _render_pdf_view(docx_bytes, *, ticket=None):
            mod._pdf_args = {"docx_bytes": docx_bytes, "ticket": ticket}
            if pdf_view == "_default":
                # Mirror the real path: a .docx render yields a PDF view copy.
                if rendered is None:
                    return None
                _, docx_name = rendered
                stem = docx_name.rsplit(".", 1)[0]
                return b"%PDF-fake-view", f"{stem}.pdf"
            return pdf_view

        mod.build_sk_data = _build_sk_data
        mod.render_sk_docx = _render_sk_docx
        mod.render_pdf_view_from_docx = _render_pdf_view
        self._old_st_mod = sys.modules.get("services.siap_templates")
        self._old_st_attr = getattr(_services_pkg, "siap_templates", None)
        sys.modules["services.siap_templates"] = mod
        setattr(_services_pkg, "siap_templates", mod)
        return mod

    def _restore_siap_templates(self):
        import services as _services_pkg
        if getattr(self, "_old_st_mod", None) is not None:
            sys.modules["services.siap_templates"] = self._old_st_mod
        else:
            sys.modules.pop("services.siap_templates", None)
        if getattr(self, "_old_st_attr", None) is not None:
            setattr(_services_pkg, "siap_templates", self._old_st_attr)
        elif hasattr(_services_pkg, "siap_templates"):
            delattr(_services_pkg, "siap_templates")

    def _run_draft(self, *, sk_ctx, doc_ctx=None, rendered=None, data=None,
                   pdf_view="_default"):
        self._stub_siap_templates(rendered=rendered, data=data, pdf_view=pdf_view)
        sk_token = oc._sk_context.set(sk_ctx)
        doc_token = oc._doc_context.set(doc_ctx)
        queue: list = []
        send_token = oc._docs_to_send_context.set(queue)
        try:
            out = _run(oc.draft_sk())
        finally:
            oc._sk_context.reset(sk_token)
            oc._doc_context.reset(doc_token)
            oc._docs_to_send_context.reset(send_token)
            self._restore_siap_templates()
        return out, queue

    def _sk_ctx(self):
        return {
            "license_id": 459, "ticket": "000123456",
            "license_name": "Pengadaan Kapal (Pembangunan)",
            "applicant_name": "CASMO", "alamat": "Jl. Laut No. 1",
        }

    def test_renders_and_queues_docx_with_filled_and_blank_report(self):
        # 3 identity fields filled, the rest blank → BOTH the editable .docx and
        # a read-only PDF view copy (P3) are queued inline, and the note reports
        # both field sets.
        data = {"nama_pemohon": "CASMO", "alamat": "Jl. Laut No. 1",
                "jenis_pengadaan": "Pembangunan"}
        out, queue = self._run_draft(
            sk_ctx=self._sk_ctx(),
            rendered=(b"PK\x03\x04docxbytes", "SK_PPKP_000123456.docx"),
            data=data,
        )
        # Queued as INLINE dicts (generated bytes), not file_id strings: the
        # .docx (edit copy) plus the .pdf (read/preview copy).
        self.assertEqual(len(queue), 2)
        self.assertTrue(all(isinstance(q, dict) for q in queue))
        by_name = {q["filename"]: q for q in queue}
        self.assertIn("SK_PPKP_000123456.docx", by_name)
        self.assertIn("SK_PPKP_000123456.pdf", by_name)
        self.assertEqual(by_name["SK_PPKP_000123456.docx"]["content"], b"PK\x03\x04docxbytes")
        self.assertEqual(
            by_name["SK_PPKP_000123456.docx"]["mime_type"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(by_name["SK_PPKP_000123456.pdf"]["mime_type"], "application/pdf")
        self.assertIn("docx", out.lower())
        self.assertIn("pdf", out.lower())
        # Filled fields surfaced (human labels).
        self.assertIn("Nama Pemohon", out)
        self.assertIn("Jenis Pengadaan", out)
        # Blank fields also surfaced so the officer completes them.
        self.assertIn("Nama Kapal", out)
        self.assertIn("No. PPKP", out)
        # It never claims to ISSUE the SK — the deputy drafts + recommends.
        self.assertIn("TTE", out)
        # P3: the note states the draft is NOT in SIAP (kills the hallucination).
        self.assertIn("tidak tersimpan di SIAP", out)

    def test_pdf_view_render_miss_ships_docx_alone(self):
        # If the PDF view render misses (fpdf2 absent / render error), the tool
        # still ships the editable .docx and never dead-ends.
        data = {"nama_pemohon": "CASMO"}
        out, queue = self._run_draft(
            sk_ctx=self._sk_ctx(),
            rendered=(b"PKxx", "SK_PPKP_000123456.docx"),
            data=data,
            pdf_view=None,
        )
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["filename"], "SK_PPKP_000123456.docx")
        self.assertIn("docx", out.lower())
        # Honest recovery wording present even without a PDF copy.
        self.assertIn("word", out.lower())
        self.assertIn("tidak tersimpan di SIAP", out)

    def test_passes_case_context_to_builder(self):
        # Capture the module reference so we can assert what the tool handed the
        # builder/renderer (the deputy must thread licence + identity + docs).
        m = self._stub_siap_templates(
            rendered=(b"PKxx", "n.docx"), data={"nama_pemohon": "CASMO"})
        sk_token = oc._sk_context.set(self._sk_ctx())
        doc_token = oc._doc_context.set(
            {"doc-1": {"content": b"x", "mime_type": "application/pdf"}})
        send_token = oc._docs_to_send_context.set([])
        try:
            _run(oc.draft_sk())
        finally:
            oc._sk_context.reset(sk_token)
            oc._doc_context.reset(doc_token)
            oc._docs_to_send_context.reset(send_token)
            self._restore_siap_templates()
        self.assertEqual(m._build_args["applicant_name"], "CASMO")
        self.assertEqual(m._build_args["alamat"], "Jl. Laut No. 1")
        self.assertEqual(m._build_args["license_name"], "Pengadaan Kapal (Pembangunan)")
        # The in-session doc bytes are handed to the builder for the Vision pass.
        self.assertIn("doc-1", m._build_args["documents"])
        self.assertEqual(m._render_args["license_id"], 459)
        self.assertEqual(m._render_args["ticket"], "000123456")

    def test_no_sk_context_degrades_not_dead_end(self):
        sk_token = oc._sk_context.set(None)
        queue: list = []
        send_token = oc._docs_to_send_context.set(queue)
        try:
            out = _run(oc.draft_sk())
        finally:
            oc._sk_context.reset(sk_token)
            oc._docs_to_send_context.reset(send_token)
        self.assertEqual(queue, [])
        self.assertIn("belum bisa", out.lower())

    def test_no_license_id_degrades(self):
        ctx = self._sk_ctx()
        ctx["license_id"] = None
        out, queue = self._run_draft(sk_ctx=ctx, rendered=None)
        self.assertEqual(queue, [])
        self.assertIn("license_id", out.lower())

    def test_render_none_reports_template_unavailable(self):
        # Template 404 / not synced → render_sk_docx None → honest message,
        # nothing queued, never a crash.
        out, queue = self._run_draft(
            sk_ctx=self._sk_ctx(), rendered=None, data={"nama_pemohon": "CASMO"})
        self.assertEqual(queue, [])
        self.assertIn("template", out.lower())
        self.assertIn("tte", out.lower())


class TestDraftSkWiring(unittest.TestCase):
    """draft_sk is a real, mode-allowed, NON-confirmation-gated officer tool."""

    def test_draft_sk_declared_and_dispatched_officer_only(self):
        self.assertIn("draft_sk", oc._TOOL_DISPATCH)
        self.assertIn("draft_sk", oc._OFFICER_TOOL_NAMES)
        names = {d["name"] for d in oc._FUNCTION_DECLARATIONS}
        self.assertIn("draft_sk", names)

    def test_draft_sk_is_not_confirmation_gated_or_case_closing(self):
        # It's a draft aid, not a SIAP write → not gated, does not close a case.
        self.assertNotIn("draft_sk", oc._REQUIRES_CONFIRMATION)
        # The signing assistant is chain-synthesis only; no drafting there.
        self.assertNotIn("draft_sk", oc._SIGNATURE_TOOL_NAMES)


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


class TestReviewerFraming(unittest.TestCase):
    """P1 — the officer is a REVIEWER of someone else's packet, never the
    applicant. Second-person applicant phrasings must be reframed, and the
    validation summary must carry a reviewer framing note for the model."""

    def test_reframe_second_person_application_phrasings(self):
        cases = [
            ("BIMA telah menganalisis berkas permohonan izin Anda.",
             "berkas berkas"),  # must NOT double 'berkas'
            ("permohonan izin Anda sudah lengkap.", "permohonan izin Anda"),
            ("dokumen Anda valid.", "dokumen Anda"),
            ("izin Anda diterima.", "izin Anda"),
        ]
        for text, forbidden in cases:
            out = oc._reframe_reviewer(text)
            self.assertNotIn(forbidden, out, f"leaked {forbidden!r} in {out!r}")
            self.assertNotIn("Anda", out, f"still second-person: {out!r}")

    def test_reframe_leaves_officer_you_intact_when_not_about_application(self):
        # Only application-referring second person is rewritten; a neutral
        # sentence without the targeted phrases is untouched.
        text = "Silakan periksa dokumen pemohon."
        self.assertEqual(oc._reframe_reviewer(text), text)

    def test_validation_summary_carries_reviewer_framing(self):
        ctx = {
            "score_percent": 90, "status": "ready",
            "summary": "BIMA telah menganalisis berkas permohonan izin Anda.",
            "issues": [],
        }
        token = oc._validation_context.set(ctx)
        try:
            out = oc.get_validation_summary()
        finally:
            oc._validation_context.reset(token)
        self.assertEqual(out["audience"], "petugas_peninjau")
        self.assertIn("framing_note", out)
        # The passed-through summary is reframed, not applicant-facing.
        self.assertNotIn("izin Anda", out["summary"])
        self.assertIn("berkas permohonan ini", out["summary"])


class TestSystemPromptContent(unittest.TestCase):
    """Lock in the prompt-only fixes (P1/P2a/P3a/P4/P5) so a later edit can't
    silently drop them. Assertions are on stable keywords, not full sentences."""

    def test_officer_prompt_forbids_addressing_officer_as_applicant(self):
        p = oc._SYSTEM_PROMPT_TEMPLATE
        self.assertIn("permohonan izin Anda", p)  # named as forbidden
        self.assertIn("berkas pemohon", p)
        self.assertIn("orang ketiga", p)

    def test_signature_prompt_forbids_addressing_head_as_applicant(self):
        p = oc._SIGNATURE_SYSTEM_PROMPT_TEMPLATE
        self.assertIn("berkas Anda", p)
        self.assertIn("berkas pemohon", p)

    def test_officer_prompt_mandates_send_document_on_view_request(self):
        p = oc._SYSTEM_PROMPT_TEMPLATE.lower()
        self.assertIn("send_document", oc._SYSTEM_PROMPT_TEMPLATE)
        # Covers the generic AND the named ("surat permohonan") view asks.
        self.assertIn("surat permohonan", p)
        self.assertIn("liat", p)  # the officer's colloquial spelling

    def test_officer_prompt_draft_means_sk_and_sk_not_in_siap(self):
        p = oc._SYSTEM_PROMPT_TEMPLATE
        low = p.lower()
        # P4: bare "draft" in review context → the SK draft.
        self.assertIn("buatkan draftnya", low)
        self.assertIn("draft_sk", p)
        # P3a: the draft SK is NOT in SIAP (kills the hallucination).
        self.assertIn("TIDAK tersimpan di SIAP", p)
        self.assertIn("Word", p)  # honest recovery names an app to open it in

    def test_officer_prompt_accepts_finding_override(self):
        p = oc._SYSTEM_PROMPT_TEMPLATE.lower()
        # P5: explicit officer clearance drops the finding on the first pass.
        self.assertIn("override", p)
        self.assertIn("sudah saya periksa dan sesuai", p)


class _FakeResp:
    """Minimal httpx.Response stand-in for the copilot chat loop."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Minimal httpx.AsyncClient stand-in: every POST returns the same canned
    Gemini payload. Async context manager, like the real client."""

    def __init__(self, payload, *a, **k):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        return _FakeResp(self._payload)


def _fake_httpx_module(payload):
    """Build a stand-in `httpx` module the copilot loop can import attributes
    from: AsyncClient (canned payload) + the three exception classes it catches."""
    mod = types.ModuleType("httpx")

    class _TimeoutException(Exception):
        pass

    class _HTTPStatusError(Exception):
        pass

    class _RequestError(Exception):
        pass

    mod.TimeoutException = _TimeoutException
    mod.HTTPStatusError = _HTTPStatusError
    mod.RequestError = _RequestError
    mod.AsyncClient = lambda *a, **k: _FakeAsyncClient(payload, *a, **k)
    return mod


class TestEmptyReplyFallback(unittest.TestCase):
    """P2(b) — a Gemini turn with NO tool call AND NO text must NOT dead-end
    with the bare 'tidak dapat menyusun jawaban' apology. It must surface the
    capability menu so the officer always has a next step (rehearsal: "saya mau
    liat surat permohonannya" produced a blank reply)."""

    def _chat_with_payload(self, payload, message="apa saja yang bisa kamu bantu",
                           mode="officer"):
        copilot = oc.OfficerCopilot(api_key="test-key")
        fake_httpx = _fake_httpx_module(payload)
        with unittest_patch(oc, "httpx", fake_httpx), \
                unittest_patch(oc, "_GEMINI_API_KEY", "test-key"):
            return _run(copilot.chat(
                message=message, ticket="000077764", history=[], mode=mode))

    def test_empty_candidate_returns_capability_menu(self):
        # Gemini returns a candidate with an empty parts list → mis-parse.
        payload = {"candidates": [{"content": {"parts": []}}]}
        result = self._chat_with_payload(payload)
        reply = result["reply"]
        # No bare dead-end apology.
        self.assertNotIn("tidak dapat menyusun jawaban", reply.lower())
        # It IS the capability menu (matches the module constant).
        self.assertEqual(reply, oc._EMPTY_REPLY_FALLBACK)
        # Menu lists the concrete capabilities the officer can pick from.
        self.assertIn("meringkas berkas", reply.lower())
        self.assertIn("mengirim dokumen", reply.lower())
        self.assertIn("membandingkan identitas", reply.lower())
        self.assertIn("draf sk", reply.lower())
        # Names a doc example so "kirim surat permohonan" is discoverable.
        self.assertIn("Surat Permohonan", reply)
        # History still advances with the fallback as the model turn.
        self.assertEqual(result["history"][-1]["text"], oc._EMPTY_REPLY_FALLBACK)

    def test_whitespace_only_text_also_falls_back(self):
        # A part carrying only whitespace is treated as empty, not a real reply.
        payload = {"candidates": [{"content": {"parts": [{"text": "   \n  "}]}}]}
        result = self._chat_with_payload(payload)
        self.assertEqual(result["reply"], oc._EMPTY_REPLY_FALLBACK)

    def test_real_text_reply_is_not_overridden(self):
        # A genuine text reply must pass through untouched (guard against the
        # fallback firing when the model actually answered).
        payload = {"candidates": [{"content": {"parts": [
            {"text": "Berkas permohonan ini sudah lengkap."}]}}]}
        result = self._chat_with_payload(payload)
        self.assertEqual(result["reply"], "Berkas permohonan ini sudah lengkap.")
        self.assertNotEqual(result["reply"], oc._EMPTY_REPLY_FALLBACK)

    def test_signature_mode_uses_signature_fallback_not_officer_menu(self):
        # FIX 2: at the Kepala-Dinas signing desk the empty-turn menu must offer
        # the signing assistant's REAL capabilities, not officer-mode tools
        # (kirim dokumen / bandingkan identitas / draf SK) that signature mode
        # can't do.
        payload = {"candidates": [{"content": {"parts": []}}]}
        result = self._chat_with_payload(payload, mode="signature")
        reply = result["reply"]
        # It is the signature variant, distinct from the officer menu.
        self.assertEqual(reply, oc._EMPTY_REPLY_FALLBACK_SIGNATURE)
        self.assertNotEqual(reply, oc._EMPTY_REPLY_FALLBACK)
        # Offers the signing-assistant's real capabilities.
        low = reply.lower()
        self.assertIn("rantai persetujuan", low)
        self.assertIn("tanda tangan siap", low)
        # And does NOT promise officer-only tools it cannot run here.
        self.assertNotIn("mengirim dokumen", low)
        self.assertNotIn("membandingkan identitas", low)
        self.assertNotIn("draf sk", low)


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
