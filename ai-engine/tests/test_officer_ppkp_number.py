"""
Tests for the OFFICER No. PPKP capability (task: write the officer-assigned PPKP
licence number into the SIAP Penomoran form + reflect it in the drafted Rekomtek).

Covers:
  1. siap_db.get_form_id_for_field — DISCOVERS the licence form that OWNS a field
     (walks license_approval_step -> license_approval_step_form -> forms JOIN
     ptsp.vw_form_fields). Returns the owning form_id; None on miss / unconfigured
     / bad ids / read error. Never raises. (For (459,"no_ppkp") → 768 by discovery.)

  2. siap_db.get_all_form_values — MERGES the form_value.properties of ALL forms
     bound to a licence for a request into one dict (so a draft reflects fields on
     DIFFERENT forms: applicant 560 + Penomoran 768). {} on miss; a non-blank value
     is never clobbered by a blank one; never raises; values never logged.

  3. officer_copilot.set_ppkp_number — writes {"no_ppkp": value} to the DISCOVERED
     Penomoran form via a mocked upsert. Honest fallback when: nomor empty, ticket
     unresolved, form_id None, client not configured, upsert !ok, upsert raises.
     Best-effort (never raises). The PPKP number is NEVER logged.

  4. officer_copilot.draft_sk overrides now include no_ppkp from the MERGED forms —
     get_all_form_values is threaded into render_output_docx's `overrides`.

  5. Tool wiring — set_ppkp_number is a declared, dispatched, officer-only tool,
     takes `nomor_ppkp`, and is NOT confirmation-gated / not a signature tool.

No network. The copilot's heavy deps are stubbed; the SIAP DB reads / the form
client / the renderer are monkeypatched per test — no Gemini, no DB, no network.

Run standalone:

    python -m tests.test_officer_ppkp_number   # from ai-engine/
    python tests/test_officer_ppkp_number.py   # also works
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


# Same leaf-dep stubs the sibling copilot suites use.
_ensure_stub("asyncpg")
_ensure_stub("dotenv", {"load_dotenv": lambda *a, **k: None})
_ensure_stub("httpx")
_ensure_stub("services.rag_service", {"query_regulations": lambda *a, **k: []})
_ensure_stub("services.agents.validator", {"_normalize_name": lambda s: s})

from services.agents import officer_copilot as oc  # noqa: E402
import services.siap_templates as st_mod  # noqa: E402
import services.siap_tools as stools_mod  # noqa: E402
import services.siap_db as sdb_mod  # noqa: E402
import services.siap_form_client as sfc_mod  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# DB-pool mocking scaffold (mirrors test_rehearsal_form_features).
# ===========================================================================


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


class _PoolPatch:
    def __init__(self, conn):
        pool = _FakePool(conn)
        self._patches = [
            patch.object(sdb_mod, "is_siap_db_configured", lambda: True),
            patch.object(sdb_mod, "get_siap_pool", AsyncMock(return_value=pool)),
        ]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


# ===========================================================================
# 1) get_form_id_for_field
# ===========================================================================


class _FieldFormConn:
    """fetchrow returns the owning form_id row; records the SQL + args."""

    def __init__(self, row):
        self._row = row
        self.seen_args = None
        self.seen_sql = None

    async def fetchrow(self, sql, *args):
        self.seen_sql = sql
        self.seen_args = args
        return self._row


class TestGetFormIdForField(unittest.TestCase):
    def test_returns_owning_form_id(self):
        conn = _FieldFormConn({"form_id": 768})
        with _PoolPatch(conn):
            out = _run(sdb_mod.get_form_id_for_field(459, "no_ppkp"))
        self.assertEqual(out, 768)
        # Walks the licence-forms join AND joins the field-name view.
        self.assertIn("ptsp.license_approval_step_form", conn.seen_sql)
        self.assertIn("ptsp.vw_form_fields", conn.seen_sql)
        # Parameterised on (license_id, field_name) — no interpolation.
        self.assertEqual(conn.seen_args, (459, "no_ppkp"))

    def test_missing_row_returns_none(self):
        conn = _FieldFormConn(None)
        with _PoolPatch(conn):
            out = _run(sdb_mod.get_form_id_for_field(459, "no_ppkp"))
        self.assertIsNone(out)

    def test_null_form_id_returns_none(self):
        conn = _FieldFormConn({"form_id": None})
        with _PoolPatch(conn):
            out = _run(sdb_mod.get_form_id_for_field(459, "no_ppkp"))
        self.assertIsNone(out)

    def test_unconfigured_returns_none(self):
        with patch.object(sdb_mod, "is_siap_db_configured", lambda: False):
            out = _run(sdb_mod.get_form_id_for_field(459, "no_ppkp"))
        self.assertIsNone(out)

    def test_bad_license_id_returns_none(self):
        with patch.object(sdb_mod, "is_siap_db_configured", lambda: True):
            out = _run(sdb_mod.get_form_id_for_field("nope", "no_ppkp"))
        self.assertIsNone(out)

    def test_blank_field_name_returns_none(self):
        with patch.object(sdb_mod, "is_siap_db_configured", lambda: True):
            out = _run(sdb_mod.get_form_id_for_field(459, "   "))
        self.assertIsNone(out)

    def test_read_error_returns_none(self):
        class _BoomConn:
            async def fetchrow(self, sql, *args):
                raise RuntimeError("db down")
        with _PoolPatch(_BoomConn()):
            out = _run(sdb_mod.get_form_id_for_field(459, "no_ppkp"))
        self.assertIsNone(out)


# ===========================================================================
# 2) get_all_form_values
# ===========================================================================


class _FormIdsConn:
    """fetch returns the licence's bound form_id rows."""

    def __init__(self, form_ids):
        self._rows = [{"form_id": fid} for fid in form_ids]
        self.seen_args = None

    async def fetch(self, sql, *args):
        assert "ptsp.license_approval_step_form" in sql
        self.seen_args = args
        return list(self._rows)


class TestGetAllFormValues(unittest.TestCase):
    def _run_merge(self, *, form_ids, per_form):
        """per_form: {form_id: props(dict|None|{})} for the patched
        get_form_value_properties."""
        conn = _FormIdsConn(form_ids)

        async def _get_fv(fid, rid):
            return per_form.get(int(fid))

        with _PoolPatch(conn), patch.object(
            sdb_mod, "get_form_value_properties", _get_fv
        ):
            return _run(sdb_mod.get_all_form_values(459, 777))

    def test_merges_disjoint_key_forms(self):
        # Applicant 560 (vessel/identity) + Penomoran 768 (no_ppkp) merge into one.
        out = self._run_merge(
            form_ids=[560, 768],
            per_form={
                560: {"nama_pemohon": "CASMO", "nama_kapal": "KM BERKAH"},
                768: {"no_ppkp": "2026/1234/01"},
            },
        )
        self.assertEqual(out, {
            "nama_pemohon": "CASMO", "nama_kapal": "KM BERKAH",
            "no_ppkp": "2026/1234/01",
        })

    def test_non_blank_not_clobbered_by_blank(self):
        # A blank value on a later form must NOT overwrite a non-blank earlier one.
        out = self._run_merge(
            form_ids=[560, 999],
            per_form={
                560: {"nama_kapal": "KM BERKAH"},
                999: {"nama_kapal": "   "},   # blank — must not clobber
            },
        )
        self.assertEqual(out.get("nama_kapal"), "KM BERKAH")

    def test_skips_none_and_empty_form_values(self):
        # A transient read (None) or an empty form ({}) contributes nothing but
        # must not break the merge of the others.
        out = self._run_merge(
            form_ids=[560, 768, 111],
            per_form={
                560: {"nama_pemohon": "CASMO"},
                768: None,        # transient read error
                111: {},          # empty form
            },
        )
        self.assertEqual(out, {"nama_pemohon": "CASMO"})

    def test_no_bound_forms_returns_empty(self):
        out = self._run_merge(form_ids=[], per_form={})
        self.assertEqual(out, {})

    def test_unconfigured_returns_empty(self):
        with patch.object(sdb_mod, "is_siap_db_configured", lambda: False):
            out = _run(sdb_mod.get_all_form_values(459, 777))
        self.assertEqual(out, {})

    def test_bad_ids_return_empty(self):
        with patch.object(sdb_mod, "is_siap_db_configured", lambda: True):
            out = _run(sdb_mod.get_all_form_values("nope", 777))
        self.assertEqual(out, {})

    def test_read_error_returns_empty(self):
        class _BoomConn:
            async def fetch(self, sql, *args):
                raise RuntimeError("db down")
        with _PoolPatch(_BoomConn()):
            out = _run(sdb_mod.get_all_form_values(459, 777))
        self.assertEqual(out, {})


# ===========================================================================
# 3) set_ppkp_number
# ===========================================================================


class _FakeFormClient:
    def __init__(self, *, configured=True, result=None, raise_exc=False):
        self._configured = configured
        self._result = result
        self._raise = raise_exc
        self.calls: list[dict] = []

    def is_configured(self) -> bool:
        return self._configured

    async def upsert_form(self, *, request_id, form_id, fields, files):
        self.calls.append({
            "request_id": request_id, "form_id": form_id,
            "fields": dict(fields), "files": dict(files),
        })
        if self._raise:
            raise RuntimeError("boom")
        if self._result is not None:
            return dict(self._result)
        return {
            "ok": True, "configured": True, "http_status": 201,
            "form_value_id": 8899,
            "fields_set": list(fields.keys()),
            "files_attached": list(files.keys()),
            "note": "",
        }


class _SetPpkpBase(unittest.TestCase):
    def _stub(self, *, request_id=777, form_id=768, form_client=None):
        self._patched = []
        self._calls: dict = {}

        async def _resolve_request_id(ticket):
            self._calls["resolve_request_id"] = ticket
            return request_id

        async def _get_form_id_for_field(license_id, field_name):
            self._calls["get_form_id_for_field"] = (license_id, field_name)
            return form_id

        fc = form_client if form_client is not None else _FakeFormClient()
        self._calls["form_client"] = fc

        def _get_form_client():
            return fc

        def _patch(mod, attr, value):
            self._patched.append((mod, attr, getattr(mod, attr, None), hasattr(mod, attr)))
            setattr(mod, attr, value)

        _patch(stools_mod, "siap_resolve_request_id", _resolve_request_id)
        _patch(sdb_mod, "get_form_id_for_field", _get_form_id_for_field)
        _patch(sfc_mod, "get_siap_form_client", _get_form_client)

    def _restore(self):
        for mod, attr, old, existed in reversed(getattr(self, "_patched", [])):
            if existed:
                setattr(mod, attr, old)
            elif hasattr(mod, attr):
                delattr(mod, attr)
        self._patched = []

    def _run_set(self, *, nomor, sk_ctx="_default", **stub_kwargs):
        self._stub(**stub_kwargs)
        if sk_ctx == "_default":
            sk_ctx = {"license_id": 459, "ticket": "000123456"}
        sk_token = oc._sk_context.set(sk_ctx)
        try:
            out = _run(oc.set_ppkp_number(nomor))
        finally:
            oc._sk_context.reset(sk_token)
            self._restore()
        return out


class TestSetPpkpNumber(_SetPpkpBase):
    def test_writes_no_ppkp_to_resolved_form(self):
        fc = _FakeFormClient()
        out = self._run_set(nomor="2026/1234/01", form_client=fc)
        # Exactly ONE upsert, carrying {"no_ppkp": <officer value>} + no files.
        self.assertEqual(len(fc.calls), 1)
        call = fc.calls[0]
        self.assertEqual(call["request_id"], 777)
        self.assertEqual(call["form_id"], 768)
        self.assertEqual(call["fields"], {"no_ppkp": "2026/1234/01"})
        self.assertEqual(call["files"], {})
        # The form was DISCOVERED for (license_id, "no_ppkp"), not hardcoded.
        self.assertEqual(self._calls["get_form_id_for_field"], (459, "no_ppkp"))
        # Confirmation echoes the exact number + steers to verify/Simpan.
        self.assertIn("2026/1234/01", out)
        self.assertIn("Penomoran", out)
        self.assertIn("Simpan", out)

    def test_trims_surrounding_whitespace(self):
        fc = _FakeFormClient()
        self._run_set(nomor="  2026/1234/01  ", form_client=fc)
        self.assertEqual(fc.calls[0]["fields"], {"no_ppkp": "2026/1234/01"})

    def test_empty_number_asks_and_does_not_write(self):
        fc = _FakeFormClient()
        out = self._run_set(nomor="   ", form_client=fc)
        # No write at all.
        self.assertEqual(fc.calls, [])
        # Honest ask, not a false refusal.
        self.assertIn("nomor", out.lower())
        self.assertNotIn("Formulir Isian tidak", out)

    def test_no_sk_context_is_honest(self):
        # sk_context None → honest degrade, no crash, no write.
        fc = _FakeFormClient()
        self._stub(form_client=fc)
        sk_token = oc._sk_context.set(None)
        try:
            out = _run(oc.set_ppkp_number("2026/1234/01"))
        finally:
            oc._sk_context.reset(sk_token)
            self._restore()
        self.assertEqual(fc.calls, [])
        self.assertIn("konteks", out.lower())

    def test_missing_license_id_is_honest(self):
        fc = _FakeFormClient()
        out = self._run_set(
            nomor="2026/1234/01",
            sk_ctx={"ticket": "000123456"},   # no license_id
            form_client=fc,
        )
        self.assertEqual(fc.calls, [])
        self.assertIn("license_id", out.lower())

    def test_ticket_unresolved_is_honest(self):
        fc = _FakeFormClient()
        out = self._run_set(nomor="2026/1234/01", request_id=None, form_client=fc)
        self.assertEqual(fc.calls, [])
        self.assertIn("request_id", out.lower())

    def test_form_id_none_is_honest(self):
        fc = _FakeFormClient()
        out = self._run_set(nomor="2026/1234/01", form_id=None, form_client=fc)
        self.assertEqual(fc.calls, [])
        # Names the Penomoran form / no_ppkp field it could not find.
        self.assertIn("Penomoran", out)

    def test_not_configured_client_is_honest(self):
        fc = _FakeFormClient(configured=False)
        out = self._run_set(nomor="2026/1234/01", form_client=fc)
        self.assertEqual(fc.calls, [])      # never attempts the write
        self.assertIn("belum aktif", out.lower())

    def test_upsert_not_ok_is_honest(self):
        fc = _FakeFormClient(result={
            "ok": False, "configured": True, "form_value_id": None,
            "fields_set": [], "files_attached": [], "note": "SIAP menolak.",
        })
        out = self._run_set(nomor="2026/1234/01", form_client=fc)
        self.assertEqual(len(fc.calls), 1)   # write WAS attempted
        self.assertIn("belum berhasil", out.lower())
        self.assertIn("SIAP menolak", out)

    def test_upsert_raises_is_best_effort_no_crash(self):
        fc = _FakeFormClient(raise_exc=True)
        out = self._run_set(nomor="2026/1234/01", form_client=fc)
        # Did not propagate the exception; honest message returned.
        self.assertIsInstance(out, str)
        self.assertIn("SIAP", out)

    def test_number_never_logged(self):
        fc = _FakeFormClient()
        secret = "2026/SECRET/9999"
        logger = logging.getLogger(oc.__name__)
        with self.assertLogs(logger, level="INFO") as cap:
            self._run_set(nomor=secret, form_client=fc)
        blob = "\n".join(cap.output)
        # The PPKP number must NOT appear anywhere in the logs (only its length).
        self.assertNotIn(secret, blob)
        self.assertNotIn("SECRET", blob)


# ===========================================================================
# 4) draft_sk overrides now include no_ppkp from the MERGED forms
# ===========================================================================


class _DraftOverridesBase(unittest.TestCase):
    """Stub draft_sk's dependencies; patch get_all_form_values to a merged dict
    and assert render_output_docx receives it as `overrides`."""

    def _stub(self, *, form_id=560, applicant_fv=None, merged=None):
        self._patched = []
        self._calls: dict = {}
        _fv = dict(applicant_fv) if applicant_fv is not None else {}
        _merged = dict(merged) if merged is not None else {}

        async def _step_template(license_id, *, is_final_step=False, step_stereotype=None):
            return {
                "found": True, "license_id": license_id,
                "stereotype": "LICENSE-RECOMMEND",
                "preferred_stereotype": "LICENSE-RECOMMEND", "fell_back": False,
                "internal_filename": "master/REK.docx", "doc_kind": "rekomendasi",
            }

        async def _resolve_request_id(ticket):
            return 777

        async def _get_applicant_form_id(license_id):
            return form_id

        async def _get_form_value(fid, rid):
            return dict(_fv)

        async def _get_all_form_values(license_id, request_id):
            self._calls["get_all_form_values"] = (license_id, request_id)
            return dict(_merged)

        async def _get_profile(profile_id):
            return {"full_name": "CASMO"}

        async def _get_case_meta(request_id):
            return {"found": True, "ticket": "000123456", "request_id": request_id,
                    "profile_id": 42}

        async def _get_license_name(license_id):
            return "Pengadaan Kapal (Pembangunan)"

        async def _render_output_docx(*, internal_filename, profile, case_meta,
                                       license_name, documents, ticket, out_prefix,
                                       overrides=None):
            self._calls["render_output_docx"] = {"overrides": overrides}
            return (b"PK\x03\x04docx", f"{out_prefix}_{ticket}.docx",
                    {"nama_pemohon": "CASMO"}, {"nama_pemohon": st_mod.SOURCE_FORM})

        def _render_pdf(docx_bytes_in, *, ticket=None):
            return (b"%PDF", f"view_{ticket}.pdf")

        def _do(mod, attr, value):
            self._patched.append((mod, attr, getattr(mod, attr, None), hasattr(mod, attr)))
            setattr(mod, attr, value)

        _do(stools_mod, "siap_get_step_output_template", _step_template)
        _do(stools_mod, "siap_resolve_request_id", _resolve_request_id)
        _do(sdb_mod, "get_applicant_form_id", _get_applicant_form_id)
        _do(sdb_mod, "get_form_value_properties", _get_form_value)
        _do(sdb_mod, "get_all_form_values", _get_all_form_values)
        _do(sdb_mod, "get_person_profile_properties", _get_profile)
        _do(sdb_mod, "get_request_case_meta", _get_case_meta)
        _do(sdb_mod, "get_license_name", _get_license_name)
        _do(st_mod, "render_output_docx", _render_output_docx)
        _do(st_mod, "render_pdf_view_from_docx", _render_pdf)

    def _restore(self):
        for mod, attr, old, existed in reversed(getattr(self, "_patched", [])):
            if existed:
                setattr(mod, attr, old)
            elif hasattr(mod, attr):
                delattr(mod, attr)
        self._patched = []

    def _run_draft(self, **stub_kwargs):
        self._stub(**stub_kwargs)
        sk_token = oc._sk_context.set({
            "license_id": 459, "ticket": "000123456",
            "license_name": "Pengadaan Kapal (Pembangunan)",
            "profile_id": 42, "is_final_step": False,
            "step_stereotype": "SURVEY-LAPANGAN",
        })
        doc_token = oc._doc_context.set(
            {"d1": {"content": b"x", "mime_type": "application/pdf"}})
        queue: list = []
        send_token = oc._docs_to_send_context.set(queue)
        try:
            out = _run(oc.draft_sk())
        finally:
            oc._sk_context.reset(sk_token)
            oc._doc_context.reset(doc_token)
            oc._docs_to_send_context.reset(send_token)
            self._restore()
        return out, queue


class TestDraftSkMergedOverrides(_DraftOverridesBase):
    def test_overrides_include_no_ppkp_from_merged_forms(self):
        # The applicant form has the vessel fields (gate passes); the MERGED forms
        # (560 + 768) add no_ppkp. draft_sk must thread the MERGED dict — including
        # no_ppkp — into render's `overrides`.
        fv = {"nama_pemohon": "CASMO", "nama_kapal": "KM BERKAH", "gt": "12",
              "thn_bangun": "2020", "tipe": "Motor", "alat": "Pukat",
              "bahan": "Kayu", "galangan": "Rembang"}
        merged = dict(fv)
        merged["no_ppkp"] = "2026/1234/01"   # the Penomoran form's field
        out, queue = self._run_draft(form_id=560, applicant_fv=fv, merged=merged)
        # Draft produced; merge was consulted for (license_id, request_id).
        self.assertTrue(len(queue) >= 1)
        self.assertEqual(self._calls.get("get_all_form_values"), (459, 777))
        ov = self._calls["render_output_docx"]["overrides"]
        self.assertIsNotNone(ov)
        self.assertEqual(ov.get("no_ppkp"), "2026/1234/01")
        # And still carries the applicant fields.
        self.assertEqual(ov.get("nama_kapal"), "KM BERKAH")

    def test_falls_back_to_applicant_form_value_when_merge_empty(self):
        # A transient merge read ({} ) must degrade to the applicant form_value so
        # no field is lost (and the draft still renders).
        fv = {"nama_pemohon": "CASMO", "nama_kapal": "KM BERKAH", "gt": "12",
              "thn_bangun": "2020", "tipe": "Motor", "alat": "Pukat",
              "bahan": "Kayu", "galangan": "Rembang"}
        out, queue = self._run_draft(form_id=560, applicant_fv=fv, merged={})
        self.assertTrue(len(queue) >= 1)
        ov = self._calls["render_output_docx"]["overrides"]
        self.assertEqual(ov, fv)


# ===========================================================================
# 5) Tool wiring
# ===========================================================================


class TestSetPpkpToolWiring(unittest.TestCase):
    def test_declared_dispatched_officer_only(self):
        self.assertIn("set_ppkp_number", oc._TOOL_DISPATCH)
        self.assertIn("set_ppkp_number", oc._OFFICER_TOOL_NAMES)
        names = [d["name"] for d in oc._FUNCTION_DECLARATIONS]
        self.assertIn("set_ppkp_number", names)
        # NOT exposed to the read-only signature assistant.
        self.assertNotIn("set_ppkp_number", oc._SIGNATURE_TOOL_NAMES)

    def test_declares_nomor_ppkp_required_arg(self):
        decl = next(d for d in oc._FUNCTION_DECLARATIONS
                    if d["name"] == "set_ppkp_number")
        props = decl["parameters"]["properties"]
        self.assertIn("nomor_ppkp", props)
        self.assertEqual(decl["parameters"]["required"], ["nomor_ppkp"])

    def test_not_confirmation_gated(self):
        # It writes an officer-supplied value (like fill_siap_form), not a
        # decision/forward — so it is NOT confirmation-gated.
        self.assertNotIn("set_ppkp_number", oc._REQUIRES_CONFIRMATION)

    def test_prompt_guides_to_set_ppkp_number(self):
        p = oc._SYSTEM_PROMPT_TEMPLATE
        # The officer prompt must name the tool + the No. PPKP / Penomoran concept.
        self.assertIn("set_ppkp_number", p)
        self.assertIn("PPKP", p)
        self.assertIn("Penomoran", p)
        # Must tell the model NOT to refuse with the stale Formulir-Isian message.
        self.assertIn("Formulir Isian", p)
        # And note Tgl. Penetapan is SIAP-set (not fillable), left blank.
        self.assertIn("Penetapan", p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
