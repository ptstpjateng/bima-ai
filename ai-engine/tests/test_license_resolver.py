"""
Tests for the LLM-driven, catalogue-grounded licence resolver —
`services/license_resolver.py` and the `get_license_catalogue` cache in
`services/siap_tools.py`.

Run standalone (no pytest — matches the other ai-engine tests):

    python -m tests.test_license_resolver     # from ai-engine/
    python tests/test_license_resolver.py     # also works

What it covers (Gemini client + SIAP DB fully stubbed — no network, no DB):
  * get_license_catalogue — loads from a mocked pool, caches within the TTL,
    returns [] when SIAP DB is unconfigured, never raises on a DB error.
  * resolve_license_intent — the happy LLM path: a fake catalogue with a PKPP
    entry (id 459), a mocked LLM returning 459 → matches contains it. The same
    holds for "PKPP", "saya mau ajukan izin PKPP", and
    "persetujuan kapal perikanan" (the LLM is mocked to return 459 for each).
  * Hallucination guard — an id NOT in the catalogue is dropped; if only a
    hallucinated id is returned → found=False with the note.
  * Alternatives — best + alternatives are returned, deduped + validated.
  * LLM error → fuzzy SQL fallback still finds PKPP by tokens (mocked pool).
  * LLM null/none → found=False with the "sebutkan nama izin" note (no
    fallback — a clean miss is honoured).
  * Empty catalogue (SIAP down) → routes to fuzzy fallback (which also returns
    nothing when the DB is down → found=False).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Make `services` importable when run as a bare script.
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


# asyncpg is only needed at DB-call time (which we mock); stub it so importing
# siap_tools → siap_db succeeds without the driver installed.
_ensure_stub("asyncpg")
_ensure_stub("dotenv", {"load_dotenv": lambda *a, **k: None})

from services import license_resolver as lr  # noqa: E402
from services import siap_tools  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# A small fake catalogue. PKPP is the headline entry (id 459); the others give
# the resolver something to (correctly) reject as alternatives / hallucinations.
# ---------------------------------------------------------------------------

_PKPP = {
    "license_id": 459,
    "name": "Persetujuan Pengadaan Kapal Perikanan (PKPP)",
    "sektor": "Kelautan dan Perikanan",
}
_FAKE_CATALOGUE = [
    {"license_id": 12, "name": "Izin Pemakaian Tanah", "sektor": "Pekerjaan Umum"},
    {"license_id": 88, "name": "Izin Lingkungan", "sektor": "Lingkungan Hidup"},
    _PKPP,
    {"license_id": 460, "name": "Surat Izin Usaha Perikanan (SIUP)",
     "sektor": "Kelautan dan Perikanan"},
]


def _llm_returning(best, alternatives=None, confidence=0.9):
    """Build an AsyncMock for ai_handler._call_gemma that returns the given
    best/alternatives as the resolver's expected JSON (raw string, like the
    real Gemini REST call)."""
    payload = json.dumps({
        "best_license_id": best,
        "alternatives": alternatives or [],
        "confidence": confidence,
    })
    return AsyncMock(return_value=payload)


def _patch_llm(mock):
    """Patch the Gemini call the resolver imports from ai_handler.

    resolve_license_intent does `from services.ai_handler import _call_gemma`
    INSIDE the function, so patching the attribute on the ai_handler module is
    what takes effect.
    """
    return patch("services.ai_handler._call_gemma", new=mock)


def _patch_catalogue(catalogue):
    return patch("services.siap_tools.get_license_catalogue",
                 new=AsyncMock(return_value=catalogue))


# ===========================================================================
# get_license_catalogue cache
# ===========================================================================

class TestCatalogueCache(unittest.TestCase):

    def setUp(self):
        siap_tools._reset_license_catalogue_cache()

    def tearDown(self):
        siap_tools._reset_license_catalogue_cache()

    def _fake_pool(self, rows):
        """A pool whose acquire() context yields a conn.fetch returning rows."""
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=rows)
        acq = MagicMock()
        acq.__aenter__ = AsyncMock(return_value=conn)
        acq.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acq)
        return pool, conn

    def test_not_configured_returns_empty(self):
        with patch("services.siap_tools.is_siap_db_configured", return_value=False):
            out = _run(siap_tools.get_license_catalogue())
        self.assertEqual(out, [])

    def test_loads_and_caches(self):
        rows = [
            {"license_id": 459,
             "name": "Persetujuan Pengadaan Kapal Perikanan (PKPP)",
             "sektor": "Kelautan dan Perikanan"},
            {"license_id": 12, "name": "Izin Pemakaian Tanah",
             "sektor": "Pekerjaan Umum"},
        ]
        pool, conn = self._fake_pool(rows)
        with patch("services.siap_tools.is_siap_db_configured", return_value=True), \
             patch("services.siap_tools.get_siap_pool",
                   new=AsyncMock(return_value=pool)):
            first = _run(siap_tools.get_license_catalogue())
            # Second call within the TTL must NOT hit the pool again.
            second = _run(siap_tools.get_license_catalogue())
        self.assertEqual(len(first), 2)
        self.assertEqual(first[0]["license_id"], 459)
        self.assertEqual(first[0]["sektor"], "Kelautan dan Perikanan")
        self.assertIs(first, second)            # same cached object
        conn.fetch.assert_awaited_once()        # only one DB read

    def test_db_error_returns_empty_never_raises(self):
        with patch("services.siap_tools.is_siap_db_configured", return_value=True), \
             patch("services.siap_tools.get_siap_pool",
                   new=AsyncMock(side_effect=RuntimeError("db down"))):
            out = _run(siap_tools.get_license_catalogue())
        self.assertEqual(out, [])


# ===========================================================================
# resolve_license_intent — LLM happy path
# ===========================================================================

class TestResolveLLMPath(unittest.TestCase):

    def setUp(self):
        siap_tools._reset_license_catalogue_cache()

    def test_pkpp_acronym_resolves(self):
        with _patch_catalogue(_FAKE_CATALOGUE), _patch_llm(_llm_returning(459)):
            out = _run(lr.resolve_license_intent("PKPP"))
        self.assertTrue(out["found"])
        self.assertEqual(len(out["matches"]), 1)
        self.assertEqual(out["matches"][0]["license_id"], 459)
        self.assertIn("PKPP", out["matches"][0]["name"])
        self.assertEqual(out["matches"][0]["sektor"], "Kelautan dan Perikanan")
        self.assertIsNone(out["note"])

    def test_filler_phrasing_resolves(self):
        # The model is mocked to map the intent to 459 regardless of filler.
        with _patch_catalogue(_FAKE_CATALOGUE), _patch_llm(_llm_returning(459)):
            out = _run(lr.resolve_license_intent("saya mau ajukan izin PKPP"))
        self.assertTrue(out["found"])
        self.assertEqual(out["matches"][0]["license_id"], 459)

    def test_synonym_phrasing_resolves(self):
        with _patch_catalogue(_FAKE_CATALOGUE), _patch_llm(_llm_returning(459)):
            out = _run(lr.resolve_license_intent("persetujuan kapal perikanan"))
        self.assertTrue(out["found"])
        self.assertEqual(out["matches"][0]["license_id"], 459)

    def test_best_plus_alternatives(self):
        # best=459, alternatives=[460] → both returned, best first.
        with _patch_catalogue(_FAKE_CATALOGUE), \
             _patch_llm(_llm_returning(459, alternatives=[460, 459])):
            out = _run(lr.resolve_license_intent("kapal perikanan"))
        self.assertTrue(out["found"])
        ids = [m["license_id"] for m in out["matches"]]
        self.assertEqual(ids, [459, 460])       # deduped, best first

    def test_hallucinated_id_is_dropped(self):
        # best=999 (not in catalogue), alt=459 (valid) → only 459 survives.
        with _patch_catalogue(_FAKE_CATALOGUE), \
             _patch_llm(_llm_returning(999, alternatives=[459])):
            out = _run(lr.resolve_license_intent("kapal"))
        self.assertTrue(out["found"])
        ids = [m["license_id"] for m in out["matches"]]
        self.assertEqual(ids, [459])

    def test_only_hallucination_is_clean_miss(self):
        # The only id returned is not in the catalogue → found=False + note,
        # NOT a fuzzy fallback (the model spoke; it just hallucinated).
        fuzzy = AsyncMock()
        with _patch_catalogue(_FAKE_CATALOGUE), \
             _patch_llm(_llm_returning(999, alternatives=[1000])), \
             patch("services.license_resolver._fuzzy_fallback", new=fuzzy):
            out = _run(lr.resolve_license_intent("sesuatu yang aneh"))
        self.assertFalse(out["found"])
        self.assertEqual(out["matches"], [])
        self.assertIn("belum menemukan izin", out["note"])
        fuzzy.assert_not_awaited()

    def test_llm_null_is_clean_miss(self):
        # Model says nothing fits (best=null, no alternatives) → clean miss.
        fuzzy = AsyncMock()
        with _patch_catalogue(_FAKE_CATALOGUE), \
             _patch_llm(_llm_returning(None, alternatives=[])), \
             patch("services.license_resolver._fuzzy_fallback", new=fuzzy):
            out = _run(lr.resolve_license_intent("cuaca hari ini"))
        self.assertFalse(out["found"])
        self.assertIn("belum menemukan izin", out["note"])
        fuzzy.assert_not_awaited()

    def test_string_id_from_model_is_coerced(self):
        # Some models emit ids as strings — must still validate + enrich.
        with _patch_catalogue(_FAKE_CATALOGUE), _patch_llm(_llm_returning("459")):
            out = _run(lr.resolve_license_intent("PKPP"))
        self.assertTrue(out["found"])
        self.assertEqual(out["matches"][0]["license_id"], 459)


# ===========================================================================
# resolve_license_intent — fuzzy fallback
# ===========================================================================

class TestResolveFuzzyFallback(unittest.TestCase):

    def setUp(self):
        siap_tools._reset_license_catalogue_cache()

    def _fake_pool_for_fuzzy(self, and_rows, or_rows):
        """conn.fetch returns and_rows on the 1st call, or_rows on the 2nd."""
        conn = MagicMock()
        conn.fetch = AsyncMock(side_effect=[and_rows, or_rows])
        acq = MagicMock()
        acq.__aenter__ = AsyncMock(return_value=conn)
        acq.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acq)
        return pool, conn

    def test_llm_error_falls_back_to_fuzzy_and_finds_pkpp(self):
        # AND-match hits on tokens ["pengadaan","kapal","perikanan"].
        and_rows = [{
            "license_id": 459,
            "name": "Persetujuan Pengadaan Kapal Perikanan (PKPP)",
            "sektor": "Kelautan dan Perikanan",
        }]
        pool, conn = self._fake_pool_for_fuzzy(and_rows, [])
        with _patch_catalogue(_FAKE_CATALOGUE), \
             _patch_llm(AsyncMock(side_effect=RuntimeError("gemini 503"))), \
             patch("services.siap_db.is_siap_db_configured", return_value=True), \
             patch("services.siap_db.get_siap_pool",
                   new=AsyncMock(return_value=pool)):
            out = _run(lr.resolve_license_intent("pengadaan kapal perikanan"))
        self.assertTrue(out["found"])
        self.assertEqual(out["matches"][0]["license_id"], 459)
        # AND-match returned rows, so the OR query was never run.
        conn.fetch.assert_awaited_once()

    def test_llm_unparseable_falls_back_to_fuzzy(self):
        and_rows = [{
            "license_id": 459,
            "name": "Persetujuan Pengadaan Kapal Perikanan (PKPP)",
            "sektor": "Kelautan dan Perikanan",
        }]
        pool, conn = self._fake_pool_for_fuzzy(and_rows, [])
        with _patch_catalogue(_FAKE_CATALOGUE), \
             _patch_llm(AsyncMock(return_value="not json at all {{{")), \
             patch("services.siap_db.is_siap_db_configured", return_value=True), \
             patch("services.siap_db.get_siap_pool",
                   new=AsyncMock(return_value=pool)):
            out = _run(lr.resolve_license_intent("pengadaan kapal perikanan"))
        self.assertTrue(out["found"])
        self.assertEqual(out["matches"][0]["license_id"], 459)

    def test_fuzzy_or_retry_when_and_empty(self):
        # AND-match empty → OR-match returns a hit.
        or_rows = [{
            "license_id": 459,
            "name": "Persetujuan Pengadaan Kapal Perikanan (PKPP)",
            "sektor": "Kelautan dan Perikanan",
        }]
        pool, conn = self._fake_pool_for_fuzzy([], or_rows)
        with _patch_catalogue(_FAKE_CATALOGUE), \
             _patch_llm(AsyncMock(side_effect=TimeoutError("t"))), \
             patch("services.siap_db.is_siap_db_configured", return_value=True), \
             patch("services.siap_db.get_siap_pool",
                   new=AsyncMock(return_value=pool)):
            out = _run(lr.resolve_license_intent("kapal perikanan tangkap"))
        self.assertTrue(out["found"])
        self.assertEqual(out["matches"][0]["license_id"], 459)
        self.assertEqual(conn.fetch.await_count, 2)   # AND then OR

    def test_empty_catalogue_routes_to_fuzzy_then_misses_when_db_down(self):
        # Catalogue empty (SIAP down) and DB unconfigured → clean miss, no LLM.
        llm = AsyncMock()
        with _patch_catalogue([]), \
             _patch_llm(llm), \
             patch("services.siap_db.is_siap_db_configured", return_value=False):
            out = _run(lr.resolve_license_intent("PKPP"))
        self.assertFalse(out["found"])
        self.assertIn("belum menemukan izin", out["note"])
        llm.assert_not_awaited()      # never called the model with no catalogue

    def test_fuzzy_no_content_tokens_is_miss(self):
        # All-stopword message → no tokens → miss, DB never touched.
        with _patch_catalogue(_FAKE_CATALOGUE), \
             _patch_llm(AsyncMock(side_effect=RuntimeError("x"))), \
             patch("services.siap_db.is_siap_db_configured", return_value=True), \
             patch("services.siap_db.get_siap_pool",
                   new=AsyncMock(side_effect=AssertionError("should not query"))):
            out = _run(lr.resolve_license_intent("saya mau ajukan izin"))
        self.assertFalse(out["found"])


# ===========================================================================
# Tokenizer unit checks
# ===========================================================================

class TestTokenizer(unittest.TestCase):

    def test_drops_stopwords_and_short_tokens(self):
        toks = lr._tokenize_for_fallback("saya mau ajukan izin PKPP di Semarang")
        # "saya","mau","ajukan","izin","di" dropped; "pkpp","semarang" kept.
        self.assertIn("pkpp", toks)
        self.assertIn("semarang", toks)
        self.assertNotIn("izin", toks)
        self.assertNotIn("di", toks)

    def test_empty_on_all_stopwords(self):
        self.assertEqual(lr._tokenize_for_fallback("saya mau ajukan izin"), [])

    def test_mask_is_content_free(self):
        masked = lr._mask_msg("CV Rahasia Banget 12345")
        self.assertNotIn("Rahasia", masked)
        self.assertIn("len=", masked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
