"""
Tests for the citizen content-scoring entrypoint — `services/citizen_scorer.py`.

Run standalone (no pytest — matches tests/test_guided_submission.py):

    python -m tests.test_citizen_scorer     # from ai-engine/
    python tests/test_citizen_scorer.py     # also works

Covers:
  * score_session_documents — thin pass-through to judge_submission (mocked).
  * render_score_message — headline %, completeness line, per-doc type lines,
    suitability flags, prioritized issues, with/without license name.
  * is_submission_ready — threshold + critical-issue gating.

No real Gemini, no real DB. judge_submission is patched; the renderer is
exercised against hand-built SuitabilityResult dataclasses.
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
_ensure_stub("httpx")

from services import citizen_scorer as cs  # noqa: E402
from services.agents.suitability_judge import (  # noqa: E402
    CompletenessSection,
    Issue,
    SuitabilityFinding,
    SuitabilityResult,
    TypeCorrectnessFinding,
    UploadedDoc,
)


def _run(coro):
    return asyncio.run(coro)


def _result(
    *,
    overall=0.9,
    completeness=None,
    type_correctness=None,
    suitability=None,
    issues=None,
) -> SuitabilityResult:
    return SuitabilityResult(
        completeness=completeness
        or CompletenessSection(score=1.0, missing=[], required=["KTP", "NIB"]),
        type_correctness=type_correctness or [],
        suitability=suitability or [],
        compatibility_findings=[],
        overall_suitability_score=overall,
        issues=issues or [],
    )


class TestScoreSessionDocuments(unittest.TestCase):
    def test_passes_through_to_judge(self):
        fake = _result(overall=0.77)
        docs = [
            UploadedDoc(
                file_id="doc-1",
                claimed_type="ktp",
                filename="ktp.jpg",
                mime_type="image/jpeg",
                content=b"x",
            )
        ]
        with patch.object(
            cs, "judge_submission", new=AsyncMock(return_value=fake)
        ) as mocked:
            out = _run(
                cs.score_session_documents(license_id=358, documents=docs)
            )
        self.assertIs(out, fake)
        mocked.assert_awaited_once()
        # license_id + documents forwarded.
        _, kwargs = mocked.call_args
        self.assertEqual(kwargs["license_id"], 358)
        self.assertEqual(kwargs["documents"], docs)


class TestRenderScoreMessage(unittest.TestCase):
    def test_headline_percent_and_completeness(self):
        res = _result(
            overall=0.86,
            completeness=CompletenessSection(
                score=0.857, missing=["NPWP"], required=["KTP", "NIB", "NPWP"]
            ),
        )
        msg = cs.render_score_message(res, license_name="Izin Penelitian")
        self.assertIn("Izin Penelitian", msg)
        self.assertIn("86%", msg)
        # 3 required, 1 missing → 2/3 lengkap.
        self.assertIn("2/3", msg)

    def test_type_mismatch_line(self):
        res = _result(
            type_correctness=[
                TypeCorrectnessFinding(
                    file="ktp.jpg",
                    file_id="doc-1",
                    claimed_type="KTP",
                    detected_type="NPWP",
                    confidence=0.9,
                    matches=False,
                ),
                TypeCorrectnessFinding(
                    file="nib.pdf",
                    file_id="doc-2",
                    claimed_type="NIB",
                    detected_type="NIB",
                    confidence=0.95,
                    matches=True,
                ),
            ]
        )
        msg = cs.render_score_message(res)
        self.assertIn("ktp.jpg", msg)
        self.assertIn("terbaca sebagai", msg)
        self.assertIn("nib.pdf", msg)

    def test_suitability_mismatch_and_partial(self):
        res = _result(
            suitability=[
                SuitabilityFinding(
                    requirement="Surat Permohonan materai",
                    file="surat.pdf",
                    file_id="doc-1",
                    judgement="mismatch",
                    evidence="tidak ada materai",
                    confidence=0.8,
                ),
                SuitabilityFinding(
                    requirement="Proposal",
                    file="prop.pdf",
                    file_id="doc-2",
                    judgement="partial",
                    evidence="metodologi belum lengkap",
                    confidence=0.6,
                ),
                SuitabilityFinding(
                    requirement="KTP",
                    file="ktp.jpg",
                    file_id="doc-3",
                    judgement="match",
                    evidence="ok",
                    confidence=0.9,
                ),
            ]
        )
        msg = cs.render_score_message(res)
        self.assertIn("Surat Permohonan materai", msg)
        self.assertIn("tidak ada materai", msg)
        self.assertIn("metodologi belum lengkap", msg)
        # A 'match' suitability finding is NOT surfaced as a problem line.
        self.assertNotIn("KTP: belum sesuai", msg)

    def test_issues_truncation(self):
        issues = [
            Issue(id=f"i{n}", severity="medium", title=f"Catatan {n}", detail="d")
            for n in range(8)
        ]
        res = _result(issues=issues)
        msg = cs.render_score_message(res)
        self.assertIn("Catatan 0", msg)
        # 8 issues, max 5 shown → "…dan 3 catatan lain."
        self.assertIn("dan 3", msg)

    def test_registry_unavailable_note(self):
        res = _result(
            completeness=CompletenessSection(
                score=0.0, missing=[], required=[], note="SIAP DB tidak tersedia."
            )
        )
        msg = cs.render_score_message(res)
        self.assertIn("SIAP DB tidak tersedia.", msg)


class TestIsSubmissionReady(unittest.TestCase):
    def test_ready_when_high_and_no_critical(self):
        res = _result(overall=0.9, issues=[])
        self.assertTrue(cs.is_submission_ready(res))

    def test_not_ready_below_threshold(self):
        res = _result(overall=0.5, issues=[])
        self.assertFalse(cs.is_submission_ready(res))

    def test_not_ready_with_critical_even_if_high_score(self):
        res = _result(
            overall=0.95,
            issues=[Issue(id="c", severity="critical", title="Dokumen wajib hilang", detail="d")],
        )
        self.assertFalse(cs.is_submission_ready(res))


if __name__ == "__main__":
    unittest.main(verbosity=2)
