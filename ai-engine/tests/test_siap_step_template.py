"""
Tests for the PER-STEP output-template selection (task #80) and the
person-profile / licence-name reads that ground the generic fill engine.

Covers:
  * siap_tools._preferred_stereotype_for_step — the desk→doc decision (pure).
  * siap_tools.siap_get_step_output_template — Rekomtek vs SK selection with a
    fallback when the preferred stereotype is missing / not a fillable .docx.
  * siap_db.get_person_profile_properties — reads ptsp.person_profile.properties
    (dict or JSON-string), degrades to {} on any miss.
  * siap_db.get_license_name.

Run standalone (no pytest — matches tests/test_officer_flow_notify.py):

    python -m tests.test_siap_step_template     # from ai-engine/

The DB pool is a fake (no asyncpg, no network); asyncpg/dotenv are stubbed so
the modules import in a bare env.
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


_ensure_stub("asyncpg")
_ensure_stub("dotenv", {"load_dotenv": lambda *a, **k: None})

import services.siap_db as siap_db  # noqa: E402
import services.siap_tools as siap_tools  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fake pool — the step-template query is a single fetchrow per stereotype
# (SELECT … FROM ptsp.files WHERE stereotype = $2 AND owner_id = $1). We route
# the row by the SECOND positional arg ($2 = stereotype).
# ---------------------------------------------------------------------------


class _FilesConn:
    def __init__(self, by_stereotype: dict):
        self.by = by_stereotype

    async def fetchrow(self, sql, *args):
        if "FROM ptsp.files" in sql:
            # args = (owner_id, stereotype)
            stereo = args[1] if len(args) >= 2 else None
            return self.by.get(stereo)
        if "FROM ptsp.person_profile" in sql:
            return self.by.get("_profile_row")
        if "FROM ptsp.license" in sql and "name" in sql:
            return self.by.get("_license_row")
        raise AssertionError(f"unexpected SQL: {sql[:60]}")


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


class _MultiPatch:
    """Patch is_siap_db_configured + get_siap_pool on BOTH siap_db (where the
    person-profile / licence-name reads live) AND siap_tools (which imported the
    two names at module load, so it holds its own references)."""

    def __init__(self, conn):
        pool = _FakePool(conn)
        self._patches = [
            patch.object(siap_db, "is_siap_db_configured", lambda: True),
            patch.object(siap_db, "get_siap_pool", AsyncMock(return_value=pool)),
            patch.object(siap_tools, "is_siap_db_configured", lambda: True),
            patch.object(siap_tools, "get_siap_pool", AsyncMock(return_value=pool)),
        ]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


def _install_pool(conn):
    return _MultiPatch(conn)


def _rek_row():
    return {"file_id": 1533, "file_name": "Surat PKPP V2.docx",
            "file_type": "docx", "internal_filename": "master/REK.docx"}


def _sk_row():
    return {"file_id": 900, "file_name": "SK Izin.docx",
            "file_type": "docx", "internal_filename": "master/SK.docx"}


# ---------------------------------------------------------------------------
# _preferred_stereotype_for_step — pure decision
# ---------------------------------------------------------------------------


class TestPreferredStereotype(unittest.TestCase):
    def test_final_step_prefers_sk(self):
        self.assertEqual(
            siap_tools._preferred_stereotype_for_step(
                is_final_step=True, step_stereotype="TTE"),
            "LICENSE-SK",
        )

    def test_technical_step_prefers_recommend(self):
        self.assertEqual(
            siap_tools._preferred_stereotype_for_step(
                is_final_step=False, step_stereotype="SURVEY-LAPANGAN"),
            "LICENSE-RECOMMEND",
        )

    def test_sk_stereotype_without_final_flag_still_sk(self):
        # 'PENANDATANGANAN BERKAS' is the single-signer Rekomtek/SK signing step.
        self.assertEqual(
            siap_tools._preferred_stereotype_for_step(
                is_final_step=False, step_stereotype="PENANDATANGANAN BERKAS"),
            "LICENSE-SK",
        )

    def test_unknown_stereotype_defaults_to_recommend(self):
        self.assertEqual(
            siap_tools._preferred_stereotype_for_step(
                is_final_step=False, step_stereotype="VERIFIKASI"),
            "LICENSE-RECOMMEND",
        )
        self.assertEqual(
            siap_tools._preferred_stereotype_for_step(
                is_final_step=False, step_stereotype=None),
            "LICENSE-RECOMMEND",
        )


# ---------------------------------------------------------------------------
# siap_get_step_output_template — selection + fallback
# ---------------------------------------------------------------------------


class TestStepOutputTemplate(unittest.TestCase):
    def test_technical_desk_returns_recommend(self):
        conn = _FilesConn({"LICENSE-RECOMMEND": _rek_row(), "LICENSE-SK": _sk_row()})
        with _install_pool(conn):
            out = _run(siap_tools.siap_get_step_output_template(
                459, is_final_step=False, step_stereotype="SURVEY-LAPANGAN"))
        self.assertTrue(out["found"])
        self.assertEqual(out["stereotype"], "LICENSE-RECOMMEND")
        self.assertEqual(out["doc_kind"], "rekomendasi")
        self.assertFalse(out["fell_back"])
        self.assertEqual(out["internal_filename"], "master/REK.docx")

    def test_signing_desk_returns_sk(self):
        conn = _FilesConn({"LICENSE-RECOMMEND": _rek_row(), "LICENSE-SK": _sk_row()})
        with _install_pool(conn):
            out = _run(siap_tools.siap_get_step_output_template(
                507, is_final_step=True, step_stereotype="TTE"))
        self.assertTrue(out["found"])
        self.assertEqual(out["stereotype"], "LICENSE-SK")
        self.assertEqual(out["doc_kind"], "sk")
        self.assertFalse(out["fell_back"])

    def test_signing_desk_falls_back_to_recommend_when_sk_missing(self):
        # Step wants SK but only a fillable Rekomtek exists → fall back + flag.
        conn = _FilesConn({"LICENSE-RECOMMEND": _rek_row(), "LICENSE-SK": None})
        with _install_pool(conn):
            out = _run(siap_tools.siap_get_step_output_template(
                459, is_final_step=True, step_stereotype="TTE"))
        self.assertTrue(out["found"])
        self.assertEqual(out["preferred_stereotype"], "LICENSE-SK")
        self.assertEqual(out["stereotype"], "LICENSE-RECOMMEND")
        self.assertTrue(out["fell_back"])

    def test_sk_non_docx_falls_back_to_fillable_recommend(self):
        # SK exists but is a legacy .rtf (not fillable) → prefer the .docx Rekom.
        sk_rtf = dict(_sk_row())
        sk_rtf["internal_filename"] = "master/SK.rtf"
        conn = _FilesConn({"LICENSE-RECOMMEND": _rek_row(), "LICENSE-SK": sk_rtf})
        with _install_pool(conn):
            out = _run(siap_tools.siap_get_step_output_template(
                25, is_final_step=True, step_stereotype="TTE"))
        self.assertTrue(out["found"])
        self.assertEqual(out["stereotype"], "LICENSE-RECOMMEND")
        self.assertTrue(out["fell_back"])

    def test_only_non_fillable_sk_still_surfaces_with_that_stereotype(self):
        # Neither is a fillable .docx (SK is .rtf, no Rekom) → surface the SK row
        # so the caller can report "template ada tapi belum bisa diisi".
        sk_rtf = dict(_sk_row())
        sk_rtf["internal_filename"] = "master/SK.rtf"
        conn = _FilesConn({"LICENSE-RECOMMEND": None, "LICENSE-SK": sk_rtf})
        with _install_pool(conn):
            out = _run(siap_tools.siap_get_step_output_template(
                25, is_final_step=True, step_stereotype="TTE"))
        self.assertTrue(out["found"])
        self.assertEqual(out["stereotype"], "LICENSE-SK")
        self.assertEqual(out["internal_filename"], "master/SK.rtf")

    def test_no_templates_not_found(self):
        conn = _FilesConn({"LICENSE-RECOMMEND": None, "LICENSE-SK": None})
        with _install_pool(conn):
            out = _run(siap_tools.siap_get_step_output_template(
                999, is_final_step=False))
        self.assertFalse(out["found"])
        self.assertEqual(out["preferred_stereotype"], "LICENSE-RECOMMEND")

    def test_unconfigured_db_degrades(self):
        with patch.object(siap_tools, "is_siap_db_configured", lambda: False):
            out = _run(siap_tools.siap_get_step_output_template(459))
        self.assertFalse(out["found"])


# ---------------------------------------------------------------------------
# siap_db.get_person_profile_properties
# ---------------------------------------------------------------------------


class TestPersonProfileRead(unittest.TestCase):
    def test_reads_dict_properties(self):
        conn = _FilesConn({"_profile_row": {
            "properties": {"full_name": "CASMO", "address": "Jl. Laut No. 1"}
        }})
        with _install_pool(conn):
            out = _run(siap_db.get_person_profile_properties(42))
        self.assertEqual(out["full_name"], "CASMO")
        self.assertEqual(out["address"], "Jl. Laut No. 1")

    def test_reads_json_string_properties(self):
        # asyncpg without a JSONB codec returns the column as a str.
        conn = _FilesConn({"_profile_row": {
            "properties": '{"full_name": "SUTRISNO", "identity_number": "3374012345678901"}'
        }})
        with _install_pool(conn):
            out = _run(siap_db.get_person_profile_properties(43))
        self.assertEqual(out["full_name"], "SUTRISNO")
        self.assertEqual(out["identity_number"], "3374012345678901")

    def test_missing_profile_returns_empty(self):
        conn = _FilesConn({"_profile_row": None})
        with _install_pool(conn):
            out = _run(siap_db.get_person_profile_properties(999))
        self.assertEqual(out, {})

    def test_invalid_json_returns_empty(self):
        conn = _FilesConn({"_profile_row": {"properties": "{not json"}})
        with _install_pool(conn):
            out = _run(siap_db.get_person_profile_properties(44))
        self.assertEqual(out, {})

    def test_unconfigured_returns_empty(self):
        with patch.object(siap_db, "is_siap_db_configured", lambda: False):
            out = _run(siap_db.get_person_profile_properties(42))
        self.assertEqual(out, {})

    def test_bad_profile_id_returns_empty(self):
        with patch.object(siap_db, "is_siap_db_configured", lambda: True):
            out = _run(siap_db.get_person_profile_properties("not-an-int"))
        self.assertEqual(out, {})


class TestLicenseNameRead(unittest.TestCase):
    def test_reads_name(self):
        conn = _FilesConn({"_license_row": {"name": "Pengadaan Kapal (Pembangunan)"}})
        with _install_pool(conn):
            out = _run(siap_db.get_license_name(459))
        self.assertEqual(out, "Pengadaan Kapal (Pembangunan)")

    def test_missing_returns_none(self):
        conn = _FilesConn({"_license_row": None})
        with _install_pool(conn):
            out = _run(siap_db.get_license_name(999))
        self.assertIsNone(out)

    def test_unconfigured_returns_none(self):
        with patch.object(siap_db, "is_siap_db_configured", lambda: False):
            out = _run(siap_db.get_license_name(459))
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
