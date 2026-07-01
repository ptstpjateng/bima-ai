"""
Tests for the suitability judge agent — `services/agents/suitability_judge.py`.

Run standalone (no pytest needed — matches the project pattern in
tests/test_guided_submission.py and tests/test_siap_write.py):

    python -m tests.test_suitability_judge     # from ai-engine/
    python tests/test_suitability_judge.py     # also works

Covers:
  * Doc-class normalisation + requirement→class heuristics
  * Completeness scoring (registry present, empty, partial)
  * Type-correctness scoring with mocked Gemini Vision (match, mismatch,
    Other, Unknown / vision-down)
  * Suitability scoring with mocked Gemini Vision (match, partial,
    mismatch, unknown)
  * Issue merging + severity ordering
  * Graceful fallback when SIAP DB is not configured (registry empty)
  * Graceful fallback when SIAP DB raises (pool acquire blows up)
  * Per-license requirement cache TTL behaviour

No real Gemini calls, no real DB. Everything is patched at module
boundaries (`extract_structured`, `get_siap_pool`,
`is_siap_db_configured`, `vision_configured`).
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Make `services` importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Stub heavy deps so importing the agent succeeds without the full stack.
# ---------------------------------------------------------------------------

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
_ensure_stub("httpx")  # gemini_vision imports httpx at module level

from services.agents import suitability_judge as sj  # noqa: E402
from services.agents.suitability_judge import (  # noqa: E402
    UploadedDoc,
    detect_doc_type,
    judge_submission,
    judge_suitability,
    _canonical_class,
    _completeness,
    _requirement_implies_class,
    _suitability_avg_score,
    _type_avg_score,
)


def _run(coro):
    return asyncio.run(coro)


def _doc(file_id: str, claimed: str, filename: str = "f.jpg") -> UploadedDoc:
    return UploadedDoc(
        file_id=file_id,
        claimed_type=claimed,
        filename=filename,
        mime_type="image/jpeg",
        content=b"\xff\xd8\xff",  # JPEG SOI; opaque to the mock
    )


# ===========================================================================
# Canonicalisation helpers
# ===========================================================================

class TestCanonicalClass(unittest.TestCase):

    def test_aliases(self):
        cases = {
            "ktp": "KTP",
            "KTP": "KTP",
            "Kartu Tanda Penduduk": "KTP",
            "nib": "NIB",
            "nomor induk berusaha": "NIB",
            "NPWP": "NPWP",
            "Surat Domisili Usaha": "Surat_Domisili",
            "domisili": "Surat_Domisili",
            "pakta integritas": "Pakta_Integritas",
            "SK Pengangkatan Pengurus": "SK_Pengangkatan",
            "Akta Pendirian Notaris": "Akta_Pendirian",
            "IMB": "IMB_PBG",
            "pbg": "IMB_PBG",
        }
        for label, expected in cases.items():
            self.assertEqual(_canonical_class(label), expected, label)

    def test_unknown_label_is_other(self):
        self.assertEqual(_canonical_class("Sertifikat Halal"), "Other")
        self.assertEqual(_canonical_class(""), "Other")

    def test_requirement_implies_class(self):
        self.assertEqual(_requirement_implies_class("Fotokopi KTP"), "KTP")
        self.assertEqual(
            _requirement_implies_class("Surat Domisili Usaha dari Kelurahan"),
            "Surat_Domisili",
        )
        # "Surat permohonan" now maps to a real PPKP doc class.
        self.assertEqual(
            _requirement_implies_class("Surat permohonan bermaterai"), "Surat_Permohonan"
        )
        self.assertIsNone(_requirement_implies_class("Pas foto warna 3x4"))


# ===========================================================================
# Completeness
# ===========================================================================

class TestCompleteness(unittest.TestCase):

    def test_all_present(self):
        docs = [_doc("1", "KTP"), _doc("2", "NIB"), _doc("3", "NPWP")]
        required = ["Fotokopi KTP", "NIB", "NPWP"]
        c = _completeness(docs, required, registry_note=None)
        self.assertEqual(c.score, 1.0)
        self.assertEqual(c.missing, [])
        self.assertEqual(c.required, required)
        self.assertIsNone(c.note)

    def test_partial_missing(self):
        docs = [_doc("1", "KTP")]
        required = ["KTP", "NIB", "NPWP"]
        c = _completeness(docs, required, registry_note=None)
        self.assertAlmostEqual(c.score, round(1 / 3, 3))
        self.assertEqual(set(c.missing), {"NIB", "NPWP"})

    def test_empty_required_uses_note(self):
        c = _completeness([_doc("1", "KTP")], required=[], registry_note=None)
        self.assertEqual(c.score, 0.0)
        self.assertEqual(c.required, [])
        self.assertIsNotNone(c.note)

    def test_registry_note_propagates(self):
        c = _completeness([], required=[], registry_note="SIAP belum dikonfigurasi.")
        self.assertEqual(c.score, 0.0)
        self.assertEqual(c.note, "SIAP belum dikonfigurasi.")

    def test_unmappable_requirements_set_neutral_note(self):
        # Requirements that don't match any known doc class — score must be
        # zero and the note must explain "manual review".
        c = _completeness(
            [_doc("1", "KTP")],
            required=["Pas foto warna 3x4", "Bukti bayar retribusi"],
            registry_note=None,
        )
        self.assertEqual(c.score, 0.0)
        self.assertEqual(c.required, ["Pas foto warna 3x4", "Bukti bayar retribusi"])
        self.assertIsNotNone(c.note)


# ===========================================================================
# Type-correctness via mocked Gemini Vision
# ===========================================================================

class TestDetectDocType(unittest.TestCase):

    def test_vision_not_configured_returns_unknown(self):
        with patch("services.agents.suitability_judge.vision_configured",
                   return_value=False):
            finding = _run(detect_doc_type(_doc("f1", "KTP")))
        self.assertEqual(finding.detected_type, "Unknown")
        self.assertFalse(finding.matches)
        self.assertIsNotNone(finding.note)

    def test_match(self):
        with patch("services.agents.suitability_judge.vision_configured",
                   return_value=True), \
             patch("services.agents.suitability_judge.extract_structured",
                   new=AsyncMock(return_value={
                       "detected_type": "KTP",
                       "confidence": 0.94,
                       "rationale": "Header KARTU TANDA PENDUDUK visible.",
                   })):
            finding = _run(detect_doc_type(_doc("f1", "KTP")))
        self.assertEqual(finding.detected_type, "KTP")
        self.assertEqual(finding.claimed_type, "KTP")
        self.assertTrue(finding.matches)
        self.assertAlmostEqual(finding.confidence, 0.94)

    def test_mismatch(self):
        with patch("services.agents.suitability_judge.vision_configured",
                   return_value=True), \
             patch("services.agents.suitability_judge.extract_structured",
                   new=AsyncMock(return_value={
                       "detected_type": "NPWP",
                       "confidence": 0.88,
                   })):
            finding = _run(detect_doc_type(_doc("f1", "KTP")))
        self.assertEqual(finding.detected_type, "NPWP")
        self.assertFalse(finding.matches)

    def test_unrecognised_class_falls_back_to_other(self):
        with patch("services.agents.suitability_judge.vision_configured",
                   return_value=True), \
             patch("services.agents.suitability_judge.extract_structured",
                   new=AsyncMock(return_value={
                       "detected_type": "Sertifikat_Halal",
                       "confidence": 0.5,
                   })):
            finding = _run(detect_doc_type(_doc("f1", "KTP")))
        self.assertEqual(finding.detected_type, "Other")

    def test_vision_failure_returns_unknown(self):
        with patch("services.agents.suitability_judge.vision_configured",
                   return_value=True), \
             patch("services.agents.suitability_judge.extract_structured",
                   new=AsyncMock(return_value=None)):
            finding = _run(detect_doc_type(_doc("f1", "KTP")))
        self.assertEqual(finding.detected_type, "Unknown")
        self.assertEqual(finding.confidence, 0.0)
        self.assertIsNotNone(finding.note)


# ===========================================================================
# Suitability per pair
# ===========================================================================

class TestJudgeSuitability(unittest.TestCase):

    def test_match(self):
        with patch("services.agents.suitability_judge.vision_configured",
                   return_value=True), \
             patch("services.agents.suitability_judge.extract_structured",
                   new=AsyncMock(return_value={
                       "judgement": "match",
                       "evidence": "Address Jl. Pemuda matches.",
                       "confidence": 0.9,
                   })):
            f = _run(judge_suitability("Surat Domisili Usaha",
                                        _doc("f1", "Surat Domisili")))
        self.assertEqual(f.judgement, "match")
        self.assertAlmostEqual(f.confidence, 0.9)

    def test_unknown_when_vision_fails(self):
        with patch("services.agents.suitability_judge.vision_configured",
                   return_value=True), \
             patch("services.agents.suitability_judge.extract_structured",
                   new=AsyncMock(return_value=None)):
            f = _run(judge_suitability("Fotokopi KTP", _doc("f1", "KTP")))
        self.assertEqual(f.judgement, "unknown")
        self.assertEqual(f.confidence, 0.0)

    def test_unknown_when_judgement_invalid(self):
        with patch("services.agents.suitability_judge.vision_configured",
                   return_value=True), \
             patch("services.agents.suitability_judge.extract_structured",
                   new=AsyncMock(return_value={
                       "judgement": "BANANA",
                       "evidence": "—",
                       "confidence": 0.7,
                   })):
            f = _run(judge_suitability("Fotokopi KTP", _doc("f1", "KTP")))
        self.assertEqual(f.judgement, "unknown")


# ===========================================================================
# Score helpers
# ===========================================================================

class TestScoreHelpers(unittest.TestCase):

    def test_type_avg_match_high_confidence(self):
        # Two clean matches → score close to confidence average.
        findings = [
            sj.TypeCorrectnessFinding(
                file="a", file_id="1", claimed_type="KTP",
                detected_type="KTP", confidence=0.9, matches=True,
            ),
            sj.TypeCorrectnessFinding(
                file="b", file_id="2", claimed_type="NIB",
                detected_type="NIB", confidence=0.8, matches=True,
            ),
        ]
        self.assertGreater(_type_avg_score(findings), 0.7)

    def test_type_avg_mismatch_zero(self):
        findings = [
            sj.TypeCorrectnessFinding(
                file="a", file_id="1", claimed_type="KTP",
                detected_type="NPWP", confidence=0.95, matches=False,
            ),
        ]
        self.assertEqual(_type_avg_score(findings), 0.0)

    def test_type_avg_empty(self):
        self.assertEqual(_type_avg_score([]), 0.0)

    def test_meterai_missing_on_sign_doc_emits_high_issue(self):
        comp = sj.CompletenessSection(score=1.0, missing=[], required=["KTP"])
        findings = [
            sj.TypeCorrectnessFinding(
                file="pakta.pdf", file_id="1", claimed_type="Pakta_Integritas",
                detected_type="Pakta_Integritas", confidence=0.9, matches=True,
                has_meterai=False,
            ),
        ]
        issues = sj._build_issues(comp, findings, [], [])
        meterai = [i for i in issues if i.id.startswith("meterai:missing")]
        self.assertEqual(len(meterai), 1)
        self.assertEqual(meterai[0].severity, sj.SEVERITY_HIGH)

    def test_meterai_only_flagged_for_sign_docs(self):
        comp = sj.CompletenessSection(score=1.0, missing=[], required=["KTP"])
        findings = [
            sj.TypeCorrectnessFinding(
                file="pakta.pdf", file_id="1", claimed_type="Pakta_Integritas",
                detected_type="Pakta_Integritas", confidence=0.9, matches=True,
                has_meterai=True,   # present → no issue
            ),
            sj.TypeCorrectnessFinding(
                file="ktp.jpg", file_id="2", claimed_type="KTP",
                detected_type="KTP", confidence=0.9, matches=True,
                has_meterai=False,  # KTP needs no meterai → no issue
            ),
        ]
        issues = sj._build_issues(comp, findings, [], [])
        self.assertEqual([i for i in issues if i.id.startswith("meterai:missing")], [])

    def test_suitability_avg(self):
        findings = [
            sj.SuitabilityFinding(
                requirement="r1", file="a", file_id="1",
                judgement="match", evidence="x", confidence=0.9,
            ),
            sj.SuitabilityFinding(
                requirement="r2", file="b", file_id="2",
                judgement="mismatch", evidence="x", confidence=0.9,
            ),
        ]
        # match=1.0 * 0.9, mismatch=0.0 * 0.9 → avg = 0.45
        self.assertAlmostEqual(_suitability_avg_score(findings), 0.45, places=2)

    def test_suitability_avg_empty(self):
        self.assertEqual(_suitability_avg_score([]), 0.0)


# ===========================================================================
# Registry cache + SIAP fallback
# ===========================================================================

class TestRegistryFallback(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        sj._registry_cache_clear()

    async def asyncTearDown(self):
        sj._registry_cache_clear()

    async def test_siap_not_configured_returns_empty_with_note(self):
        with patch("services.agents.suitability_judge.is_siap_db_configured",
                   return_value=False):
            reqs, note = await sj.get_license_requirements(42)
        self.assertEqual(reqs, [])
        self.assertIsNotNone(note)

    async def test_siap_pool_raises_returns_empty_with_note(self):
        with patch("services.agents.suitability_judge.is_siap_db_configured",
                   return_value=True), \
             patch("services.agents.suitability_judge.get_siap_pool",
                   new=AsyncMock(side_effect=RuntimeError("boom"))):
            reqs, note = await sj.get_license_requirements(42)
        self.assertEqual(reqs, [])
        self.assertIn("Gagal", note or "")

    async def test_cache_hit_within_ttl(self):
        # Seed the cache by hand, then verify a second call doesn't touch
        # the DB-config check.
        sj._registry_cache[99] = (time.monotonic() + 60, ["Fotokopi KTP"])
        with patch("services.agents.suitability_judge.is_siap_db_configured") as m:
            reqs, note = await sj.get_license_requirements(99)
            m.assert_not_called()
        self.assertEqual(reqs, ["Fotokopi KTP"])
        self.assertIsNone(note)


# ===========================================================================
# End-to-end orchestration (mocked Vision + mocked registry)
# ===========================================================================

class TestJudgeSubmissionE2E(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        sj._registry_cache_clear()

    async def _mock_vision_classify_then_judge(self, type_responses, suit_responses):
        """Build an extract_structured mock that returns type-detect responses
        first, then suitability responses, in call order."""
        calls = list(type_responses) + list(suit_responses)
        async def _fake(*args, **kwargs):
            return calls.pop(0)
        return _fake

    async def test_full_happy_path(self):
        docs = [
            _doc("u1", "KTP", "ktp.jpg"),
            _doc("u2", "Surat Domisili", "domisili.pdf"),
        ]
        # Registry returns 2 requirements, both implying known classes.
        with patch(
            "services.agents.suitability_judge.get_license_requirements",
            new=AsyncMock(return_value=(
                ["Fotokopi KTP", "Surat Domisili Usaha"],
                None,
            )),
        ), patch(
            "services.agents.suitability_judge.vision_configured",
            return_value=True,
        ), patch(
            "services.agents.suitability_judge.extract_structured",
            new=AsyncMock(side_effect=[
                # type detection (2 docs, in order)
                {"detected_type": "KTP", "confidence": 0.95},
                {"detected_type": "Surat_Domisili", "confidence": 0.85},
                # suitability (2 requirement-doc pairs)
                {"judgement": "match", "evidence": "Header KTP visible.",
                 "confidence": 0.9},
                {"judgement": "match", "evidence": "Alamat usaha cocok.",
                 "confidence": 0.8},
            ]),
        ):
            result = await judge_submission(license_id=10, documents=docs)

        self.assertEqual(result.completeness.score, 1.0)
        self.assertEqual(result.completeness.missing, [])
        self.assertEqual(len(result.type_correctness), 2)
        self.assertTrue(all(f.matches for f in result.type_correctness))
        self.assertEqual(len(result.suitability), 2)
        self.assertTrue(all(f.judgement == "match" for f in result.suitability))
        # No issues expected — clean run.
        self.assertEqual(result.issues, [])
        self.assertGreater(result.overall_suitability_score, 0.8)

    async def test_type_mismatch_surfaces_high_severity_issue(self):
        docs = [_doc("u1", "KTP", "wrong.jpg")]
        with patch(
            "services.agents.suitability_judge.get_license_requirements",
            new=AsyncMock(return_value=(["Fotokopi KTP"], None)),
        ), patch(
            "services.agents.suitability_judge.vision_configured",
            return_value=True,
        ), patch(
            "services.agents.suitability_judge.extract_structured",
            new=AsyncMock(side_effect=[
                # Type detection — Gemini says it's an NPWP, not KTP.
                {"detected_type": "NPWP", "confidence": 0.9},
                # Suitability — Gemini also flags mismatch.
                {"judgement": "mismatch",
                 "evidence": "Tidak ada header KTP.",
                 "confidence": 0.85},
            ]),
        ):
            result = await judge_submission(license_id=10, documents=docs)

        # KTP requirement is "present" (the file was labelled KTP) so
        # completeness is 1.0 — type-correctness is what catches the lie.
        self.assertEqual(result.completeness.score, 1.0)
        self.assertEqual(len(result.type_correctness), 1)
        self.assertFalse(result.type_correctness[0].matches)
        # Issues: one type:mismatch (high) + one suitability:mismatch (high).
        severities = [i.severity for i in result.issues]
        self.assertIn("high", severities)
        # Type mismatch must rank ahead of (or alongside) low-severity items.
        ids = [i.id for i in result.issues]
        self.assertTrue(any(i.startswith("type:mismatch:") for i in ids))
        self.assertTrue(any(i.startswith("suitability:mismatch:") for i in ids))

    async def test_missing_doc_emits_critical_issue(self):
        # Only KTP uploaded, requirements ask for KTP + NPWP.
        docs = [_doc("u1", "KTP")]
        with patch(
            "services.agents.suitability_judge.get_license_requirements",
            new=AsyncMock(return_value=(["Fotokopi KTP", "NPWP"], None)),
        ), patch(
            "services.agents.suitability_judge.vision_configured",
            return_value=True,
        ), patch(
            "services.agents.suitability_judge.extract_structured",
            new=AsyncMock(side_effect=[
                {"detected_type": "KTP", "confidence": 0.95},
                {"judgement": "match", "evidence": "ok", "confidence": 0.9},
            ]),
        ):
            result = await judge_submission(license_id=10, documents=docs)

        self.assertIn("NPWP", result.completeness.missing)
        self.assertLess(result.completeness.score, 1.0)
        self.assertTrue(
            any(i.severity == "critical" and "NPWP" in i.title
                for i in result.issues),
            f"expected critical NPWP issue, got: {[i.title for i in result.issues]}",
        )

    async def test_registry_unavailable_degrades_gracefully(self):
        # SIAP DB unreachable → requirements empty, note populated. The
        # endpoint should still produce a type-correctness pass.
        docs = [_doc("u1", "KTP")]
        with patch(
            "services.agents.suitability_judge.get_license_requirements",
            new=AsyncMock(return_value=([], "SIAP belum dikonfigurasi.")),
        ), patch(
            "services.agents.suitability_judge.vision_configured",
            return_value=True,
        ), patch(
            "services.agents.suitability_judge.extract_structured",
            new=AsyncMock(return_value={"detected_type": "KTP", "confidence": 0.9}),
        ):
            result = await judge_submission(license_id=10, documents=docs)

        self.assertEqual(result.completeness.score, 0.0)
        self.assertIsNotNone(result.completeness.note)
        self.assertEqual(len(result.type_correctness), 1)
        # No suitability calls were made (no requirements).
        self.assertEqual(result.suitability, [])

    async def test_compatibility_findings_pass_through(self):
        docs = [_doc("u1", "KTP")]
        # Inject a pre-computed compatibility finding (as the v1 validator
        # would yield) and make sure it lands in the merged issue list.
        compat = [
            sj.Issue(
                id="compat:name_ktp_vs_nib",
                severity="high",
                title="Name KTP vs NIB",
                detail="Nama tidak konsisten.",
            )
        ]
        with patch(
            "services.agents.suitability_judge.get_license_requirements",
            new=AsyncMock(return_value=([], None)),
        ), patch(
            "services.agents.suitability_judge.vision_configured",
            return_value=True,
        ), patch(
            "services.agents.suitability_judge.extract_structured",
            new=AsyncMock(return_value={"detected_type": "KTP", "confidence": 0.9}),
        ):
            result = await judge_submission(
                license_id=10,
                documents=docs,
                compatibility_findings=compat,
            )

        self.assertEqual(result.compatibility_findings, compat)
        self.assertTrue(any(i.id == "compat:name_ktp_vs_nib" for i in result.issues))

    async def test_severity_ordering_critical_first(self):
        # One critical (missing doc) + one low (Unknown) — critical must come
        # first in the merged issue list.
        docs = [_doc("u1", "KTP")]
        with patch(
            "services.agents.suitability_judge.get_license_requirements",
            new=AsyncMock(return_value=(["Fotokopi KTP", "NPWP"], None)),
        ), patch(
            "services.agents.suitability_judge.vision_configured",
            return_value=True,
        ), patch(
            "services.agents.suitability_judge.extract_structured",
            new=AsyncMock(side_effect=[
                # Type detection — vision returns None to simulate failure
                # so we get a low-severity "unreadable" issue.
                None,
                # Suitability — vision returns None too (no requirement match
                # since KTP is uploaded but vision failed on the type pass).
                None,
            ]),
        ):
            result = await judge_submission(license_id=10, documents=docs)

        self.assertGreaterEqual(len(result.issues), 1)
        # First issue must be the critical missing-doc one.
        self.assertEqual(result.issues[0].severity, "critical")


if __name__ == "__main__":
    unittest.main(verbosity=2)
