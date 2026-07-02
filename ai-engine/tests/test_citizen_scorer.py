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
import re
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
        self.assertIn("ktp.jpg", msg)      # mismatch still surfaces the file
        self.assertIn("dilabeli", msg)     # new mismatch phrasing (was "terbaca sebagai")
        self.assertIn("NPWP", msg)         # detected type shown
        self.assertIn("NIB", msg)          # the matched doc's detected type

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


class TestNameAnyDocument(unittest.TestCase):
    """Open-vocabulary naming — an 'Other' doc that Vision could still title
    renders that free-text title (masked) instead of the flat 'Dokumen lain'."""

    def test_other_with_document_name_shows_name(self):
        res = _result(
            type_correctness=[
                TypeCorrectnessFinding(
                    file="scan.pdf",
                    file_id="doc-1",
                    claimed_type="Other",
                    detected_type="Other",
                    confidence=0.6,
                    matches=False,
                    document_name="Nota Kesepahaman",
                ),
            ]
        )
        msg = cs.render_score_message(res)
        self.assertIn("Nota Kesepahaman", msg)
        self.assertNotIn("Dokumen lain", msg)

    def test_other_without_document_name_falls_back_to_label(self):
        res = _result(
            type_correctness=[
                TypeCorrectnessFinding(
                    file="scan.pdf",
                    file_id="doc-1",
                    claimed_type="Other",
                    detected_type="Other",
                    confidence=0.6,
                    matches=False,
                    document_name=None,
                ),
            ]
        )
        msg = cs.render_score_message(res)
        self.assertIn("Dokumen lain", msg)

    def test_known_class_ignores_document_name(self):
        # A real enum class still leads with its human label, not the free text.
        res = _result(
            type_correctness=[
                TypeCorrectnessFinding(
                    file="ktp.jpg",
                    file_id="doc-1",
                    claimed_type="KTP",
                    detected_type="KTP",
                    confidence=0.95,
                    matches=True,
                    document_name="KARTU TANDA PENDUDUK Republik Indonesia",
                ),
            ]
        )
        msg = cs.render_score_message(res)
        self.assertIn("KTP", msg)

    def test_document_name_is_pii_masked(self):
        # document_name can echo a business/person name AND embedded PII — a
        # 16-digit NIK riding in the title must be masked before display.
        res = _result(
            type_correctness=[
                TypeCorrectnessFinding(
                    file="scan.pdf",
                    file_id="doc-1",
                    claimed_type="Other",
                    detected_type="Other",
                    confidence=0.5,
                    matches=False,
                    document_name="Surat 3327080511740081",
                ),
            ]
        )
        msg = cs.render_score_message(res)
        self.assertNotIn("3327080511740081", msg)

    def test_new_sign_signals_surface_when_missing(self):
        # A Surat Pesanan missing signature + stamp shows the compact flags.
        res = _result(
            type_correctness=[
                TypeCorrectnessFinding(
                    file="pesanan.pdf",
                    file_id="doc-1",
                    claimed_type="Surat_Pesanan",
                    detected_type="Surat_Pesanan",
                    confidence=0.9,
                    matches=True,
                    has_signature=False,
                    has_stamp=False,
                ),
            ]
        )
        msg = cs.render_score_message(res)
        self.assertIn("Surat Pesanan", msg)
        self.assertIn("tanda tangan belum terlihat", msg)
        self.assertIn("cap belum terlihat", msg)


class TestDedupChecklist(unittest.TestCase):
    """Re-uploads of the SAME document class collapse to ONE checklist line —
    no class appears twice in 'Dokumen yang terbaca'."""

    def test_duplicate_ktp_collapses_to_one_line(self):
        res = _result(
            type_correctness=[
                TypeCorrectnessFinding(
                    file="ktp1.jpg", file_id="d1", claimed_type="KTP",
                    detected_type="KTP", confidence=0.9, matches=True,
                ),
                TypeCorrectnessFinding(
                    file="ktp2.jpg", file_id="d2", claimed_type="KTP",
                    detected_type="KTP", confidence=0.95, matches=True,
                ),
                TypeCorrectnessFinding(
                    file="ktp3.jpg", file_id="d3", claimed_type="KTP",
                    detected_type="KTP", confidence=0.8, matches=True,
                ),
            ]
        )
        msg = cs.render_score_message(res)
        # The type block shows the KTP class exactly once.
        block = msg.split("*Dokumen yang terbaca:*", 1)[-1]
        self.assertEqual(block.count("*KTP*"), 1)

    def test_dedup_keeps_match_over_later_nonmatch(self):
        # A good KTP read followed by a bad one of the same class → the good
        # (match) read is kept, so the checklist doesn't show a spurious conflict.
        findings = [
            TypeCorrectnessFinding(
                file="ktp_good.jpg", file_id="d1", claimed_type="KTP",
                detected_type="KTP", confidence=0.95, matches=True,
            ),
            TypeCorrectnessFinding(
                file="ktp_bad.jpg", file_id="d2", claimed_type="Surat_Domisili",
                detected_type="KTP", confidence=0.6, matches=False,
            ),
        ]
        deduped = cs._dedup_type_findings(findings)
        self.assertEqual(len(deduped), 1)
        self.assertTrue(deduped[0].matches)

    def test_dedup_never_collapses_unknown_or_other(self):
        # Two 'Other' docs and two 'Unknown' docs are distinct attachments —
        # they must NOT be collapsed together.
        findings = [
            TypeCorrectnessFinding(file="a.pdf", file_id="d1", claimed_type="Other",
                                   detected_type="Other", confidence=0.5, matches=False),
            TypeCorrectnessFinding(file="b.pdf", file_id="d2", claimed_type="Other",
                                   detected_type="Other", confidence=0.5, matches=False),
            TypeCorrectnessFinding(file="c.pdf", file_id="d3", claimed_type="KTP",
                                   detected_type="Unknown", confidence=0.1, matches=False),
            TypeCorrectnessFinding(file="d.pdf", file_id="d4", claimed_type="KTP",
                                   detected_type="Unknown", confidence=0.1, matches=False),
        ]
        deduped = cs._dedup_type_findings(findings)
        self.assertEqual(len(deduped), 4)

    def test_distinct_classes_all_shown(self):
        res = _result(
            type_correctness=[
                TypeCorrectnessFinding(file="ktp.jpg", file_id="d1", claimed_type="KTP",
                                       detected_type="KTP", confidence=0.9, matches=True),
                TypeCorrectnessFinding(file="nib.pdf", file_id="d2", claimed_type="NIB",
                                       detected_type="NIB", confidence=0.9, matches=True),
            ]
        )
        msg = cs.render_score_message(res)
        self.assertIn("*KTP*", msg)
        self.assertIn("*NIB*", msg)


class TestMaskPiiUnit(unittest.TestCase):
    """Direct unit cover for the reusable `services.pii.mask_pii` helper."""

    def setUp(self):
        from services.pii import mask_pii
        self.mask = mask_pii

    def test_16_digit_nik_masked_but_short_id_not(self):
        # 16-digit run → masked; a 9-digit licence/ticket number → untouched.
        self.assertNotIn("3327080511740081", self.mask("NIK : 3327080511740081"))
        self.assertEqual(self.mask("tiket 000123456 ok"), "tiket 000123456 ok")
        # 15- and 17-digit runs are NOT NIKs → left alone.
        self.assertEqual(self.mask("332708051174008"), "332708051174008")
        self.assertEqual(self.mask("33270805117400812"), "33270805117400812")

    def test_grouped_nik_masked(self):
        for grouped in ("3327 0805 1174 0081", "3327.0805.1174.0081"):
            self.assertNotIn("3327080511740081", self.mask(grouped))
            self.assertIn("33", self.mask(grouped))

    def test_phone_shapes_masked(self):
        self.assertNotIn("085117557091", self.mask("085117557091"))
        self.assertNotIn("6285117557091", self.mask("+6285117557091"))
        self.assertNotIn("6285117557091", self.mask("wa 6285117557091"))

    def test_already_masked_review_block_is_noop(self):
        # The existing review block ("33******81" / "08****12") must not get
        # double-masked into something awkward.
        self.assertEqual(self.mask("33******81"), "33******81")
        self.assertEqual(self.mask("08****12"), "08****12")

    def test_idempotent(self):
        once = self.mask("NIK : 3327080511740081 telp 085117557091")
        self.assertEqual(self.mask(once), once)

    def test_empty_and_none_safe(self):
        self.assertEqual(self.mask(""), "")
        self.assertIsNone(self.mask(None))


class TestEvidencePiiMasked(unittest.TestCase):
    """PII leak fix — a SuitabilityFinding's `evidence` is a Gemini Vision quote
    of the document and CAN contain a raw 16-digit NIK or a phone number. The
    citizen score message MUST render it masked (matching the existing
    "33******81" review block), never the full value."""

    _NIK = "3327080511740081"   # 16 digits — a real KTP NIK shape
    _PHONE = "085117557091"     # Indonesian mobile

    def test_nik_in_evidence_is_masked(self):
        res = _result(
            suitability=[
                SuitabilityFinding(
                    requirement="KTP sesuai pemohon",
                    file="ktp.jpg",
                    file_id="doc-1",
                    judgement="mismatch",
                    evidence=(
                        "PROVINSI JAWA TENGAH KABUPATEN PEMALANG "
                        f"NIK : {self._NIK} nama tidak cocok"
                    ),
                    confidence=0.8,
                ),
            ]
        )
        msg = cs.render_score_message(res)
        # The full NIK must NOT appear anywhere in the citizen message.
        self.assertNotIn(self._NIK, msg)
        # The masked form (first 2 + last 2, middle starred) IS present.
        self.assertIn("33", msg)
        self.assertIn("************81", msg)
        # Surrounding non-PII context is preserved.
        self.assertIn("KABUPATEN PEMALANG", msg)

    def test_phone_in_evidence_is_masked(self):
        res = _result(
            suitability=[
                SuitabilityFinding(
                    requirement="Surat Domisili",
                    file="domisili.pdf",
                    file_id="doc-2",
                    judgement="partial",
                    evidence=f"narahubung {self._PHONE} tertera di surat",
                    confidence=0.6,
                ),
            ]
        )
        msg = cs.render_score_message(res)
        self.assertNotIn(self._PHONE, msg)
        # Exact masked token: 08 + middle starred + last 2 digits (91).
        self.assertIn("08********91", msg)

    def test_non_pii_evidence_unchanged(self):
        res = _result(
            suitability=[
                SuitabilityFinding(
                    requirement="Alamat usaha",
                    file="d.pdf",
                    file_id="doc-3",
                    judgement="mismatch",
                    evidence="alamat usaha jelas",
                    confidence=0.7,
                ),
            ]
        )
        msg = cs.render_score_message(res)
        # The evidence is echoed verbatim (the surrounding "*...*" are WhatsApp
        # bold markers, not masking) — assert the exact evidence line is intact.
        self.assertIn("belum sesuai — alamat usaha jelas", msg)

    def test_short_id_not_masked(self):
        # A 9-digit ticket / licence number embedded in evidence must survive
        # untouched — only 16-digit NIK runs and phone-shaped runs are targeted.
        res = _result(
            suitability=[
                SuitabilityFinding(
                    requirement="Nomor berkas",
                    file="d.pdf",
                    file_id="doc-4",
                    judgement="partial",
                    evidence="nomor tiket 000123456 tercatat",
                    confidence=0.7,
                ),
            ]
        )
        msg = cs.render_score_message(res)
        self.assertIn("000123456", msg)


class TestNoEmojiInScoreMessage(unittest.TestCase):
    """FIX B — the rendered score message carries no emoji (plain-text status
    bands + bullets only)."""

    _EMOJI = re.compile(
        "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
        "\U0001F1E6-\U0001F1FF\U0000FE0F\U000020E3\U00002190-\U000021FF]"
    )

    def _assert_clean(self, text):
        self.assertEqual(self._EMOJI.findall(text), [], f"emoji in: {text!r}")

    def test_full_message_is_emoji_free(self):
        res = _result(
            overall=0.58,
            completeness=CompletenessSection(
                score=0.5, missing=["NPWP"], required=["KTP", "NIB", "NPWP"]
            ),
            type_correctness=[
                TypeCorrectnessFinding(file="ktp.jpg", file_id="d1",
                                       claimed_type="KTP", detected_type="NPWP",
                                       confidence=0.9, matches=False),
                TypeCorrectnessFinding(file="nib.pdf", file_id="d2",
                                       claimed_type="NIB", detected_type="NIB",
                                       confidence=0.95, matches=True),
                TypeCorrectnessFinding(file="x.pdf", file_id="d3",
                                       claimed_type="KTP", detected_type="Unknown",
                                       confidence=0.1, matches=False),
            ],
            suitability=[
                SuitabilityFinding(requirement="Surat materai", file="s.pdf",
                                   file_id="d4", judgement="mismatch",
                                   evidence="tidak ada materai", confidence=0.8),
                SuitabilityFinding(requirement="Proposal", file="p.pdf",
                                   file_id="d5", judgement="partial",
                                   evidence="kurang", confidence=0.6),
            ],
            issues=[
                Issue(id="c", severity="critical", title="Dokumen wajib hilang", detail="d"),
                Issue(id="h", severity="high", title="Materai hilang", detail="d"),
                Issue(id="m", severity="medium", title="Foto buram", detail="d"),
            ],
        )
        msg = cs.render_score_message(res, license_name="Izin Penelitian")
        self._assert_clean(msg)
        # Plain-text band word is present instead of a coloured circle.
        self.assertIn("Perlu diperbaiki", msg)

    def test_high_score_band_is_plain_text(self):
        msg = cs.render_score_message(_result(overall=0.92))
        self._assert_clean(msg)
        self.assertIn("Lengkap", msg)


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
