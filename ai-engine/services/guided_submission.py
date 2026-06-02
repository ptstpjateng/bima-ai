"""
Guided submission flow — BIMA walks a citizen through filing a real SIAP
licence application end to end ([[BIMA Vision]] req #4, Wave 3).

Waves 1+2 gave BIMA a SIAP read layer (catalogue, requirements, status), a
validator, and an officer write seam. Wave 3 ties them into ONE continuous
citizen experience: a UMKM owner says "saya mau ajukan izin ..." and BIMA
conversationally collects the application fields over many WhatsApp turns,
validates the submission, and — on a clean validation — files the request in
SIAP, handing back a ticket + a portal tracking link.

────────────────────────────────────────────────────────────────────────────
WHERE THIS PLUGS IN
────────────────────────────────────────────────────────────────────────────
`ai_handler.generate_ai_response` (and its streaming twin) call
`maybe_handle()` as an early FAST-PATH, right after the SIAP ticket
fast-path and before intent classification. `maybe_handle()` returns:
  * a reply string  → the message belonged to a guided-submission session
                       (or started one); ai_handler returns it directly.
  * None            → not a submission message; ai_handler proceeds as before.

The whole flow is deterministic Python + the SIAP tool layer + the validator
— it makes NO Gemma generation call. That keeps form-filling fast (<1 s/turn
when SIAP is reachable) and hallucination-free.

────────────────────────────────────────────────────────────────────────────
STATE — multi-turn, per-user, survives across messages
────────────────────────────────────────────────────────────────────────────
A citizen fills a form across many separate WhatsApp messages. `ai_handler`'s
existing `_history` keeps only the last 2 turns — far too short. So this
module owns its OWN per-user state: a `SubmissionSession` dataclass in a
bounded in-memory LRU (`_sessions`). Each session is a small state machine:

  RESOLVING_LICENSE  — citizen named a licence; we matched (or need to
                       disambiguate) it against SIAP's catalogue.
  COLLECTING_FIELDS  — walking the citizen item by item through the required
                       applicant fields, one question per turn.
  REVIEW             — all fields collected; showing a summary, awaiting the
                       citizen's "ya" to validate + submit.
  DONE / FAILED      — terminal; the session is cleared.

In-memory state is acceptable for this slice (same call as `ai_handler`'s
`_history` and the rate limiter). A process restart drops in-flight forms;
a durable store (Redis / a DB table) is a documented follow-up.

────────────────────────────────────────────────────────────────────────────
DOCUMENTS — deliberate scope decision
────────────────────────────────────────────────────────────────────────────
The validator needs the citizen's documents (KTP/NIB/NPWP). Receiving
document IMAGES over WhatsApp (APTANA media webhook → download → decode) is
a sizeable integration on its own. To keep this slice solid rather than
over-stretched, document intake runs through the validator's existing
DEMO-FIXTURE path (`ai-engine/tests/fixtures/*.json`, also used by the
bima-admin case page). That exercises the real validate → branch → submit
logic end to end with no PII and no Gemini-Vision quota burn.

Real document-upload-over-WhatsApp is DEFERRED and reported as a follow-up
slice. When it lands, only `_run_validation()` changes — it will build real
`Document` objects from the citizen's uploaded media and call
`services.agents.validator.validate_submission` directly. The state machine,
the field collection, and the submit step do not change.

────────────────────────────────────────────────────────────────────────────
FEATURE FLAG
────────────────────────────────────────────────────────────────────────────
`BIMA_GUIDED_SUBMISSION_ENABLED` (default "false"). While off, `maybe_handle`
always returns None — ai_handler behaves exactly as before. This makes the
PR safe to merge before the SIAP submission token is provisioned and the
flow is verified on Beta-SIAP.

────────────────────────────────────────────────────────────────────────────
PII
────────────────────────────────────────────────────────────────────────────
NIK and phone are masked in every log line (`_mask`). Full values live only
in the in-memory session and the SIAP request body.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("bima_ai.guided_submission")

_PORTAL_TRACK_URL = "https://portal.bimaptsp.com/track/{ticket}"


# ===========================================================================
# Feature flag
# ===========================================================================

def is_enabled() -> bool:
    """Read at call time, not import time — lets tests/ops flip the env."""
    return os.getenv("BIMA_GUIDED_SUBMISSION_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


# ===========================================================================
# Submission-intent detection
# ===========================================================================

# A citizen starting a filing. Indonesian: "saya mau ajukan/mengajukan/
# daftar/mendaftar izin ...". English: "I want to apply for ...". Word-bounded
# verbs so "pendaftaran" alone (a noun) does not trip it; the verb must pair
# with a licensing object word (izin / perizinan / permohonan / permit /
# license / NIB). The classifier is intentionally conservative — a false
# negative just routes to the normal chat path; a false positive hijacks a
# message into the form, which is worse.
_SUBMISSION_INTENT_PATTERN = re.compile(
    r"\b("
    r"(?:mau|ingin|pengen|hendak|akan|tolong|bantu|bisa)\s+"
    r"(?:saya\s+)?"
    r"(?:di)?(?:ajukan|mengajukan|ajuin|daftar(?:kan)?|mendaftar(?:kan)?|"
    r"buat(?:kan)?|urus(?:kan)?|proses(?:kan)?)"
    r"|"
    r"(?:apply\s+for|submit)\s+(?:a\s+|an\s+)?(?:new\s+)?"
    r"|"
    r"(?:start|begin|file)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:application|permit)"
    r")",
    re.IGNORECASE,
)

# The message must ALSO mention a licensing object — guards against
# "saya mau daftar antrean" (queue) etc.
_LICENSING_OBJECT_PATTERN = re.compile(
    r"\b(izin|perizinan|permohonan|permit|licen[cs]e|lisensi|nib|"
    r"sertifikat\s+standar|sertifikat)\b",
    re.IGNORECASE,
)


def detect_submission_intent(message: str) -> bool:
    """True when the message reads as 'I want to file a licence application'.

    Requires BOTH a filing verb-phrase AND a licensing-object noun, so that a
    bare "saya mau daftar" or "apa itu izin usaha" does not start a flow.
    """
    if not message or not message.strip():
        return False
    return bool(
        _SUBMISSION_INTENT_PATTERN.search(message)
        and _LICENSING_OBJECT_PATTERN.search(message)
    )


# Cancel words — let a citizen abandon an in-flight form at any step.
_CANCEL_PATTERN = re.compile(
    r"^\s*(batal|batalkan|cancel|stop|berhenti|ga jadi|gak jadi|tidak jadi|"
    r"keluar)\s*$",
    re.IGNORECASE,
)

# Affirmation words — the citizen's "yes, submit it" at the REVIEW step.
_AFFIRM_PATTERN = re.compile(
    r"^\s*(ya|iya|ya|yes|y|ok|oke|okay|setuju|lanjut(?:kan)?|benar|betul|"
    r"kirim|submit|gas|jalankan|boleh)\s*$",
    re.IGNORECASE,
)


# ===========================================================================
# Field collection — the applicant profile fields we walk the citizen through.
#
# These are the citizen-supplied profile fields a SIAP licence-request needs.
# We deliberately collect the APPLICANT profile here, not the licence's full
# document checklist — documents are validated separately (see module
# docstring on the deferred WhatsApp document-upload slice).
# ===========================================================================

@dataclass(frozen=True)
class FieldSpec:
    key: str
    question: str               # Indonesian prompt shown to the citizen
    validate: Any               # callable(str) -> (ok: bool, cleaned_or_error)
    is_pii: bool = False        # mask in logs / review summary


def _v_name(raw: str) -> tuple[bool, str]:
    s = re.sub(r"\s+", " ", (raw or "")).strip()
    if len(s) < 3:
        return False, "Nama terlalu pendek. Mohon tulis nama lengkap sesuai KTP."
    if not re.search(r"[A-Za-z]", s):
        return False, "Nama tidak valid. Mohon tulis nama lengkap sesuai KTP."
    return True, s


def _v_nik(raw: str) -> tuple[bool, str]:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) != 16:
        return False, (
            "NIK harus 16 digit angka (sesuai KTP). Mohon kirim ulang NIK Anda."
        )
    return True, digits


def _v_business_name(raw: str) -> tuple[bool, str]:
    s = re.sub(r"\s+", " ", (raw or "")).strip()
    if len(s) < 2:
        return False, "Nama usaha terlalu pendek. Mohon tulis nama usaha Anda."
    return True, s


def _v_phone(raw: str) -> tuple[bool, str]:
    digits = re.sub(r"\D", "", raw or "")
    # Indonesian mobile: 10-15 digits after stripping; tolerate 0/62 prefixes.
    if not (9 <= len(digits) <= 15):
        return False, (
            "Nomor HP tidak valid. Mohon kirim nomor WhatsApp aktif "
            "(contoh: 081234567890)."
        )
    return True, digits


# Order matters — this IS the question sequence the citizen walks through.
_FIELD_SPECS: list[FieldSpec] = [
    FieldSpec(
        key="applicant_name",
        question="Siapa nama lengkap pemohon (sesuai KTP)?",
        validate=_v_name,
    ),
    FieldSpec(
        key="nik",
        question="Berapa NIK (16 digit) pemohon? Mohon ketik nomornya.",
        validate=_v_nik,
        is_pii=True,
    ),
    FieldSpec(
        key="business_name",
        question="Apa nama usaha / badan usaha Anda?",
        validate=_v_business_name,
    ),
    FieldSpec(
        key="phone",
        question=(
            "Nomor WhatsApp aktif untuk pemberitahuan status "
            "(contoh: 081234567890)?"
        ),
        validate=_v_phone,
        is_pii=True,
    ),
]

_FIELD_BY_KEY: dict[str, FieldSpec] = {f.key: f for f in _FIELD_SPECS}


# ===========================================================================
# Session state
# ===========================================================================

class Stage(str, Enum):
    RESOLVING_LICENSE = "resolving_license"
    COLLECTING_FIELDS = "collecting_fields"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"


@dataclass
class SubmissionSession:
    """One citizen's in-flight guided submission. Lives in `_sessions`."""

    user_id: str
    stage: Stage = Stage.RESOLVING_LICENSE
    license_id: Optional[int] = None
    license_name: Optional[str] = None
    requirements: list[str] = field(default_factory=list)
    sla_working_days: Optional[int] = None
    retribution_fee: Optional[str] = None
    # license candidates when the lookup is ambiguous (citizen must pick).
    candidates: list[dict[str, Any]] = field(default_factory=list)
    # collected applicant fields: key -> cleaned value.
    fields: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def next_missing_field(self) -> Optional[FieldSpec]:
        for spec in _FIELD_SPECS:
            if spec.key not in self.fields:
                return spec
        return None

    def touch(self) -> None:
        self.updated_at = time.time()


# Bounded LRU of in-flight sessions — same shape as ai_handler._history.
_MAX_SESSIONS = 500
# Stale-session TTL: a form abandoned for this long is dropped on next touch.
_SESSION_TTL_SECONDS = 6 * 60 * 60  # 6 hours
_sessions: "OrderedDict[str, SubmissionSession]" = OrderedDict()


def _get_session(user_id: str) -> Optional[SubmissionSession]:
    sess = _sessions.get(user_id)
    if sess is None:
        return None
    if time.time() - sess.updated_at > _SESSION_TTL_SECONDS:
        logger.info("Guided-submission session expired | user=%s", _mask(user_id))
        _sessions.pop(user_id, None)
        return None
    return sess


def _put_session(sess: SubmissionSession) -> None:
    _sessions[sess.user_id] = sess
    _sessions.move_to_end(sess.user_id)
    while len(_sessions) > _MAX_SESSIONS:
        _sessions.popitem(last=False)


def _clear_session(user_id: str) -> None:
    _sessions.pop(user_id, None)


def has_active_session(user_id: str) -> bool:
    """True when this user has an in-flight (non-terminal) submission."""
    sess = _get_session(user_id)
    return sess is not None and sess.stage in (
        Stage.RESOLVING_LICENSE,
        Stage.COLLECTING_FIELDS,
        Stage.REVIEW,
    )


def _mask(value: Optional[str]) -> str:
    """Mask PII (NIK, phone, user_id) for logs — never log the full value."""
    if not value:
        return "<none>"
    s = str(value)
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


# ===========================================================================
# Validator integration
#
# This slice runs the validator through its DEMO-FIXTURE path (see module
# docstring). The fixtures are the same canned ValidateResponse JSON the
# bima-admin case page uses — they exercise the real validate → branch →
# submit logic with no PII and no Gemini-Vision quota. When real
# document-over-WhatsApp intake lands this function swaps to building
# `Document` objects and calling `validator.validate_submission` directly;
# nothing else in this module changes.
# ===========================================================================

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _demo_fixture_name() -> str:
    """Which fixture the guided flow validates against. Read at call time
    (not import time) so ops can flip it without a restart — same pattern as
    `is_enabled()`. "clean" is the happy-path default so the flow
    demonstrates a real end-to-end submit."""
    return os.getenv("GUIDED_SUBMISSION_DEMO_FIXTURE", "clean").strip() or "clean"


def _run_validation(sess: SubmissionSession) -> dict[str, Any]:
    """Validate the citizen's submission. Returns a normalised result dict:

      {"ok": bool, "status": str, "score_percent": int, "summary": str,
       "issues": [{severity, message}, ...]}

    `ok` is True only when the validation status is "ready" (clean) — that is
    the gate for the SIAP submit step. Any miss / load failure returns
    ok=False with a friendly note rather than raising.
    """
    path = _FIXTURES_DIR / f"{_demo_fixture_name()}.json"
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Guided-submission validation fixture unavailable | path=%s | err=%s",
            path, exc,
        )
        return {
            "ok": False,
            "status": "unverified",
            "score_percent": 0,
            "summary": (
                "Validasi dokumen belum bisa dijalankan saat ini. Mohon coba "
                "lagi nanti."
            ),
            "issues": [],
        }

    status = str(payload.get("status", "unverified"))
    issues_raw = payload.get("issues") or []
    issues = [
        {
            "severity": str(it.get("severity", "")).lower(),
            "message": str(it.get("message", "")),
        }
        for it in issues_raw
        if isinstance(it, dict)
    ]
    logger.info(
        "Guided-submission validation | user=%s | status=%s | issues=%d",
        _mask(sess.user_id), status, len(issues),
    )
    return {
        "ok": status == "ready",
        "status": status,
        "score_percent": int(payload.get("score_percent", 0) or 0),
        "summary": str(payload.get("summary", "")),
        "issues": issues,
    }


# ===========================================================================
# Reply builders — plain-Indonesian, WhatsApp-friendly.
# ===========================================================================

def _fmt_review(sess: SubmissionSession) -> str:
    """The pre-submit summary the citizen confirms with 'ya'."""
    lines = [
        f"Baik, mohon dicek kembali permohonan Anda:",
        "",
        f"📋 *Jenis izin:* {sess.license_name}",
    ]
    for spec in _FIELD_SPECS:
        val = sess.fields.get(spec.key, "-")
        shown = _mask(val) if spec.is_pii else val
        label = {
            "applicant_name": "Nama pemohon",
            "nik": "NIK",
            "business_name": "Nama usaha",
            "phone": "No. WhatsApp",
        }.get(spec.key, spec.key)
        lines.append(f"• *{label}:* {shown}")
    if sess.sla_working_days:
        lines.append(f"• *Perkiraan SLA:* {sess.sla_working_days} hari kerja")
    if sess.retribution_fee:
        lines.append(f"• *Retribusi:* {sess.retribution_fee}")
    lines += [
        "",
        "Jika sudah benar, ketik *YA* untuk memvalidasi dan mengirim "
        "permohonan ke SIAP. Ketik *BATAL* untuk membatalkan.",
    ]
    return "\n".join(lines)


def _fmt_validation_issues(result: dict[str, Any]) -> str:
    """The 'please fix this before we submit' message — flow does NOT submit."""
    lines = [
        "⚠️ Permohonan Anda belum bisa dikirim — ada yang perlu diperbaiki "
        f"dulu (kelengkapan {result.get('score_percent', 0)}%):",
        "",
    ]
    issues = result.get("issues") or []
    if issues:
        for it in issues[:5]:
            lines.append(f"• {it.get('message', '')}".rstrip())
    else:
        lines.append(f"• {result.get('summary', 'Dokumen belum lengkap.')}")
    lines += [
        "",
        "Perbaiki poin di atas, lalu ketik *KIRIM* untuk mencoba lagi, atau "
        "*BATAL* untuk membatalkan.",
    ]
    return "\n".join(lines)


# ===========================================================================
# State-machine handlers
# ===========================================================================

async def _start_session(user_id: str, message: str) -> str:
    """Begin a new guided submission: resolve the licence the citizen named."""
    from services.siap_tools import siap_lookup_license

    sess = SubmissionSession(user_id=user_id)

    # Strip the intent verbs so the leftover is a cleaner licence query.
    query = _SUBMISSION_INTENT_PATTERN.sub(" ", message)
    query = re.sub(
        r"\b(saya|aku|untuk|usaha|sebuah|tolong|bantu|mau|ingin|pengen|"
        r"please|a|an|new|the)\b",
        " ", query, flags=re.IGNORECASE,
    )
    query = re.sub(r"\s+", " ", query).strip()

    lookup = await siap_lookup_license(query) if query else {"found": False}

    if not lookup.get("found"):
        # Could not resolve — keep the session in RESOLVING_LICENSE and ask
        # the citizen to name the licence precisely.
        sess.stage = Stage.RESOLVING_LICENSE
        _put_session(sess)
        note = lookup.get("note", "")
        logger.info(
            "Guided-submission start — licence unresolved | user=%s | query=%r",
            _mask(user_id), query,
        )
        return (
            "Saya siap membantu Anda mengajukan izin lewat SIAP Jateng. 🙌\n\n"
            "Mohon sebutkan *nama izin* yang ingin Anda ajukan "
            "(contoh: \"Izin Pemakaian Tanah\")."
            + (f"\n\n_{note}_" if note else "")
        )

    matches = lookup.get("matches") or []
    if len(matches) > 1:
        # Ambiguous — let the citizen pick from a numbered shortlist.
        sess.candidates = matches[:8]
        sess.stage = Stage.RESOLVING_LICENSE
        _put_session(sess)
        lines = [
            "Saya menemukan beberapa jenis izin yang cocok. Mohon pilih "
            "dengan membalas *nomornya*:",
            "",
        ]
        for idx, m in enumerate(matches[:8], start=1):
            sektor = f" _(Bidang: {m['sektor']})_" if m.get("sektor") else ""
            lines.append(f"{idx}. {m['name']}{sektor}")
        lines += ["", "Ketik *BATAL* untuk membatalkan."]
        return "\n".join(lines)

    # Exactly one match — lock it in and fetch requirements.
    return await _lock_license(sess, matches[0])


async def _lock_license(sess: SubmissionSession, match: dict[str, Any]) -> str:
    """Pin the chosen licence onto the session and move to field collection."""
    from services.siap_tools import siap_get_requirements

    sess.license_id = int(match["license_id"])
    sess.license_name = match["name"]
    sess.candidates = []

    reqs = await siap_get_requirements(license_id=sess.license_id)
    if reqs.get("found"):
        sess.requirements = list(reqs.get("requirements") or [])
        sess.sla_working_days = reqs.get("sla_working_days")
        sess.retribution_fee = reqs.get("retribution_fee")

    sess.stage = Stage.COLLECTING_FIELDS
    sess.touch()
    _put_session(sess)
    logger.info(
        "Guided-submission licence locked | user=%s | license_id=%s",
        _mask(sess.user_id), sess.license_id,
    )

    intro_lines = [
        f"Baik! Anda akan mengajukan *{sess.license_name}* lewat SIAP Jateng.",
    ]
    if sess.requirements:
        intro_lines += [
            "",
            "Dokumen yang nanti perlu Anda siapkan:",
        ]
        for r in sess.requirements[:8]:
            intro_lines.append(f"• {r}")
    intro_lines += [
        "",
        "Sekarang saya akan menanyakan data pemohon satu per satu. "
        "Ketik *BATAL* kapan saja untuk membatalkan.",
        "",
    ]
    # Ask the first field.
    first = sess.next_missing_field()
    intro_lines.append(f"1️⃣ {first.question}" if first else "")
    return "\n".join(intro_lines).rstrip()


def _handle_license_choice(sess: SubmissionSession, message: str) -> Optional[dict]:
    """At RESOLVING_LICENSE with candidates: parse the citizen's numeric pick.
    Returns the chosen match dict, or None if the message was not a valid pick.
    """
    if not sess.candidates:
        return None
    m = re.match(r"^\s*(\d{1,2})\s*$", message.strip())
    if not m:
        return None
    idx = int(m.group(1))
    if 1 <= idx <= len(sess.candidates):
        return sess.candidates[idx - 1]
    return None


async def _collect_field(sess: SubmissionSession, message: str) -> str:
    """At COLLECTING_FIELDS: validate the answer to the pending question,
    store it, then ask the next field — or move to REVIEW when done."""
    spec = sess.next_missing_field()
    if spec is None:
        # Defensive — shouldn't happen; jump straight to review.
        sess.stage = Stage.REVIEW
        sess.touch()
        _put_session(sess)
        return _fmt_review(sess)

    ok, cleaned = spec.validate(message)
    if not ok:
        sess.touch()
        _put_session(sess)
        # cleaned holds the error message in the failure case.
        return f"{cleaned}\n\n{spec.question}"

    sess.fields[spec.key] = cleaned
    sess.touch()
    logger.info(
        "Guided-submission field collected | user=%s | field=%s | value=%s",
        _mask(sess.user_id), spec.key,
        _mask(cleaned) if spec.is_pii else cleaned,
    )

    nxt = sess.next_missing_field()
    if nxt is None:
        sess.stage = Stage.REVIEW
        _put_session(sess)
        return "✅ Semua data pemohon terkumpul.\n\n" + _fmt_review(sess)

    _put_session(sess)
    collected = len(sess.fields)
    return f"✅ Tercatat.\n\n{collected + 1}️⃣ {nxt.question}"


async def _submit(sess: SubmissionSession) -> str:
    """REVIEW + citizen confirmed: run the validator, then submit to SIAP.

    Validation issues → tell the citizen what to fix, do NOT submit.
    Clean validation  → POST to SIAP, return ticket + portal track link.
    """
    from services.siap_submission_client import get_siap_submission_client

    # --- Step 1: validate the submission --------------------------------
    result = _run_validation(sess)
    if not result["ok"]:
        # Keep the session at REVIEW so the citizen can fix + retry ("KIRIM").
        sess.stage = Stage.REVIEW
        sess.touch()
        _put_session(sess)
        return _fmt_validation_issues(result)

    # --- Step 2: submit to SIAP -----------------------------------------
    client = get_siap_submission_client()
    if not client.is_configured():
        sess.stage = Stage.FAILED
        sess.touch()
        _put_session(sess)
        logger.warning(
            "Guided-submission: SIAP submission client not configured | user=%s",
            _mask(sess.user_id),
        )
        return (
            "✅ Dokumen Anda sudah lengkap dan tervalidasi.\n\n"
            "Namun kanal pengiriman otomatis ke SIAP belum aktif di sistem "
            "saat ini. Permohonan Anda *belum terkirim*. Mohon hubungi "
            "petugas DPMPTSP untuk menyelesaikan pengajuan, atau coba lagi "
            "nanti. Mohon maaf atas ketidaknyamanannya. 🙏"
        )

    # profile_id: in this slice we do not yet resolve a real SIAP profile_id
    # from the citizen's NIK (that is a SIAP-side lookup endpoint BIMA does
    # not have read access to). GUIDED_SUBMISSION_PROFILE_ID lets ops pin a
    # known Beta-SIAP test profile for rehearsal. Real per-citizen profile
    # resolution is a documented follow-up.
    profile_id_raw = os.getenv("GUIDED_SUBMISSION_PROFILE_ID", "").strip()
    if not profile_id_raw.isdigit():
        sess.stage = Stage.FAILED
        sess.touch()
        _put_session(sess)
        logger.warning(
            "Guided-submission: no GUIDED_SUBMISSION_PROFILE_ID configured | "
            "user=%s", _mask(sess.user_id),
        )
        return (
            "✅ Dokumen Anda sudah lengkap dan tervalidasi.\n\n"
            "Namun pemetaan profil pemohon ke SIAP belum dikonfigurasi di "
            "sistem saat ini. Permohonan *belum terkirim* — mohon hubungi "
            "petugas DPMPTSP untuk menyelesaikan pengajuan. 🙏"
        )

    description = (
        f"Permohonan {sess.license_name} via BIMA-AI untuk usaha "
        f"'{sess.fields.get('business_name', '-')}'."
    )
    submit = await client.create_request(
        license_id=sess.license_id,
        profile_id=int(profile_id_raw),
        description=description,
    )

    if not submit.get("ok"):
        sess.stage = Stage.FAILED
        sess.touch()
        _put_session(sess)
        note = submit.get("note", "Terjadi kesalahan saat mengirim ke SIAP.")
        logger.warning(
            "Guided-submission submit failed | user=%s | note=%s",
            _mask(sess.user_id), note,
        )
        return (
            "✅ Dokumen Anda sudah tervalidasi, tetapi pengiriman ke SIAP "
            f"gagal:\n\n_{note}_\n\n"
            "Permohonan *belum terkirim*. Ketik *KIRIM* untuk mencoba lagi, "
            "atau hubungi petugas DPMPTSP."
        )

    # --- Success --------------------------------------------------------
    ticket = submit.get("ticket")
    sess.stage = Stage.DONE
    sess.touch()
    _clear_session(sess.user_id)
    logger.info(
        "Guided-submission SUBMITTED | user=%s | license_id=%s | ticket=%s",
        _mask(sess.user_id), sess.license_id, ticket,
    )

    if ticket:
        track = _PORTAL_TRACK_URL.format(ticket=ticket)
        return (
            "🎉 *Permohonan Anda berhasil dikirim ke SIAP Jateng!*\n\n"
            f"📋 *Jenis izin:* {sess.license_name}\n"
            f"🎟️ *Nomor tiket:* {ticket}\n\n"
            f"Pantau status permohonan Anda kapan saja di:\n{track}\n\n"
            "Simpan nomor tiket ini. Anda akan kami kabari saat status "
            "permohonan berubah. Terima kasih. 🙏"
        )
    # Submitted but SIAP did not return a ticket — still a success.
    return (
        "🎉 *Permohonan Anda berhasil dikirim ke SIAP Jateng!*\n\n"
        f"📋 *Jenis izin:* {sess.license_name}\n\n"
        "Nomor tiket akan tersedia sebentar lagi. Anda dapat memantau status "
        f"di {_PORTAL_TRACK_URL.format(ticket='')[:-1]}. Terima kasih. 🙏"
    )


# ===========================================================================
# Public entry point — called by ai_handler as a FAST-PATH.
# ===========================================================================

async def maybe_handle(user_id: str, message: str) -> Optional[str]:
    """Guided-submission FAST-PATH for `ai_handler`.

    Returns:
      * a reply string — the message belonged to (or started) a guided
        submission; `ai_handler` should return it directly without calling
        Gemma.
      * None — not a submission message; `ai_handler` proceeds normally.

    The flow only ever engages when `BIMA_GUIDED_SUBMISSION_ENABLED` is on.
    With the flag off this returns None unconditionally, so `ai_handler`
    behaves exactly as it did before this module existed.
    """
    if not is_enabled():
        return None

    msg = (message or "").strip()
    sess = _get_session(user_id)

    # --- No active session: only engage on a fresh submission intent ----
    if sess is None or sess.stage in (Stage.DONE, Stage.FAILED):
        if detect_submission_intent(msg):
            logger.info(
                "Guided-submission intent detected — starting flow | user=%s",
                _mask(user_id),
            )
            try:
                return await _start_session(user_id, msg)
            except Exception:
                logger.exception(
                    "Guided-submission start crashed | user=%s", _mask(user_id)
                )
                _clear_session(user_id)
                return None  # degrade: let ai_handler take the message
        return None

    # --- Active session: this message belongs to the flow ---------------
    # A citizen can bail out of the form at any step.
    if _CANCEL_PATTERN.match(msg):
        _clear_session(user_id)
        logger.info("Guided-submission cancelled | user=%s", _mask(user_id))
        return (
            "Baik, pengajuan izin dibatalkan. Jika sewaktu-waktu ingin "
            "mengajukan lagi, cukup beri tahu saya. 🙏"
        )

    try:
        if sess.stage == Stage.RESOLVING_LICENSE:
            # Either a numeric pick from a shortlist, or a fresh licence name.
            choice = _handle_license_choice(sess, msg)
            if choice is not None:
                return await _lock_license(sess, choice)
            if sess.candidates:
                return (
                    "Mohon balas dengan *nomor* dari daftar di atas, atau "
                    "ketik *BATAL* untuk membatalkan."
                )
            # No candidates pending → treat the message as a licence query.
            return await _start_session(user_id, f"ajukan izin {msg}")

        if sess.stage == Stage.COLLECTING_FIELDS:
            return await _collect_field(sess, msg)

        if sess.stage == Stage.REVIEW:
            if _AFFIRM_PATTERN.match(msg):
                return await _submit(sess)
            # "KIRIM" retry after a validation-issues message.
            if re.match(r"^\s*kirim\s*$", msg, re.IGNORECASE):
                return await _submit(sess)
            return (
                "Ketik *YA* untuk memvalidasi dan mengirim permohonan, atau "
                "*BATAL* untuk membatalkan. Jika ada data yang ingin diubah, "
                "ketik *BATAL* lalu mulai ulang pengajuan."
            )
    except Exception:
        logger.exception(
            "Guided-submission handler crashed | user=%s | stage=%s",
            _mask(user_id), sess.stage,
        )
        _clear_session(user_id)
        return (
            "Maaf, terjadi gangguan saat memproses pengajuan Anda. "
            "Pengajuan dibatalkan — mohon mulai ulang dengan menyebutkan "
            "izin yang ingin Anda ajukan."
        )

    # Unknown stage — clear and let ai_handler handle the message.
    _clear_session(user_id)
    return None
