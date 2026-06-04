"""
Unit tests for the two reusable building blocks of the guided-submission
redesign:

  * services.submission_intent.classify_confirm_intent — the LLM JSON intent
    classifier (AFFIRM/DECLINE/CORRECT/QUESTION/UNCLEAR) used at the CONFIRM
    stage so the citizen never has to type a rigid keyword. Tests cover: the
    LLM path (mocked _call_gemma), the keyword fallback when the LLM is down /
    unparseable / out-of-vocab, and the always-on literal 'batal' hard-cancel.

  * services.gemini_vision.extract_ktp_fields — the KTP field extractor (name +
    NIK + gender) that replaces the 4-question Q&A. Tests cover: a clean read,
    a non-16-digit NIK dropped to None, gender normalisation, the not-a-KTP
    case, and the Vision-unconfigured / Vision-failure fallbacks (return None,
    never raise).

Run standalone (no pytest):

    python3 tests/test_submission_intent.py

No real network, no real Gemini.
"""

from __future__ import annotations

import asyncio
import os
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

from services import submission_intent as si  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# Hard cancel — the literal 'batal' is always a strong decline, no LLM.
# ===========================================================================

class TestHardCancel(unittest.TestCase):
    def test_literal_cancel_words(self):
        for w in ("batal", "Batal", "  batalkan ", "cancel", "stop", "berhenti"):
            self.assertTrue(si.is_hard_cancel(w), f"{w!r} should be hard cancel")

    def test_non_cancel_words(self):
        for w in ("ya", "oke pak lanjut", "nanti dulu", "batalkan pesanan saya yang lain"):
            self.assertFalse(si.is_hard_cancel(w), f"{w!r} should NOT be hard cancel")

    def test_hard_cancel_short_circuits_llm(self):
        # 'batal' must classify as DECLINE even with a key set and the LLM
        # patched to something else — it never reaches the model.
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}, clear=False):
            with patch("services.ai_handler._call_gemma",
                       new=AsyncMock(return_value='{"intent": "AFFIRM"}')):
                self.assertEqual(_run(si.classify_confirm_intent("batal")), si.DECLINE)


# ===========================================================================
# LLM path — _call_gemma mocked.
# ===========================================================================

class TestLLMClassifier(unittest.TestCase):
    def setUp(self):
        os.environ["GEMINI_API_KEY"] = "test-key"

    def tearDown(self):
        os.environ.pop("GEMINI_API_KEY", None)

    def _classify(self, raw_json, message="apa pun"):
        with patch("services.ai_handler._call_gemma",
                   new=AsyncMock(return_value=raw_json)):
            return _run(si.classify_confirm_intent(message))

    def test_affirm(self):
        self.assertEqual(self._classify('{"intent": "AFFIRM"}'), si.AFFIRM)

    def test_decline(self):
        self.assertEqual(self._classify('{"intent": "DECLINE"}'), si.DECLINE)

    def test_correct(self):
        self.assertEqual(self._classify('{"intent": "CORRECT"}'), si.CORRECT)

    def test_question(self):
        self.assertEqual(self._classify('{"intent": "QUESTION"}'), si.QUESTION)

    def test_unclear(self):
        self.assertEqual(self._classify('{"intent": "UNCLEAR"}'), si.UNCLEAR)

    def test_code_fences_stripped(self):
        self.assertEqual(self._classify('```json\n{"intent": "AFFIRM"}\n```'), si.AFFIRM)

    def test_out_of_vocab_falls_back_to_keyword(self):
        # Model drifts to a label we don't accept → keyword fallback on the
        # message. The message clearly affirms, so we still get AFFIRM.
        self.assertEqual(self._classify('{"intent": "MAYBE"}', "ya lanjut saja"), si.AFFIRM)

    def test_unparseable_falls_back_to_keyword(self):
        # Non-JSON from the model → keyword fallback. "nanti dulu" → DECLINE.
        self.assertEqual(self._classify("not json at all", "nanti dulu"), si.DECLINE)

    def test_llm_exception_falls_back_to_keyword(self):
        with patch("services.ai_handler._call_gemma",
                   new=AsyncMock(side_effect=RuntimeError("boom"))):
            self.assertEqual(_run(si.classify_confirm_intent("oke")), si.AFFIRM)


# ===========================================================================
# Keyword fallback — no GEMINI_API_KEY.
# ===========================================================================

class TestKeywordFallback(unittest.TestCase):
    def setUp(self):
        os.environ.pop("GEMINI_API_KEY", None)

    def _c(self, msg):
        return _run(si.classify_confirm_intent(msg))

    def test_affirm_variants(self):
        for msg in ("ya", "iya", "oke", "oke pak lanjut", "ya saya yakin",
                    "iya setuju", "boleh, lanjutkan", "betul", "gas", "silakan kirim"):
            self.assertEqual(self._c(msg), si.AFFIRM, f"{msg!r} should AFFIRM")

    def test_decline_variants(self):
        for msg in ("tidak", "nanti dulu", "belum", "jangan dulu", "ga jadi",
                    "tidak jadi", "batal", "stop", "nanti saja deh"):
            self.assertEqual(self._c(msg), si.DECLINE, f"{msg!r} should DECLINE")

    def test_ambiguous_is_unclear(self):
        # Without the LLM, a question / correction we can't keyword-match is
        # UNCLEAR — the flow then asks a gentle clarifier rather than guessing.
        for msg in ("ini buat apa?", "NIK saya 3374...", "hmm gimana ya"):
            self.assertEqual(self._c(msg), si.UNCLEAR, f"{msg!r} should be UNCLEAR")

    def test_empty(self):
        self.assertEqual(self._c(""), si.UNCLEAR)
        self.assertEqual(self._c("   "), si.UNCLEAR)


# ===========================================================================
# KTP field extractor (services.gemini_vision.extract_ktp_fields).
# ===========================================================================

from services import gemini_vision as gv  # noqa: E402


class TestKtpExtractor(unittest.TestCase):

    def _extract(self, parsed, *, configured=True):
        with patch("services.gemini_vision.is_configured", return_value=configured), \
             patch("services.gemini_vision.extract_structured",
                   new=AsyncMock(return_value=parsed)):
            return _run(gv.extract_ktp_fields(image_bytes=b"\xff\xd8\xff", mime_type="image/jpeg"))

    def test_clean_read(self):
        out = self._extract({
            "is_ktp": True, "nama": "Budi Santoso",
            "nik": "3374012345678901", "jenis_kelamin": "LAKI-LAKI",
        })
        self.assertEqual(out["is_ktp"], True)
        self.assertEqual(out["name"], "Budi Santoso")
        self.assertEqual(out["nik"], "3374012345678901")
        self.assertEqual(out["gender"], "LAKI-LAKI")

    def test_nik_with_separators_is_normalised(self):
        out = self._extract({
            "is_ktp": True, "nama": "Sri", "nik": "3374 0123 4567 8901",
            "jenis_kelamin": "PEREMPUAN",
        })
        self.assertEqual(out["nik"], "3374012345678901")
        self.assertEqual(out["gender"], "PEREMPUAN")

    def test_short_nik_dropped_to_none(self):
        # A garbled / partial NIK (not exactly 16 digits) must be None so the
        # flow asks for it rather than storing a wrong value.
        out = self._extract({"is_ktp": True, "nama": "Budi", "nik": "337401234", "jenis_kelamin": ""})
        self.assertEqual(out["name"], "Budi")
        self.assertIsNone(out["nik"])
        self.assertIsNone(out["gender"])

    def test_not_a_ktp(self):
        out = self._extract({"is_ktp": False, "nama": "", "nik": "", "jenis_kelamin": ""})
        self.assertFalse(out["is_ktp"])
        self.assertIsNone(out["name"])
        self.assertIsNone(out["nik"])

    def test_vision_unconfigured_returns_none(self):
        out = self._extract({"is_ktp": True}, configured=False)
        self.assertIsNone(out)

    def test_vision_failure_returns_none(self):
        # extract_structured returning None (network/parse failure) → None.
        out = self._extract(None)
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
