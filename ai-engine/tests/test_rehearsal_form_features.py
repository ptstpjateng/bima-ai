"""
Tests for the two rehearsal-shaping features (no network; mock clients/DB/Vision):

  Task 1 — officer_copilot._perform_form_fill: the SHARED no-hallucination fill
    core. Fields come ONLY from real sources (via a mocked resolve_fill_data),
    slot documents map to the correct form slots, and a client error / not-
    configured client degrades best-effort (never raises).

  Task 2 — guided_submission citizen auto-fill: after a mocked successful submit
    the upload-then-fill task schedules, awaits the upload FIRST, calls
    _perform_form_fill with vision docs from sess.documents + slot docs from a
    mocked list_documents, sends a follow-up via a mocked whatsapp_sender.
    send_text, degrades silently when the form client / form_id is unavailable,
    and NEVER delays / breaks the submit return.

  Task 3 — siap_db.get_form_value_properties: reads ptsp.form_value.properties
    (dict or JSON-string), returns {} on miss / unconfigured / bad ids.

  Task 4 — officer_copilot.draft_sk GATE: refuses (no draft, lists blanks) when
    the form_value is empty / vessel-blank, PROCEEDS when it is filled, and
    FALLS BACK to the current draft behaviour when form_id is None.

Run standalone (no pytest needed):

    python -m tests.test_rehearsal_form_features   # from ai-engine/
    python tests/test_rehearsal_form_features.py   # also works
"""

from __future__ import annotations

import asyncio
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


# Same leaf-dep stubs the sibling copilot suites use (chromadb via rag_service,
# the DB driver, the name-normaliser's Vision dep).
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
from services import guided_submission as gs  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# Task 1 — _perform_form_fill (the SHARED core)
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
            "form_value_id": 71234,
            "fields_set": list(fields.keys()),
            "files_attached": list(files.keys()),
            "note": "",
        }


class _PerformFillBase(unittest.TestCase):
    def _patch(self, *, data=None, classes=None, form_client=None):
        self._patched = []
        self._calls: dict = {}
        _data = dict(data if data is not None else {})
        _classes = dict(classes if classes is not None else {})

        async def _resolve_fill_data(keys, *, profile=None, case_meta=None,
                                     license_name=None, documents=None,
                                     run_vision=True, overrides=None):
            self._calls["resolve_fill_data"] = {
                "keys": list(keys), "profile": profile, "case_meta": case_meta,
                "license_name": license_name, "documents": documents,
                "run_vision": run_vision, "overrides": overrides,
            }
            cls = {k: _classes.get(k, st_mod.SOURCE_BLANK) for k in keys}
            dat = {k: v for k, v in _data.items() if k in keys}
            return dat, cls

        fc = form_client if form_client is not None else _FakeFormClient()
        self._calls["form_client"] = fc

        def _get_form_client():
            return fc

        def _do(mod, attr, value):
            self._patched.append((mod, attr, getattr(mod, attr, None), hasattr(mod, attr)))
            setattr(mod, attr, value)

        _do(st_mod, "resolve_fill_data", _resolve_fill_data)
        _do(sfc_mod, "get_siap_form_client", _get_form_client)

    def _restore(self):
        for mod, attr, old, existed in reversed(getattr(self, "_patched", [])):
            if existed:
                setattr(mod, attr, old)
            elif hasattr(mod, attr):
                delattr(mod, attr)
        self._patched = []

    def _perform(self, *, slot_documents=None, **patch_kwargs):
        self._patch(**patch_kwargs)
        try:
            out = _run(oc._perform_form_fill(
                request_id=777, license_id=459, form_id=560,
                profile={"full_name": "CASMO"}, case_meta={"ticket": "000123456"},
                license_name="Pembangunan Kapal Penangkap Ikan",
                vision_documents={"d1": {"content": b"x", "mime_type": "application/pdf"}},
                slot_documents=slot_documents or [],
            ))
        finally:
            self._restore()
        return out


class TestPerformFormFill(_PerformFillBase):
    def test_fields_only_from_real_sources_and_correct_slot_mapping(self):
        data = {"nama_pemohon": "CASMO", "no_siup": "SIUP-1"}
        classes = {"nama_pemohon": st_mod.SOURCE_PROFILE,
                   "no_siup": st_mod.SOURCE_VISION}
        slot_docs = [
            {"file_id": 991, "filename": "ktp.jpg", "label": "KTP"},
            {"file_id": 993, "filename": "nib.pdf", "label": "SIUP"},
            {"file_id": 999, "filename": "random.pdf", "label": "Catatan"},
        ]
        out = self._perform(data=data, classes=classes, slot_documents=slot_docs)

        # Never raises; ok.
        self.assertTrue(out["ok"])
        # filled reports ONLY the real reads, with provenance class.
        self.assertEqual(set(out["filled"].keys()), {"nama_pemohon", "no_siup"})
        self.assertEqual(out["filled"]["nama_pemohon"], st_mod.SOURCE_PROFILE)
        self.assertEqual(out["filled"]["no_siup"], st_mod.SOURCE_VISION)
        # blank_keys = every text key that didn't get a value.
        self.assertIn("nama_kapal", out["blank_keys"])
        self.assertNotIn("nama_pemohon", out["blank_keys"])
        # Slots: KTP→up_ktp(991), SIUP→up_siup_oss(993); the random doc is skipped.
        self.assertEqual(out["attached"]["up_ktp"], 991)
        self.assertEqual(out["attached"]["up_siup_oss"], 993)
        self.assertEqual(len(out["attached"]), 2)
        # The endpoint got ONLY the real reads (nothing fabricated).
        sent = self._calls["form_client"].calls[0]
        self.assertEqual(sent["fields"], data)
        self.assertEqual(sent["request_id"], 777)
        self.assertEqual(sent["form_id"], 560)
        # resolve_fill_data was fed the applicant form field keys + run_vision.
        rfd = self._calls["resolve_fill_data"]
        self.assertIn("nama_pemohon", rfd["keys"])
        self.assertIn("nama_kapal", rfd["keys"])
        self.assertTrue(rfd["run_vision"])

    def test_unattachable_slot_doc_surfaced_not_dropped(self):
        # A slot doc with no valid file_id matches a slot alias but can't be
        # attached — it is reported in `unattachable`, never fabricated.
        slot_docs = [
            {"file_id": None, "filename": "Fotokopi KTP.jpg", "label": "KTP"},
            {"file_id": 993, "filename": "nib.pdf", "label": "SIUP"},
        ]
        out = self._perform(data={}, classes={}, slot_documents=slot_docs)
        self.assertEqual(out["attached"], {"up_siup_oss": 993})
        self.assertEqual(len(out["unattachable"]), 1)
        self.assertIn("KTP", out["unattachable"][0])

    def test_client_error_degrades_best_effort_no_raise(self):
        fc = _FakeFormClient(raise_exc=True)
        out = self._perform(data={"nama_pemohon": "CASMO"},
                            classes={"nama_pemohon": st_mod.SOURCE_PROFILE},
                            form_client=fc)
        # No crash; a structured non-ok result.
        self.assertFalse(out["ok"])
        self.assertTrue(out["configured"])
        self.assertIn("SIAP", out["note"])

    def test_not_configured_client_degrades(self):
        fc = _FakeFormClient(configured=False)
        out = self._perform(data={"nama_pemohon": "CASMO"},
                            classes={"nama_pemohon": st_mod.SOURCE_PROFILE},
                            form_client=fc)
        self.assertFalse(out["ok"])
        self.assertFalse(out["configured"])
        # No upsert attempted.
        self.assertEqual(fc.calls, [])

    def test_upsert_failure_returns_structured_note(self):
        fc = _FakeFormClient(result={
            "ok": False, "configured": True, "http_status": 422,
            "form_value_id": None, "fields_set": [], "files_attached": [],
            "note": "Data Formulir Isian tidak valid menurut SIAP.",
        })
        out = self._perform(data={"nama_pemohon": "CASMO"},
                            classes={"nama_pemohon": st_mod.SOURCE_PROFILE},
                            form_client=fc)
        self.assertFalse(out["ok"])
        self.assertTrue(out["configured"])
        self.assertIn("tidak valid", out["note"])


# ===========================================================================
# Task 3 — siap_db.get_form_value_properties
# ===========================================================================


class _FormValueConn:
    def __init__(self, row):
        self._row = row
        self.seen_args = None

    async def fetchrow(self, sql, *args):
        assert "FROM ptsp.form_value" in sql
        self.seen_args = args
        return self._row


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


class TestGetFormValueProperties(unittest.TestCase):
    def test_reads_dict_properties(self):
        conn = _FormValueConn({"properties": {"nama_kapal": "KM BERKAH"}})
        with _PoolPatch(conn):
            out = _run(sdb_mod.get_form_value_properties(560, 777))
        self.assertEqual(out, {"nama_kapal": "KM BERKAH"})
        # Query keyed on (form_id, request_id).
        self.assertEqual(conn.seen_args, (560, 777))

    def test_reads_json_string_properties(self):
        conn = _FormValueConn({"properties": '{"gt": "12"}'})
        with _PoolPatch(conn):
            out = _run(sdb_mod.get_form_value_properties(560, 777))
        self.assertEqual(out, {"gt": "12"})

    def test_missing_row_returns_empty(self):
        conn = _FormValueConn(None)
        with _PoolPatch(conn):
            out = _run(sdb_mod.get_form_value_properties(560, 777))
        self.assertEqual(out, {})

    def test_null_properties_returns_empty(self):
        conn = _FormValueConn({"properties": None})
        with _PoolPatch(conn):
            out = _run(sdb_mod.get_form_value_properties(560, 777))
        self.assertEqual(out, {})

    def test_invalid_json_returns_empty(self):
        conn = _FormValueConn({"properties": "{not json"})
        with _PoolPatch(conn):
            out = _run(sdb_mod.get_form_value_properties(560, 777))
        self.assertEqual(out, {})

    def test_unconfigured_returns_empty(self):
        with patch.object(sdb_mod, "is_siap_db_configured", lambda: False):
            out = _run(sdb_mod.get_form_value_properties(560, 777))
        self.assertEqual(out, {})

    def test_bad_ids_return_empty(self):
        with patch.object(sdb_mod, "is_siap_db_configured", lambda: True):
            out = _run(sdb_mod.get_form_value_properties("nope", 777))
        self.assertEqual(out, {})

    def test_read_error_returns_none(self):
        # A transient DB read error must be distinguishable from a genuine empty
        # form: the caller (draft_sk gate) degrades to the ungated draft on None,
        # but treats {} as "form is empty". So a raised read returns None, NOT {}.
        class _BoomConn:
            async def fetchrow(self, sql, *args):
                raise RuntimeError("db down")
        with _PoolPatch(_BoomConn()):
            out = _run(sdb_mod.get_form_value_properties(560, 777))
        self.assertIsNone(out)


# ===========================================================================
# Task 4 — draft_sk GATE + form-sourcing
# ===========================================================================


class _DraftGateBase(unittest.TestCase):
    """Stub the modules draft_sk resolves via `from services import …`, patching
    get_applicant_form_id + get_form_value_properties so the gate is exercised."""

    def _stub(self, *, form_id=560, form_value=None, rendered_data=None,
              rendered_classes=None, merged_form_values=None):
        self._patched = []
        self._calls: dict = {}
        _fv = dict(form_value) if form_value is not None else {}
        # The licence-wide 560+768 merge. MUST be stubbed: the real
        # `get_all_form_values` resolves the licence's bound form_ids with a live
        # query against the SIAP database, so leaving it unstubbed made this
        # suite pass where no SIAP DB exists and fail inside the container (where
        # licence 459 really does resolve to [560, 768]).
        # Default: the merge yields exactly the applicant form's values — i.e.
        # Penomoran 768 is empty, the normal state before an officer assigns a
        # No. PPKP. Pass `merged_form_values` to model 768 carrying data.
        _merged = (dict(merged_form_values)
                   if merged_form_values is not None else dict(_fv))
        _data = dict(rendered_data if rendered_data is not None else {})
        _classes = dict(rendered_classes if rendered_classes is not None else {})

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
            self._calls["get_applicant_form_id"] = license_id
            return form_id

        async def _get_form_value(fid, rid):
            # Record EVERY read, not just the last. Keying by name alone is what
            # hid this: the merge read 560 then 768, and 768 silently overwrote
            # the entry — so "get_form_value was called" could not distinguish
            # the applicant gate read from the licence-wide merge.
            self._calls.setdefault("get_form_value_calls", []).append((fid, rid))
            self._calls["get_form_value"] = (fid, rid)
            return dict(_fv)

        async def _get_all_form_values(license_id, request_id):
            self._calls["get_all_form_values"] = (license_id, request_id)
            return dict(_merged)

        async def _get_profile(profile_id):
            return {"full_name": "CASMO", "address": "Jl. Laut No. 1"}

        async def _get_case_meta(request_id):
            return {"found": True, "ticket": "000123456", "request_id": request_id,
                    "profile_id": 42}

        async def _get_license_name(license_id):
            return "Pengadaan Kapal (Pembangunan)"

        async def _render_output_docx(*, internal_filename, profile, case_meta,
                                       license_name, documents, ticket, out_prefix,
                                       overrides=None):
            self._calls["render_output_docx"] = {
                "overrides": overrides, "internal_filename": internal_filename,
            }
            fname = f"{out_prefix}_{ticket or 'draft'}.docx"
            return (b"PK\x03\x04docx", fname, _data, _classes)

        def _render_pdf(docx_bytes_in, *, ticket=None):
            return (b"%PDF", f"view_{ticket or 'draft'}.pdf")

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


class TestDraftSkGate(_DraftGateBase):
    def test_refuses_when_form_value_empty(self):
        out, queue = self._run_draft(form_id=560, form_value={})
        # No draft, no document queued.
        self.assertEqual(queue, [])
        self.assertIn("Formulir Isian", out)
        self.assertIn("kosong", out.lower())
        # render was never reached.
        self.assertNotIn("render_output_docx", self._calls)
        # The blank required-field labels are listed.
        self.assertIn("Nama Kapal", out)
        self.assertIn("GT", out)

    def test_refuses_when_all_vessel_keys_blank(self):
        # The form has some non-vessel keys but every vessel-subject key is blank.
        fv = {"nama_pemohon": "CASMO", "alamat": "Jl. Laut No. 1",
              "nama_kapal": "  ", "gt": ""}
        out, queue = self._run_draft(form_id=560, form_value=fv)
        self.assertEqual(queue, [])
        self.assertIn("kosong", out.lower())
        self.assertNotIn("render_output_docx", self._calls)

    def test_proceeds_when_form_value_filled(self):
        fv = {"nama_pemohon": "CASMO", "nama_kapal": "KM BERKAH", "gt": "12",
              "thn_bangun": "2020", "tipe": "Motor", "alat": "Pukat",
              "bahan": "Kayu", "galangan": "Rembang"}
        out, queue = self._run_draft(
            form_id=560, form_value=fv,
            rendered_data={"nama_pemohon": "CASMO", "nama_kapal": "KM BERKAH"},
            rendered_classes={"nama_pemohon": st_mod.SOURCE_FORM,
                              "nama_kapal": st_mod.SOURCE_FORM})
        # A draft WAS produced (docx + pdf queued) and render ran.
        self.assertTrue(len(queue) >= 1)
        self.assertIn("render_output_docx", self._calls)
        # The form_value was threaded as overrides (source of truth).
        self.assertEqual(self._calls["render_output_docx"]["overrides"], fv)
        self.assertIn("Rekomendasi", out)

    def test_falls_back_to_draft_when_form_id_none(self):
        # No discoverable applicant form → DO NOT gate; draft proceeds as before,
        # and overrides is None (no form_value read forced).
        out, queue = self._run_draft(
            form_id=None, form_value={},
            rendered_data={"nama_pemohon": "CASMO"},
            rendered_classes={"nama_pemohon": st_mod.SOURCE_PROFILE})
        self.assertTrue(len(queue) >= 1)
        self.assertIn("render_output_docx", self._calls)
        self.assertIsNone(self._calls["render_output_docx"]["overrides"])
        # What form_id=None actually short-circuits: the APPLICANT form is never
        # read, so the gate cannot fire. It does NOT stop the licence-wide merge
        # below — an earlier version of this test asserted that it did, which was
        # true until get_all_form_values landed two hours later the same day.
        self.assertEqual(self._calls.get("get_form_value_calls", []), [])
        # The 560+768 merge is REQUEST-scoped, not form_id-scoped, and still
        # runs: the officer's Penomoran form (768, no_ppkp) is a DIFFERENT form
        # and its number belongs on the draft whether or not the applicant form
        # is discoverable. It never raises and returns {} on a miss, so overrides
        # degrades to None here.
        self.assertEqual(self._calls.get("get_all_form_values"), (459, 777))
        self.assertIn("Rekomendasi", out)

    def test_gate_reads_only_the_applicant_form(self):
        # The companion to the above: when the applicant form IS discoverable it
        # is read exactly once, for the gate, with the applicant form_id.
        fv = {"nama_pemohon": "CASMO", "nama_kapal": "KM BERKAH", "gt": "12",
              "thn_bangun": "2020", "tipe": "Motor", "alat": "Pukat",
              "bahan": "Kayu", "galangan": "Rembang"}
        self._run_draft(form_id=560, form_value=fv,
                        rendered_data={"nama_pemohon": "CASMO"},
                        rendered_classes={"nama_pemohon": st_mod.SOURCE_FORM})
        self.assertEqual(self._calls.get("get_form_value_calls"), [(560, 777)])

    def test_penomoran_number_reaches_the_draft(self):
        # The contract the stale assertion was masking, and which nothing else
        # covered: no_ppkp lives on the OFFICER's Penomoran form (768), not on
        # the applicant's 560, and must reach the draft via the merge.
        fv = {"nama_pemohon": "CASMO", "nama_kapal": "KM BERKAH", "gt": "12",
              "thn_bangun": "2020", "tipe": "Motor", "alat": "Pukat",
              "bahan": "Kayu", "galangan": "Rembang"}
        merged = dict(fv, no_ppkp="503/PPKP/2026/001")
        self._run_draft(
            form_id=560, form_value=fv, merged_form_values=merged,
            rendered_data={"nama_pemohon": "CASMO"},
            rendered_classes={"nama_pemohon": st_mod.SOURCE_FORM})
        overrides = self._calls["render_output_docx"]["overrides"]
        self.assertEqual(overrides.get("no_ppkp"), "503/PPKP/2026/001")

    def test_merge_failure_degrades_to_the_applicant_form(self):
        # get_all_form_values returns {} on a miss/transient error; the draft
        # must then still use the applicant form_value read for the gate rather
        # than lose every field.
        fv = {"nama_pemohon": "CASMO", "nama_kapal": "KM BERKAH", "gt": "12",
              "thn_bangun": "2020", "tipe": "Motor", "alat": "Pukat",
              "bahan": "Kayu", "galangan": "Rembang"}
        self._run_draft(
            form_id=560, form_value=fv, merged_form_values={},
            rendered_data={"nama_pemohon": "CASMO"},
            rendered_classes={"nama_pemohon": st_mod.SOURCE_FORM})
        self.assertEqual(self._calls["render_output_docx"]["overrides"], fv)


# ===========================================================================
# Task 2 — CITIZEN auto-fill at submission (guided_submission)
# ===========================================================================


class _FakeDocClient:
    def __init__(self, *, configured=True, documents=None):
        self._configured = configured
        self._documents = documents if documents is not None else []
        self.list_calls = 0

    def is_configured(self):
        return self._configured

    async def list_documents(self, request_id):
        self.list_calls += 1
        return {"ok": True, "configured": True, "documents": list(self._documents)}


def _session(**over):
    # Realistic channel-prefixed id ('wa-{msisdn}') — the follow-up MUST route
    # through _send_to_user, which strips the prefix and picks the sender.
    sess = gs.SubmissionSession(user_id="wa-628123456789")
    sess.license_id = 459
    sess.license_name = "Pengadaan Kapal (Pembangunan)"
    sess.ticket = "000123456"
    sess.documents = [
        gs.SessionDocument(file_id="f1", claimed_type="KTP", filename="ktp.jpg",
                           mime_type="image/jpeg", content=b"xx"),
    ]
    for k, v in over.items():
        setattr(sess, k, v)
    return sess


class TestCitizenAutoFill(unittest.TestCase):
    """The submission auto-fill: schedules AFTER upload, calls _perform_form_fill
    with vision docs from sess.documents + slot docs from list_documents, sends a
    follow-up via whatsapp_sender.send_text, degrades silently on unavailability,
    and NEVER breaks the (already-returned) submit."""

    def _run_autofill(self, *, form_id=560, doc_client=None, core=None,
                      sent=None, perform_calls=None):
        patches = []

        async def _get_applicant_form_id(license_id):
            return form_id

        async def _get_profile(profile_id):
            return {"full_name": "CASMO", "address": "Jl. Laut No. 1"}

        async def _get_case_meta(request_id):
            return {"found": True, "ticket": "000123456", "request_id": request_id,
                    "profile_id": 42}

        async def _get_license_name(license_id):
            return "Pengadaan Kapal (Pembangunan)"

        _core = core if core is not None else {
            "ok": True, "configured": True, "form_value_id": 1,
            "filled": {"nama_pemohon": st_mod.SOURCE_PROFILE,
                       "no_siup": st_mod.SOURCE_VISION},
            "blank_keys": ["nama_kapal", "gt", "galangan"],
            "attached": {"up_ktp": 991}, "unattachable": [], "classes": {},
            "note": "", "http_status": 201,
        }

        async def _perform_form_fill(**kwargs):
            if perform_calls is not None:
                perform_calls.append(kwargs)
            return dict(_core)

        _sent = sent if sent is not None else []

        async def _send_text(recipient_phone, body, **kw):
            _sent.append({"recipient": recipient_phone, "body": body})
            return True

        fake_doc = doc_client if doc_client is not None else _FakeDocClient(
            documents=[{"file_id": 991, "filename": "ktp.jpg", "mime": "image/jpeg"}])

        # Patch the modules the coroutine imports lazily.
        patches.append(patch.object(sdb_mod, "get_applicant_form_id", _get_applicant_form_id))
        patches.append(patch.object(sdb_mod, "get_person_profile_properties", _get_profile))
        patches.append(patch.object(sdb_mod, "get_request_case_meta", _get_case_meta))
        patches.append(patch.object(sdb_mod, "get_license_name", _get_license_name))
        patches.append(patch.object(oc, "_perform_form_fill", _perform_form_fill))
        patches.append(patch(
            "services.siap_document_client.get_siap_document_client",
            lambda: fake_doc))
        patches.append(patch(
            "services.whatsapp_sender.send_text", _send_text))

        for p in patches:
            p.start()
        try:
            _run(gs._auto_fill_siap_form_for_citizen(_session(), 777, 42))
        finally:
            for p in patches:
                p.stop()
        return _sent, fake_doc

    def test_autofill_calls_perform_and_sends_followup(self):
        perform_calls: list = []
        sent, fake_doc = self._run_autofill(perform_calls=perform_calls)
        # _perform_form_fill was called with vision docs from sess.documents and
        # slot docs from list_documents.
        self.assertEqual(len(perform_calls), 1)
        pc = perform_calls[0]
        self.assertEqual(pc["request_id"], 777)
        self.assertEqual(pc["form_id"], 560)
        self.assertIn("f1", pc["vision_documents"])
        self.assertEqual(pc["vision_documents"]["f1"]["content"], b"xx")
        self.assertEqual(pc["slot_documents"][0]["file_id"], 991)
        # A follow-up WhatsApp was sent to the citizen: the blank field LABELS
        # (never the values), the filled ones as a COUNT. It deliberately does
        # not roll-call what was filled — that work is done and re-listing it
        # was a second wall of text after the ticket reply.
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["recipient"], "628123456789")
        body = sent[0]["body"]
        self.assertIn("Bapak/Ibu", body)
        self.assertIn("2 dari 5", body)          # filled counted, not listed
        self.assertNotIn("Nama Pemohon", body)   # a filled label: not re-listed
        self.assertIn("Nama Kapal", body)        # blank label
        # Never asks for data BIMA cannot receive: the session is gone by now.
        self.assertNotIn("kirimkan", body.lower())
        # list_documents was consulted for the slot docs.
        self.assertEqual(fake_doc.list_calls, 1)

    def test_degrades_silently_when_form_id_none(self):
        perform_calls: list = []
        sent, _ = self._run_autofill(form_id=None, perform_calls=perform_calls)
        # No fill attempted, no message.
        self.assertEqual(perform_calls, [])
        self.assertEqual(sent, [])

    def test_followup_routes_to_telegram_for_tg_citizen(self):
        # A 'tg-' citizen must get the follow-up via the Telegram sender, NOT the
        # WhatsApp sender — the channel prefix decides routing. Regression guard
        # for the misdelivery bug (raw prefixed id → whatsapp_sender).
        patches = []

        async def _get_applicant_form_id(license_id):
            return 560

        async def _get_profile(profile_id):
            return {"full_name": "Casmo"}

        async def _get_case_meta(request_id):
            return {"found": True, "profile_id": 42}

        async def _get_license_name(license_id):
            return "X"

        async def _perform_form_fill(**kwargs):
            return {"ok": True, "configured": True,
                    "filled": {"nama_pemohon": "profile"}, "blank_keys": ["gt"],
                    "attached": {}, "unattachable": [], "classes": {},
                    "note": "", "http_status": 201, "form_value_id": 1}

        tg_sent: list = []

        async def _tg_send(chat_id, body, **kw):
            tg_sent.append({"chat_id": chat_id, "text": body})
            return True

        async def _wa_send(recipient_phone, body, **kw):
            raise AssertionError("WhatsApp sender must NOT fire for a tg- citizen")

        patches.append(patch.object(sdb_mod, "get_applicant_form_id", _get_applicant_form_id))
        patches.append(patch.object(sdb_mod, "get_person_profile_properties", _get_profile))
        patches.append(patch.object(sdb_mod, "get_request_case_meta", _get_case_meta))
        patches.append(patch.object(sdb_mod, "get_license_name", _get_license_name))
        patches.append(patch.object(oc, "_perform_form_fill", _perform_form_fill))
        patches.append(patch(
            "services.siap_document_client.get_siap_document_client",
            lambda: _FakeDocClient(documents=[])))
        patches.append(patch("services.telegram_sender.send_text", _tg_send))
        patches.append(patch("services.whatsapp_sender.send_text", _wa_send))
        for p in patches:
            p.start()
        try:
            _run(gs._auto_fill_siap_form_for_citizen(
                _session(user_id="tg-99001122"), 777, 42))
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(len(tg_sent), 1)
        self.assertEqual(tg_sent[0]["chat_id"], "99001122")

    def test_degrades_silently_when_form_client_not_configured(self):
        core = {
            "ok": False, "configured": False, "form_value_id": None,
            "filled": {}, "blank_keys": [], "attached": {}, "unattachable": [],
            "classes": {}, "note": "not configured", "http_status": None,
        }
        sent, _ = self._run_autofill(core=core)
        # Form client not configured → NO citizen message (silent degrade).
        self.assertEqual(sent, [])

    def test_never_raises_when_send_fails(self):
        # A send failure must be swallowed (the submit already returned).
        patches = []

        async def _get_applicant_form_id(license_id):
            return 560

        async def _get_profile(profile_id):
            return {}

        async def _get_case_meta(request_id):
            return {"found": True, "profile_id": 42}

        async def _get_license_name(license_id):
            return "X"

        async def _perform_form_fill(**kwargs):
            return {"ok": True, "configured": True, "filled": {"nama_pemohon": "profile"},
                    "blank_keys": ["gt"], "attached": {}, "unattachable": [],
                    "classes": {}, "note": "", "http_status": 201,
                    "form_value_id": 1}

        async def _boom_send(recipient_phone, body, **kw):
            raise RuntimeError("wa down")

        patches.append(patch.object(sdb_mod, "get_applicant_form_id", _get_applicant_form_id))
        patches.append(patch.object(sdb_mod, "get_person_profile_properties", _get_profile))
        patches.append(patch.object(sdb_mod, "get_request_case_meta", _get_case_meta))
        patches.append(patch.object(sdb_mod, "get_license_name", _get_license_name))
        patches.append(patch.object(oc, "_perform_form_fill", _perform_form_fill))
        patches.append(patch(
            "services.siap_document_client.get_siap_document_client",
            lambda: _FakeDocClient(documents=[])))
        patches.append(patch("services.whatsapp_sender.send_text", _boom_send))
        for p in patches:
            p.start()
        try:
            # Must NOT raise.
            _run(gs._auto_fill_siap_form_for_citizen(_session(), 777, 42))
        finally:
            for p in patches:
                p.stop()

    def test_spawn_schedules_upload_then_fill_order(self):
        # _spawn_upload_then_fill runs the upload FIRST, then the auto-fill.
        order: list = []

        async def _fake_upload(sess, request_id):
            order.append("upload")

        async def _fake_autofill(sess, request_id, profile_id):
            order.append("autofill")

        async def _drive():
            with patch.object(gs, "_upload_docs_to_siap", _fake_upload), \
                 patch.object(gs, "_auto_fill_siap_form_for_citizen", _fake_autofill):
                gs._spawn_upload_then_fill(_session(), 777, 42)
                # Let the scheduled task run to completion.
                for _ in range(5):
                    await asyncio.sleep(0)
                    if len(order) >= 2:
                        break

        _run(_drive())
        self.assertEqual(order, ["upload", "autofill"])


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
