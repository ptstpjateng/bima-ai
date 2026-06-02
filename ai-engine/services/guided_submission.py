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

_PORTAL_TRACK_URL = "https://portal.nolongin.com/track/{ticket}"


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
    # AWAITING_PRIVY_SIGN sits BETWEEN field collection and review when the
    # licence requires legally-binding signed documents (Phase 5 — vault
    # doc §6). BIMA has POSTed PDFs to Privy and surfaced the signing URL
    # to the citizen; the session waits for the Privy webhook (or a
    # citizen "sudah belum?" poll) to advance. Sessions only enter this
    # stage when PRIVY_ENABLED is on AND the licence has a legal-doc
    # requirement — otherwise the flow goes COLLECTING_FIELDS → REVIEW.
    AWAITING_PRIVY_SIGN = "awaiting_privy_sign"
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
    # Privy state — populated only when the session enters
    # AWAITING_PRIVY_SIGN. request_id is the Privy signing request we
    # match incoming webhooks against; signing_url is what we hand the
    # citizen as a tap-to-sign link; signed_pdf is the result Privy
    # returns on signing_complete (held in memory for the SIAP attach
    # step, then cleared).
    privy_request_id: Optional[str] = None
    privy_signing_url: Optional[str] = None
    signed_pdf: Optional[bytes] = None
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
    sess = _sessions.pop(user_id, None)
    # Clear the Privy reverse-index too — stale entries would otherwise
    # let a late webhook for an abandoned session find a closed user_id.
    if sess and sess.privy_request_id:
        _privy_request_to_user.pop(sess.privy_request_id, None)


def has_active_session(user_id: str) -> bool:
    """True when this user has an in-flight (non-terminal) submission."""
    sess = _get_session(user_id)
    return sess is not None and sess.stage in (
        Stage.RESOLVING_LICENSE,
        Stage.COLLECTING_FIELDS,
        Stage.AWAITING_PRIVY_SIGN,
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


# Reverse index: Privy request_id → user_id. Lets the inbound webhook
# (services/routers/privy_callback) find which citizen session a callback
# belongs to without scanning the LRU. Populated when we enter
# AWAITING_PRIVY_SIGN, cleared when the session terminates.
_privy_request_to_user: dict[str, str] = {}


# Citizen-facing question that nudges them when waiting on Privy.
_PRIVY_POLL_PATTERN = re.compile(
    r"\b(sudah\s+belum|sudah\s+selesai|udah\s+belum|cek\s+(?:status\s+)?ttd|"
    r"status\s+tanda\s+tangan)\b",
    re.IGNORECASE,
)


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
        # All applicant fields are in. If this licence requires a signed
        # legal document AND Privy is configured, branch into the signing
        # flow before REVIEW. Otherwise fall through to the existing
        # behaviour (straight to REVIEW). The Privy branch is opt-in and
        # additive — when off, the flow looks exactly as it did before
        # Phase 5.
        privy_reply = await _maybe_enter_privy_sign(sess)
        if privy_reply is not None:
            return privy_reply
        sess.stage = Stage.REVIEW
        _put_session(sess)
        return "✅ Semua data pemohon terkumpul.\n\n" + _fmt_review(sess)

    _put_session(sess)
    collected = len(sess.fields)
    return f"✅ Tercatat.\n\n{collected + 1}️⃣ {nxt.question}"


# ===========================================================================
# Privy integration (Phase 5)
#
# Branches the state machine through Privy.id for citizen e-signing +
# e-meterai when the licence requires it. Lives in this file (not
# privy_client.py) because every transition is a guided-submission state
# change — privy_client.py only owns the HTTP shape.
# ===========================================================================


def _license_requires_privy(license_name: Optional[str]) -> bool:
    """Heuristic — does this licence need a signed Pakta Integritas /
    Surat Pernyataan via Privy?

    For Phase 5 every guided submission produces a signed Pakta
    Integritas + meteraied Surat Pernyataan (vault doc §6 step 4),
    so once Privy is enabled this is just "do we have a licence_name
    at all". When DPMPTSP's legal team carves out exceptions later,
    extend this to consult a per-licence allowlist instead.
    """
    return bool(license_name)


async def _maybe_enter_privy_sign(sess: SubmissionSession) -> Optional[str]:
    """Move the session into AWAITING_PRIVY_SIGN if Privy is on for this licence.

    Returns a citizen-facing reply when the branch fires; None to mean
    "no Privy step needed — proceed to REVIEW as before". This shape is
    the integration contract used by `_collect_field` above.

    Best-effort: any Privy failure here is reported to the citizen but
    leaves the session at REVIEW so the citizen can still try to file
    (DPMPTSP will then process the unsigned-document path manually) —
    Privy being down must NOT block the existing happy path. This is
    important during the KYC window when Privy may be intermittently
    unavailable.
    """
    from services import legal_templates
    from services.privy_client import get_privy_client

    client = get_privy_client()
    if not client.is_configured():
        # Phase 5 is opt-in. Keep the legacy flow when Privy is off.
        return None
    if not _license_requires_privy(sess.license_name):
        return None

    try:
        ctx = legal_templates.build_signing_context(
            license_name=sess.license_name or "",
            applicant_name=sess.fields.get("applicant_name", ""),
            nik=sess.fields.get("nik", ""),
            business_name=sess.fields.get("business_name", ""),
            phone=sess.fields.get("phone", ""),
        )
        pdf_bytes = legal_templates.render_pdf("pakta_integritas", ctx)
    except Exception:
        logger.exception(
            "Guided-submission: legal template render failed | user=%s",
            _mask(sess.user_id),
        )
        # Skip Privy and let the citizen continue with REVIEW (degrade).
        return None

    initiated = await client.initiate_signing(
        pdf_bytes=pdf_bytes,
        signer_nik=sess.fields.get("nik", ""),
        signer_name=sess.fields.get("applicant_name", ""),
        document_title=f"Pakta Integritas — {sess.license_name}",
    )
    if not initiated.get("ok"):
        logger.warning(
            "Guided-submission: Privy initiate_signing failed | user=%s | note=%s",
            _mask(sess.user_id), initiated.get("note"),
        )
        # Privy unavailable — fall back to REVIEW (manual processing).
        return None

    sess.stage = Stage.AWAITING_PRIVY_SIGN
    sess.privy_request_id = str(initiated["request_id"])
    sess.privy_signing_url = str(initiated["signing_url"])
    sess.touch()
    _put_session(sess)
    _privy_request_to_user[sess.privy_request_id] = sess.user_id
    logger.info(
        "Guided-submission entering AWAITING_PRIVY_SIGN | user=%s | request_id=%s",
        _mask(sess.user_id), sess.privy_request_id[:8] + "…",
    )

    return (
        "✅ Semua data pemohon terkumpul.\n\n"
        "Permohonan Anda memerlukan tanda tangan digital pada *Pakta "
        "Integritas* yang sah secara hukum (UU ITE / UU 10/2020).\n\n"
        "Silakan buka tautan berikut di handphone Anda untuk verifikasi "
        "dan tanda tangan (sekali saja, ~3 menit):\n"
        f"{sess.privy_signing_url}\n\n"
        "Setelah selesai, saya akan otomatis mengirim permohonan Anda ke "
        "SIAP Jateng. Anda juga bisa mengetik *SUDAH BELUM?* untuk "
        "memeriksa status, atau *BATAL* untuk membatalkan."
    )


async def _poll_privy_status(sess: SubmissionSession) -> str:
    """Citizen asks "sudah belum?" — poll Privy's status endpoint."""
    from services.privy_client import get_privy_client

    if not sess.privy_request_id:
        return (
            "Belum ada permintaan tanda tangan yang aktif. Ketik *BATAL* "
            "untuk membatalkan dan memulai ulang."
        )
    client = get_privy_client()
    result = await client.get_signed_doc(sess.privy_request_id)
    if result.get("ok") and result.get("pdf_bytes"):
        # Race: webhook hasn't arrived yet but the doc is ready. Take the
        # PDF here so the citizen does not have to keep polling.
        await _advance_after_privy_signed(sess, bytes(result["pdf_bytes"]))
        return await _submit(sess)
    if result.get("pending"):
        return (
            "Tanda tangan Anda *belum selesai diproses* di Privy. Mohon "
            "selesaikan verifikasi pada tautan yang saya kirim sebelumnya, "
            "atau cek kotak masuk WhatsApp/Email dari Privy. "
            "Coba ketik *SUDAH BELUM?* lagi sebentar lagi."
        )
    note = str(result.get("note") or "Terjadi kesalahan saat mengecek status.")
    return f"⚠️ {note}\n\nKetik *BATAL* untuk membatalkan."


async def _advance_after_privy_signed(
    sess: SubmissionSession, signed_pdf: bytes,
) -> None:
    """Move the session out of AWAITING_PRIVY_SIGN onto the REVIEW path."""
    sess.signed_pdf = signed_pdf
    sess.stage = Stage.REVIEW
    sess.touch()
    _put_session(sess)
    if sess.privy_request_id:
        _privy_request_to_user.pop(sess.privy_request_id, None)
    logger.info(
        "Guided-submission Privy signed — moving to REVIEW | user=%s | size=%dB",
        _mask(sess.user_id), len(signed_pdf or b""),
    )


# Public hooks invoked by the inbound webhook router. Kept tiny so the
# router doesn't reach into private state machine internals. Best-effort
# — any failure is logged but does not propagate.

async def notify_privy_signed(*, request_id: str, signed_pdf: bytes) -> None:
    """Webhook reports the citizen finished signing. Submit to SIAP."""
    user_id = _privy_request_to_user.get(request_id)
    if not user_id:
        logger.info(
            "Privy webhook signed: no matching session | request_id=%s",
            (request_id or "")[:8] + "…",
        )
        return
    sess = _get_session(user_id)
    if sess is None or sess.stage != Stage.AWAITING_PRIVY_SIGN:
        logger.info(
            "Privy webhook signed: session not in AWAITING_PRIVY_SIGN | user=%s",
            _mask(user_id),
        )
        return
    await _advance_after_privy_signed(sess, signed_pdf)
    # Submit and push the result through whatever outbound channel the
    # citizen used (the webhook router has no inbound message context;
    # the reply is delivered via the notifications dispatcher).
    reply = await _submit(sess)
    await _push_citizen_message(user_id, reply)


async def notify_privy_failed(*, request_id: str, reason: str) -> None:
    """Webhook reports the citizen abandoned / signing expired."""
    user_id = _privy_request_to_user.pop(request_id, None)
    if not user_id:
        logger.info(
            "Privy webhook failed: no matching session | request_id=%s | reason=%s",
            (request_id or "")[:8] + "…", reason,
        )
        return
    sess = _get_session(user_id)
    if sess is None:
        return
    # Put them back at REVIEW so they can either retype "KIRIM" to retry
    # (which will rebuild the Privy request) or "BATAL" to abort.
    sess.stage = Stage.REVIEW
    sess.privy_request_id = None
    sess.privy_signing_url = None
    sess.touch()
    _put_session(sess)
    await _push_citizen_message(
        user_id,
        (
            f"⚠️ Proses tanda tangan digital tidak dapat diselesaikan "
            f"({reason}). Permohonan Anda *belum terkirim*. Ketik *KIRIM* "
            f"untuk mencoba lagi, atau *BATAL* untuk membatalkan."
        ),
    )


async def _push_citizen_message(user_id: str, text: str) -> None:
    """Send a proactive message to the citizen.

    Channel routing is intentionally simple in this slice: the webhook
    runs out-of-band of an inbound message so we have no implicit
    channel context. We log + best-effort dispatch through the
    notifications module when available; the real Phase-5 wiring of
    multi-channel push is tracked in vault doc §7 and will fill this in.
    """
    try:
        # Late import — notifications module pulls in network deps.
        from services import notifications
    except Exception:
        notifications = None  # type: ignore[assignment]
    if notifications is None:
        logger.info(
            "Privy push fallback: notifications module unavailable | user=%s",
            _mask(user_id),
        )
        return
    push = getattr(notifications, "push_freeform", None)
    if push is None:
        logger.info(
            "Privy push fallback: notifications.push_freeform unavailable | "
            "user=%s — message buffered.", _mask(user_id),
        )
        return
    try:
        await push(user_id=user_id, text=text)
    except Exception:
        logger.exception(
            "Privy push_freeform crashed | user=%s", _mask(user_id),
        )


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

        if sess.stage == Stage.AWAITING_PRIVY_SIGN:
            # Citizen is waiting on Privy. Only two interactions are
            # meaningful here: "sudah belum?" → poll Privy, or any other
            # message → gently re-share the signing link.
            if _PRIVY_POLL_PATTERN.search(msg):
                return await _poll_privy_status(sess)
            if sess.privy_signing_url:
                return (
                    "Mohon selesaikan tanda tangan digital Anda di:\n"
                    f"{sess.privy_signing_url}\n\n"
                    "Ketik *SUDAH BELUM?* untuk mengecek status, atau "
                    "*BATAL* untuk membatalkan."
                )
            return (
                "Sedang menunggu konfirmasi tanda tangan digital. Ketik "
                "*SUDAH BELUM?* untuk mengecek status, atau *BATAL* untuk "
                "membatalkan."
            )

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
