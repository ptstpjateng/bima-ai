"""
Tests for the officer copilot's SIAP form-fill tool + the doc-view matcher fix.

Two features (task #83):

  1. fill_siap_form — fills the request's SIAP Formulir Isian (text fields +
     file slots) by REUSING the #80 no-hallucination fill engine
     (siap_templates.resolve_fill_data) for the text fields and mapping the
     APPLICANT-UPLOAD docs to the form's file slots, then calling
     siap_form_client.upsert_form. Asserts: fields filled from real sources only,
     docs mapped to the correct slots, the endpoint is called with the right
     shape, best-effort fallback on error, and a provenance-split note.

  2. _resolve_doc_ref alias/extension fix — the rehearsal's "desain kapal" /
     "gambar rancang bangun kapal" must resolve the "Desain_Kapal" upload, and
     the SIUP synonym set must too.

Run standalone (no pytest needed):

    python -m tests.test_officer_copilot_formfill   # from ai-engine/
    python tests/test_officer_copilot_formfill.py   # also works

The copilot's heavy deps (chromadb via rag_service, DB driver, Vision) are
stubbed so the module imports without them. resolve_fill_data / the SIAP DB
reads / the form client are monkeypatched — no Gemini, no network.
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


# Same leaf-dep stubs the sibling docsend suite uses (chromadb via rag_service,
# the DB driver, the name-normaliser's Vision dep). The real siap_* modules
# import cleanly once asyncpg is stubbed; we monkeypatch their functions per test.
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


# ---------------------------------------------------------------------------
# A fake form client that records the upsert_form call and returns a scripted
# result (ok/failed/not-configured), so we assert the endpoint shape + best-
# effort fallback without any network.
# ---------------------------------------------------------------------------


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


class _FillSiapFormBase(unittest.TestCase):
    """Shared monkeypatch scaffold for fill_siap_form."""

    def _stub(
        self,
        *,
        data=None,
        classes=None,
        form_id=560,
        request_id=777,
        profile=None,
        license_name="Pembangunan Kapal Penangkap Ikan",
        form_client=None,
    ):
        self._patched = []
        calls: dict = {}

        _data = dict(data if data is not None else {})
        _classes = dict(classes if classes is not None else {})

        async def _resolve_fill_data(keys, *, profile=None, case_meta=None,
                                     license_name=None, documents=None,
                                     run_vision=True):
            calls["resolve_fill_data"] = {
                "keys": list(keys), "profile": profile, "case_meta": case_meta,
                "license_name": license_name, "documents": documents,
                "run_vision": run_vision,
            }
            # Only return the classes for the requested keys (mirrors real).
            cls = {k: _classes.get(k, st_mod.SOURCE_BLANK) for k in keys}
            dat = {k: v for k, v in _data.items() if k in keys}
            return dat, cls

        async def _resolve_request_id(ticket):
            calls["resolve_request_id"] = ticket
            return request_id

        async def _get_applicant_form_id(license_id):
            calls["get_applicant_form_id"] = license_id
            return form_id

        async def _get_profile(profile_id):
            calls["get_profile"] = profile_id
            return dict(profile if profile is not None else {})

        async def _get_case_meta(rid):
            calls["get_case_meta"] = rid
            return {"found": True, "ticket": "000123456", "request_id": rid}

        async def _get_license_name(license_id):
            calls["get_license_name"] = license_id
            return license_name

        fc = form_client if form_client is not None else _FakeFormClient()
        calls["form_client"] = fc

        def _get_form_client():
            return fc

        def _patch(mod, attr, value):
            self._patched.append((mod, attr, getattr(mod, attr, None), hasattr(mod, attr)))
            setattr(mod, attr, value)

        _patch(st_mod, "resolve_fill_data", _resolve_fill_data)
        _patch(stools_mod, "siap_resolve_request_id", _resolve_request_id)
        _patch(sdb_mod, "get_applicant_form_id", _get_applicant_form_id)
        _patch(sdb_mod, "get_person_profile_properties", _get_profile)
        _patch(sdb_mod, "get_request_case_meta", _get_case_meta)
        _patch(sdb_mod, "get_license_name", _get_license_name)
        _patch(sfc_mod, "get_siap_form_client", _get_form_client)
        return calls

    def _restore(self):
        for mod, attr, old, existed in reversed(getattr(self, "_patched", [])):
            if existed:
                setattr(mod, attr, old)
            elif hasattr(mod, attr):
                delattr(mod, attr)
        self._patched = []

    def _run_fill(self, *, sk_ctx, doc_ctx=None, **stub_kwargs):
        calls = self._stub(**stub_kwargs)
        sk_token = oc._sk_context.set(sk_ctx)
        doc_token = oc._doc_context.set(doc_ctx)
        try:
            out = _run(oc.fill_siap_form())
        finally:
            oc._sk_context.reset(sk_token)
            oc._doc_context.reset(doc_token)
            self._restore()
        return out, calls

    def _sk_ctx(self):
        return {
            "license_id": 459, "ticket": "000123456",
            "license_name": "Pembangunan Kapal Penangkap Ikan",
            "profile_id": 42, "is_final_step": False,
            "step_stereotype": "SURVEY-LAPANGAN",
        }

    def _doc_ctx(self):
        # The APPLICANT-UPLOAD docs as the officer bridge injects them: the
        # document-API source keys them `siap:doc:{file_id}` — the trailing id is
        # the REAL ptsp.files.file_id the form-value endpoint copies into a slot.
        # Labels cover several slot classifications.
        return {
            "siap:doc:991": {"filename": "ktp_casmo.jpg", "claimed_type": "KTP",
                             "detected_type": "KTP", "content": b"x"},
            "siap:doc:992": {"filename": "scan_permohonan.pdf",
                             "claimed_type": "Lampiran",
                             "detected_type": "Surat_Permohonan", "content": b"x"},
            "siap:doc:993": {"filename": "nib_oss.pdf", "claimed_type": "NIB",
                             "detected_type": "SIUP", "content": b"x"},
            "siap:doc:994": {"filename": "Desain_Kapal.pdf",
                             "claimed_type": "Desain Kapal",
                             "detected_type": "Desain Kapal", "content": b"x"},
            "siap:doc:995": {"filename": "spec_alat.pdf",
                             "claimed_type": "Spesifikasi Alat Tangkap",
                             "detected_type": "Spesifikasi Alat Tangkap",
                             "content": b"x"},
        }


class TestFillSiapFormHappyPath(_FillSiapFormBase):

    def test_fills_fields_from_real_sources_and_maps_docs_to_slots(self):
        # nama_pemohon/alamat from profile; no_siup/nama_kapal from Vision;
        # jenis_pengadaan from case. blank keys are omitted (never fabricated).
        data = {
            "nama_pemohon": "CASMO", "alamat": "Jl. Laut No. 1",
            "jenis_pengadaan": "Pembangunan",
            "no_siup": "SIUP-123", "nama_kapal": "KM Berkah",
        }
        classes = {
            "nama_pemohon": st_mod.SOURCE_PROFILE,
            "alamat": st_mod.SOURCE_PROFILE,
            "jenis_pengadaan": st_mod.SOURCE_CASE,
            "no_siup": st_mod.SOURCE_VISION,
            "nama_kapal": st_mod.SOURCE_VISION,
        }
        out, calls = self._run_fill(
            sk_ctx=self._sk_ctx(), doc_ctx=self._doc_ctx(),
            data=data, classes=classes)

        # --- endpoint called with the right shape ---
        fc = calls["form_client"]
        self.assertEqual(len(fc.calls), 1)
        sent = fc.calls[0]
        self.assertEqual(sent["request_id"], 777)
        self.assertEqual(sent["form_id"], 560)
        # ONLY the real reads land in fields — nothing fabricated.
        self.assertEqual(sent["fields"], data)

        # --- docs mapped to the CORRECT slots; the slot value is the REAL SIAP
        #     file_id extracted from the `siap:doc:{id}` ctx key, not the key ---
        self.assertEqual(sent["files"]["up_ktp"], 991)
        self.assertEqual(sent["files"]["up_permohonan"], 992)
        self.assertEqual(sent["files"]["up_siup_oss"], 993)     # NIB/SIUP → siup slot
        self.assertEqual(sent["files"]["up_gambar_rancang_bangun"], 994)  # Desain Kapal
        self.assertEqual(sent["files"]["up_spesifikasi"], 995)

        # --- the #80 engine was fed the applicant form field keys + real data ---
        rfd = calls["resolve_fill_data"]
        self.assertIn("nama_pemohon", rfd["keys"])
        self.assertIn("nama_kapal", rfd["keys"])
        self.assertEqual(rfd["license_name"], "Pembangunan Kapal Penangkap Ikan")
        self.assertTrue(rfd["run_vision"])

        # --- provenance-split note: profile/case vs Vision, plus verify steer ---
        self.assertIn("DATA RESMI SIAP", out)
        self.assertIn("Nama Pemohon", out)          # official (profile)
        self.assertIn("DIBACA DARI DOKUMEN UNGGAHAN", out)
        self.assertIn("MOHON DIPERIKSA", out)        # Vision flagged
        self.assertIn("Simpan", out)                 # verify-then-save steer
        self.assertIn("SK akan dibuat SIAP", out)
        # edit link present
        self.assertIn("/admin/daftar-permohonans/777/edit", out)

    def test_form_id_is_discovered_not_hardcoded(self):
        # A different licence resolves a different form_id — proving the tool
        # calls get_applicant_form_id(license_id) rather than baking in 560.
        out, calls = self._run_fill(
            sk_ctx=self._sk_ctx(), doc_ctx=self._doc_ctx(),
            data={"nama_pemohon": "CASMO"},
            classes={"nama_pemohon": st_mod.SOURCE_PROFILE},
            form_id=612)
        self.assertEqual(calls["get_applicant_form_id"], 459)
        self.assertEqual(calls["form_client"].calls[0]["form_id"], 612)

    def test_only_confident_doc_matches_are_attached(self):
        # A doc that matches NO slot alias is skipped (never forced into a slot).
        doc_ctx = {
            "siap:doc:991": {"filename": "ktp.jpg", "claimed_type": "KTP",
                             "detected_type": "KTP", "content": b"x"},
            "siap:doc:999": {"filename": "random_note.pdf", "claimed_type": "Catatan",
                             "detected_type": "Catatan", "content": b"x"},
        }
        out, calls = self._run_fill(
            sk_ctx=self._sk_ctx(), doc_ctx=doc_ctx,
            data={}, classes={})
        files = calls["form_client"].calls[0]["files"]
        self.assertEqual(files, {"up_ktp": 991})   # the note is not mapped

    def test_profile_requirements_doc_not_attachable(self):
        # A profile_requirements-sourced doc (ctx key `siap:req:{id}`) is NOT an
        # APPLICANT-UPLOAD file, so it CANNOT be copied into a form slot — it is
        # skipped for the file mapping even though it matches a slot alias.
        doc_ctx = {
            "siap:req:12": {"filename": "Fotokopi KTP.jpg", "claimed_type": "KTP",
                            "detected_type": "KTP", "content": b"x"},
            "siap:doc:993": {"filename": "nib.pdf", "claimed_type": "NIB",
                             "detected_type": "SIUP", "content": b"x"},
        }
        out, calls = self._run_fill(
            sk_ctx=self._sk_ctx(), doc_ctx=doc_ctx, data={}, classes={})
        files = calls["form_client"].calls[0]["files"]
        # Only the document-API (APPLICANT-UPLOAD) doc is attached; the KTP from
        # profile_requirements is not.
        self.assertEqual(files, {"up_siup_oss": 993})


class TestFillSiapFormProvenanceAndBlanks(_FillSiapFormBase):

    def test_vision_read_flagged_separately_from_official(self):
        data = {"nama_pemohon": "CASMO", "no_siup": "SIUP-9"}
        classes = {
            "nama_pemohon": st_mod.SOURCE_PROFILE,
            "no_siup": st_mod.SOURCE_VISION,
        }
        out, _ = self._run_fill(
            sk_ctx=self._sk_ctx(), doc_ctx=self._doc_ctx(),
            data=data, classes=classes)
        # The official section names the profile field; the Vision section names
        # the OCR'd field. They are in DISTINCT sentences.
        official_idx = out.index("DATA RESMI SIAP")
        vision_idx = out.index("DIBACA DARI DOKUMEN UNGGAHAN")
        self.assertIn("Nama Pemohon", out[official_idx:vision_idx])
        self.assertIn("No. SIUP", out[vision_idx:])

    def test_blank_fields_reported_not_fabricated(self):
        # Only nama_pemohon read; every other field is blank → must be listed as
        # "belum terisi" and NOT sent to SIAP.
        out, calls = self._run_fill(
            sk_ctx=self._sk_ctx(), doc_ctx=self._doc_ctx(),
            data={"nama_pemohon": "CASMO"},
            classes={"nama_pemohon": st_mod.SOURCE_PROFILE})
        self.assertEqual(
            calls["form_client"].calls[0]["fields"], {"nama_pemohon": "CASMO"})
        self.assertIn("belum terisi", out.lower())
        # A blank field like GT is named in the blank list.
        self.assertIn("GT", out)


class TestFillSiapFormBestEffort(_FillSiapFormBase):

    def test_upsert_failure_returns_honest_message_not_crash(self):
        fc = _FakeFormClient(result={
            "ok": False, "configured": True, "http_status": 422,
            "form_value_id": None, "fields_set": [], "files_attached": [],
            "note": "Data Formulir Isian tidak valid menurut SIAP.",
        })
        out, calls = self._run_fill(
            sk_ctx=self._sk_ctx(), doc_ctx=self._doc_ctx(),
            data={"nama_pemohon": "CASMO"},
            classes={"nama_pemohon": st_mod.SOURCE_PROFILE},
            form_client=fc)
        self.assertIn("belum berhasil", out.lower())
        self.assertIn("tidak valid", out)
        # still steers the officer to SIAP + shows the edit link
        self.assertIn("langsung di SIAP", out)
        self.assertIn("/admin/daftar-permohonans/777/edit", out)

    def test_client_raise_is_caught(self):
        fc = _FakeFormClient(raise_exc=True)
        out, _ = self._run_fill(
            sk_ctx=self._sk_ctx(), doc_ctx=self._doc_ctx(),
            data={"nama_pemohon": "CASMO"},
            classes={"nama_pemohon": st_mod.SOURCE_PROFILE},
            form_client=fc)
        # The tool caught the raise and returned an honest failure line.
        self.assertIn("belum berhasil", out.lower())

    def test_not_configured_client_degrades(self):
        fc = _FakeFormClient(configured=False)
        out, calls = self._run_fill(
            sk_ctx=self._sk_ctx(), doc_ctx=self._doc_ctx(),
            data={"nama_pemohon": "CASMO"},
            classes={"nama_pemohon": st_mod.SOURCE_PROFILE},
            form_client=fc)
        # No upsert attempted; officer told to fill by hand, with what BIMA read.
        self.assertEqual(fc.calls, [])
        self.assertIn("belum aktif", out.lower())
        self.assertIn("Nama Pemohon", out)

    def test_no_sk_context_degrades(self):
        # Admin-dashboard path: no sk_context bound → honest degrade, no crash.
        out = _run(oc.fill_siap_form())
        self.assertIn("tidak tersedia pada sesi", out.lower())

    def test_missing_license_id_degrades(self):
        ctx = dict(self._sk_ctx())
        ctx["license_id"] = None
        out, calls = self._run_fill(sk_ctx=ctx, doc_ctx=self._doc_ctx())
        self.assertIn("license_id", out.lower())

    def test_unresolvable_request_id_degrades(self):
        out, calls = self._run_fill(
            sk_ctx=self._sk_ctx(), doc_ctx=self._doc_ctx(), request_id=None)
        self.assertIn("request_id", out.lower())
        # No form client call — we bail before writing.
        self.assertEqual(calls["form_client"].calls, [])

    def test_unresolvable_form_id_degrades(self):
        out, calls = self._run_fill(
            sk_ctx=self._sk_ctx(), doc_ctx=self._doc_ctx(), form_id=None)
        self.assertIn("formulir pemohon", out.lower())
        self.assertEqual(calls["form_client"].calls, [])


# ===========================================================================
# Doc-view matcher fix — the rehearsal dead-ends.
# ===========================================================================


class TestDocViewMatcherFix(unittest.TestCase):
    """`_resolve_doc_ref` must resolve a doc by requirement LABEL or FILENAME
    across synonyms + a stripped extension (the rehearsal: "desain kapal" /
    "gambar rancang bangun kapal" → Desain_Kapal.pdf)."""

    def _ctx(self):
        return {
            "d1": {"filename": "Desain_Kapal.pdf",
                   "claimed_type": "Desain Kapal",
                   "detected_type": "Desain Kapal", "content": b"x"},
            "d2": {"filename": "nib_oss.pdf", "claimed_type": "NIB",
                   "detected_type": "SIUP", "content": b"x"},
            "d3": {"filename": "ktp.jpg", "claimed_type": "KTP",
                   "detected_type": "KTP", "content": b"x"},
        }

    def test_desain_kapal_resolves(self):
        self.assertEqual(oc._resolve_doc_ref(self._ctx(), "desain kapal"), "d1")

    def test_gambar_rancang_bangun_kapal_resolves(self):
        # The exact rehearsal phrase that dead-ended.
        self.assertEqual(
            oc._resolve_doc_ref(self._ctx(), "gambar rancang bangun kapal"), "d1")

    def test_rancang_bangun_resolves(self):
        self.assertEqual(oc._resolve_doc_ref(self._ctx(), "rancang bangun"), "d1")

    def test_desain_kapal_resolves_by_filename_only(self):
        # A doc with ONLY a filename (no requirement label) still resolves via
        # the synonym → filename canonicalisation (extension stripped).
        ctx = {"only": {"filename": "Desain_Kapal.pdf", "claimed_type": "",
                        "detected_type": "", "content": b"x"}}
        self.assertEqual(
            oc._resolve_doc_ref(ctx, "gambar rancang bangun kapal"), "only")

    def test_nib_synonym_resolves_siup_upload(self):
        self.assertEqual(oc._resolve_doc_ref(self._ctx(), "NIB"), "d2")

    def test_izin_usaha_synonym_resolves_siup(self):
        self.assertEqual(oc._resolve_doc_ref(self._ctx(), "izin usaha"), "d2")

    def test_send_document_resolves_desain_kapal(self):
        # End-to-end via the send_document tool the officer actually triggers.
        doc_token = oc._doc_context.set(self._ctx())
        queue: list = []
        send_token = oc._docs_to_send_context.set(queue)
        try:
            out = oc.send_document("gambar rancang bangun kapal")
        finally:
            oc._doc_context.reset(doc_token)
            oc._docs_to_send_context.reset(send_token)
        self.assertIn("Desain_Kapal.pdf", out)
        self.assertEqual(queue, ["d1"])

    def test_extension_stripped_norm(self):
        self.assertEqual(oc._norm_ref("Desain_Kapal.pdf"), "desain kapal")
        self.assertEqual(oc._norm_ref("surat_pesanan.PDF"), "surat pesanan")

    def test_unknown_ref_still_returns_none(self):
        # A ref that matches no doc + no synonym must stay None (no false hit).
        self.assertIsNone(oc._resolve_doc_ref(self._ctx(), "akta notaris"))

    def test_ktp_still_resolves_after_change(self):
        # Regression: the pre-existing exact/substring path still works.
        self.assertEqual(oc._resolve_doc_ref(self._ctx(), "KTP"), "d3")


# ===========================================================================
# Slot-mapping over-match fix (the core issue): short bare aliases used to
# substring-match unrelated labels and mis-slot the WRONG document into a legal
# form. "oss"/"api" removed; matching is now whole-token, not substring.
# ===========================================================================


class TestSlotMappingOverMatchFix(unittest.TestCase):
    """`_map_docs_to_slots` must match aliases on WHOLE TOKENS, so a short alias
    never matches a substring inside a longer word — a mis-slot writes the wrong
    document into a legal form."""

    def _map_one(self, *, detected="", claimed="", filename="", key="siap:doc:1"):
        ctx = {key: {"filename": filename, "claimed_type": claimed,
                     "detected_type": detected, "content": b"x"}}
        return oc._map_docs_to_slots(ctx)

    # --- the two dangerous bare aliases must no longer over-match ---

    def test_grosse_akta_does_not_map_to_siup_slot(self):
        # "oss" (removed) used to substring-match "Grosse Akta" → wrong file into
        # the SIUP slot. It must now map to NOTHING.
        files, mapped, un = self._map_one(detected="Grosse Akta",
                                          filename="grosse_akta.pdf")
        self.assertNotIn("up_siup_oss", files)
        self.assertEqual(files, {})

    def test_rekapitulasi_does_not_map_to_spesifikasi_slot(self):
        # "api" (removed) used to substring-match "Rekapitulasi" → wrong file into
        # the Spesifikasi slot. It must now map to NOTHING.
        files, mapped, un = self._map_one(detected="Rekapitulasi Berkas",
                                          filename="rekap_berkas.pdf")
        self.assertNotIn("up_spesifikasi", files)
        self.assertEqual(files, {})

    def test_more_api_substring_words_do_not_mis_slot(self):
        # Common Indonesian words that CONTAIN "api" must not land in Spesifikasi.
        for label in ("Persiapan Dokumen", "Pengapian", "Surat rapi",
                      "Berkas Kapital"):
            files, _, _ = self._map_one(detected=label)
            self.assertNotIn("up_spesifikasi", files,
                             f"{label!r} wrongly mapped to up_spesifikasi")

    # --- the real docs the removed aliases used to (also) catch still map ---

    def test_real_siup_casmo_still_maps_to_siup_slot(self):
        files, _, _ = self._map_one(detected="SIUP CASMO", filename="siup.pdf")
        self.assertEqual(files, {"up_siup_oss": 1})

    def test_real_nib_still_maps_to_siup_slot(self):
        files, _, _ = self._map_one(detected="NIB", claimed="NIB",
                                    filename="nib_oss.pdf")
        self.assertEqual(files, {"up_siup_oss": 1})

    def test_real_spesifikasi_alat_tangkap_still_maps(self):
        files, _, _ = self._map_one(detected="Spesifikasi Alat Tangkap",
                                    filename="spec.pdf")
        self.assertEqual(files, {"up_spesifikasi": 1})

    def test_api_pukat_maps_via_scorer_detected_type_label(self):
        # The Casmo packet's alat-tangkap doc filename is "API Pukat Cincin Teri"
        # — after dropping bare "api" the FILENAME alone no longer matches, but
        # the citizen_scorer assigns detected_type "Spesifikasi Alat Tangkap"
        # which the whole-token match still catches. That label is the real
        # signal (per the task note), so the doc still slots correctly.
        files, _, _ = self._map_one(
            detected="Spesifikasi Alat Tangkap",
            claimed="Spesifikasi Alat Tangkap",
            filename="API Pukat Cincin Teri.pdf")
        self.assertEqual(files, {"up_spesifikasi": 1})

    def test_api_pukat_filename_only_no_longer_over_matches(self):
        # Sanity on the trade-off: with ONLY the filename "API Pukat Cincin Teri"
        # and no scorer label, it maps to NOTHING (dropping bare "api" is what
        # prevents the far more dangerous mis-slots). This is the intended,
        # safe behaviour — the scorer label above carries the real signal.
        files, _, _ = self._map_one(filename="API Pukat Cincin Teri.pdf")
        self.assertEqual(files, {})

    # --- whole-token matching semantics ---

    def test_short_alias_matches_whole_word_not_substring(self):
        self.assertTrue(oc._alias_matches_label("nib", "nib oss"))
        self.assertTrue(oc._alias_matches_label("ktp", "ktp"))
        self.assertFalse(oc._alias_matches_label("ktp", "ektp"))
        self.assertFalse(oc._alias_matches_label("nib", "grosse akta"))
        self.assertFalse(oc._alias_matches_label("api", "rekapitulasi"))

    def test_multiword_alias_matches_contiguous_token_run(self):
        self.assertTrue(
            oc._alias_matches_label("alat tangkap", "spesifikasi alat tangkap"))
        self.assertTrue(
            oc._alias_matches_label("surat pesanan", "surat pesanan casmo"))
        self.assertTrue(oc._alias_matches_label(
            "gambar rancang bangun", "gambar rancang bangun kapal"))
        # Non-contiguous tokens must NOT match.
        self.assertFalse(
            oc._alias_matches_label("alat tangkap", "alat berat tangkap"))

    def test_full_casmo_packet_maps_to_correct_slots(self):
        # The real Casmo packet, labelled as the scorer/bridge produce it, must
        # map every doc to its correct slot — no mis-slot, none dropped.
        packet = {
            "siap:doc:101": {"filename": "ktp_casmo.jpg", "claimed_type": "KTP",
                             "detected_type": "KTP", "content": b"x"},
            "siap:doc:102": {"filename": "siup_casmo.pdf", "claimed_type": "SIUP",
                             "detected_type": "SIUP", "content": b"x"},
            "siap:doc:103": {"filename": "Desain_Kapal.pdf",
                             "claimed_type": "Desain Kapal",
                             "detected_type": "Desain Kapal", "content": b"x"},
            "siap:doc:104": {"filename": "pakta.pdf",
                             "claimed_type": "Pakta Integritas",
                             "detected_type": "Pakta Integritas", "content": b"x"},
            "siap:doc:105": {"filename": "surat_pesanan.pdf",
                             "claimed_type": "Surat Pesanan",
                             "detected_type": "Surat Pesanan", "content": b"x"},
            "siap:doc:106": {"filename": "persetujuan_nama.pdf",
                             "claimed_type": "Persetujuan Nama Kapal",
                             "detected_type": "Persetujuan Nama Kapal",
                             "content": b"x"},
            "siap:doc:107": {"filename": "API Pukat Cincin Teri.pdf",
                             "claimed_type": "Spesifikasi Alat Tangkap",
                             "detected_type": "Spesifikasi Alat Tangkap",
                             "content": b"x"},
            "siap:doc:108": {"filename": "test_materai.pdf",
                             "claimed_type": "Surat Permohonan",
                             "detected_type": "Surat Permohonan", "content": b"x"},
        }
        files, mapped, un = oc._map_docs_to_slots(packet)
        self.assertEqual(files, {
            "up_ktp": 101,
            "up_siup_oss": 102,
            "up_gambar_rancang_bangun": 103,
            "up_pakta_integritas": 104,
            "up_kontrak": 105,
            "up_srt_kapal": 106,
            "up_spesifikasi": 107,
            "up_permohonan": 108,
        })
        self.assertEqual(un, [])


class TestUnattachableDocSurfaced(_FillSiapFormBase):
    """FIX 2 — a doc that MATCHES a slot alias but has no attachable file_id
    (a profile-requirements `siap:req:{id}`) must not be silently dropped; the
    officer note surfaces it so they can attach it by hand."""

    def test_unattachable_matched_doc_surfaced_in_note(self):
        # A req-sourced KTP matches up_ktp but can't be attached; an
        # APPLICANT-UPLOAD SIUP attaches normally.
        doc_ctx = {
            "siap:req:12": {"filename": "Fotokopi KTP.jpg", "claimed_type": "KTP",
                            "detected_type": "KTP", "content": b"x"},
            "siap:doc:993": {"filename": "nib.pdf", "claimed_type": "NIB",
                             "detected_type": "SIUP", "content": b"x"},
        }
        out, calls = self._run_fill(
            sk_ctx=self._sk_ctx(), doc_ctx=doc_ctx, data={}, classes={})
        files = calls["form_client"].calls[0]["files"]
        self.assertEqual(files, {"up_siup_oss": 993})
        # The recognised-but-unattachable KTP is surfaced, not dropped.
        self.assertIn("tidak dapat dilampirkan otomatis", out)
        self.assertIn("lampirkan manual di SIAP", out)
        self.assertIn("KTP", out)

    def test_map_returns_unattachable_list(self):
        # Unit-level: the mapper itself reports the unattachable match and does
        # NOT put it in `files`.
        doc_ctx = {
            "siap:req:12": {"filename": "Fotokopi KTP.jpg", "claimed_type": "KTP",
                            "detected_type": "KTP", "content": b"x"},
        }
        files, mapped, un = oc._map_docs_to_slots(doc_ctx)
        self.assertEqual(files, {})
        self.assertEqual(mapped, [])
        self.assertEqual(len(un), 1)
        self.assertIn("KTP", un[0])

    def test_no_unattachable_note_when_all_docs_attach(self):
        # When every matched doc has a real file_id, the note carries no
        # unattachable clause.
        out, calls = self._run_fill(
            sk_ctx=self._sk_ctx(), doc_ctx=self._doc_ctx(), data={}, classes={})
        self.assertNotIn("tidak dapat dilampirkan otomatis", out)


class TestDocViewApiAliasOverMatchFix(unittest.TestCase):
    """The bare "api" alias was also removed from the doc-view alias map, so
    `_canonical_doc_key` can no longer canonicalise a word merely CONTAINING
    "api" (Rekapitulasi, Persiapan) to the spesifikasi doc."""

    def test_rekapitulasi_does_not_canonicalise_to_spesifikasi(self):
        self.assertNotEqual(
            oc._canonical_doc_key("Rekapitulasi Berkas"),
            "spesifikasi_alat_tangkap")
        self.assertIsNone(oc._canonical_doc_key("Rekapitulasi Berkas"))

    def test_persiapan_does_not_canonicalise_to_spesifikasi(self):
        self.assertNotEqual(
            oc._canonical_doc_key("Persiapan Dokumen"),
            "spesifikasi_alat_tangkap")

    def test_real_spesifikasi_still_canonicalises(self):
        self.assertEqual(
            oc._canonical_doc_key("Spesifikasi Alat Tangkap"),
            "spesifikasi_alat_tangkap")
        self.assertEqual(
            oc._canonical_doc_key("alat tangkap"), "spesifikasi_alat_tangkap")

    def test_doc_view_resolver_does_not_resolve_rekapitulasi_to_spec(self):
        # End-to-end via _resolve_doc_ref: a ref "Rekapitulasi" must not resolve
        # a Spesifikasi Alat Tangkap doc.
        ctx = {"s1": {"filename": "spec.pdf", "claimed_type": "Spesifikasi Alat Tangkap",
                      "detected_type": "Spesifikasi Alat Tangkap", "content": b"x"}}
        self.assertIsNone(oc._resolve_doc_ref(ctx, "Rekapitulasi"))


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
