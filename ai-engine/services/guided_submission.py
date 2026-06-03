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

import base64
import json
import logging
import mimetypes
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from services import session_store

load_dotenv()

logger = logging.getLogger("bima_ai.guided_submission")

_PORTAL_TRACK_URL = "https://portal.nolongin.com/track/{ticket}"

# The demo-slice license: "Surat Keterangan Penelitian" (Izin Penelitian) on
# Beta-SIAP. Used as the default license_id for content-scoring when the
# resolved session has no license_id yet (shouldn't happen in the normal
# flow, but the scorer needs a registry key). Overridable via env so the
# same code can score any license in a later slice.
_DEMO_LICENSE_ID = int(os.getenv("GUIDED_SUBMISSION_DEMO_LICENSE_ID", "358"))


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
    # COLLECTING_DOCS: applicant fields are done; we are waiting for the
    # citizen's document images/PDFs so BIMA can CONTENT-score them before
    # review. When no documents arrive (the chat transport has not delivered
    # media yet) the flow falls back to the demo-fixture validation path.
    COLLECTING_DOCS = "collecting_docs"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"


@dataclass
class SessionDocument:
    """One document the citizen sent in-session, held in memory for live
    content-scoring + officer doc-Q&A. Bytes never touch disk and are never
    logged."""

    file_id: str            # opaque per-session id (e.g. "doc-1")
    claimed_type: str       # what the citizen / packet labelled it
    filename: str
    mime_type: str
    content: bytes


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
    # documents the citizen sent in-session (bytes held in memory only) — the
    # input to live content-scoring and to the officer's doc-Q&A.
    documents: list[SessionDocument] = field(default_factory=list)
    # the last content-scoring result (kept so a 'KIRIM' retry and the officer
    # brief can reuse it without re-running Gemini Vision).
    last_score: Optional[dict[str, Any]] = None
    # True once the citizen has been shown the "send as-is" override prompt for
    # a sub-threshold packet. The NEXT KIRIM/YA then force-submits past the
    # ok-gate (otherwise KIRIM loops forever — a flawed packet could never be
    # filed). Reset implicitly per session.
    override_offered: bool = False
    # the resulting SIAP ticket once submitted (for officer-brief wiring).
    ticket: Optional[str] = None
    request_id: Optional[int] = None
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


# ---------------------------------------------------------------------------
# Redis serialization (durable sessions — services/session_store.py).
#
# The store is generic; this module supplies the (encode, decode) pair. The
# session is JSON with two careful exclusions:
#   * SessionDocument.content (raw bytes) → base64 string, decoded back to
#     bytes on load. Bytes are NEVER logged (session_store logs only lengths).
#   * last_score["result"] is a SuitabilityResult dataclass (not JSON-safe) →
#     DROPPED on encode. The JSON-safe score fields (ok/status/score_percent/
#     summary/message/issues) survive, so the score reminder + KIRIM retry +
#     officer brief still work after a restart. A post-restart KIRIM that needs
#     the rich `result` simply re-scores from the rehydrated document bytes.
# ---------------------------------------------------------------------------

def _score_for_redis(score: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Strip the non-JSON `result` dataclass from a last_score dict so it can
    be persisted. Keeps every JSON-safe field. Returns None unchanged."""
    if not isinstance(score, dict):
        return None
    return {k: v for k, v in score.items() if k != "result"}


def _encode_session(sess: SubmissionSession) -> str:
    docs = [
        {
            "file_id": d.file_id,
            "claimed_type": d.claimed_type,
            "filename": d.filename,
            "mime_type": d.mime_type,
            "content_b64": base64.b64encode(d.content).decode("ascii"),
        }
        for d in sess.documents
    ]
    payload = {
        "user_id": sess.user_id,
        "stage": sess.stage.value,
        "license_id": sess.license_id,
        "license_name": sess.license_name,
        "requirements": sess.requirements,
        "sla_working_days": sess.sla_working_days,
        "retribution_fee": sess.retribution_fee,
        "candidates": sess.candidates,
        "fields": sess.fields,
        "documents": docs,
        "last_score": _score_for_redis(sess.last_score),
        "override_offered": sess.override_offered,
        "ticket": sess.ticket,
        "request_id": sess.request_id,
        "created_at": sess.created_at,
        "updated_at": sess.updated_at,
    }
    return json.dumps(payload)


def _decode_session(blob: str) -> SubmissionSession:
    raw = json.loads(blob)
    docs = [
        SessionDocument(
            file_id=str(d["file_id"]),
            claimed_type=str(d.get("claimed_type", "")),
            filename=str(d.get("filename", "")),
            mime_type=str(d.get("mime_type", "application/octet-stream")),
            content=base64.b64decode(d["content_b64"]),
        )
        for d in raw.get("documents") or []
    ]
    return SubmissionSession(
        user_id=str(raw["user_id"]),
        stage=Stage(raw.get("stage", Stage.RESOLVING_LICENSE.value)),
        license_id=raw.get("license_id"),
        license_name=raw.get("license_name"),
        requirements=list(raw.get("requirements") or []),
        sla_working_days=raw.get("sla_working_days"),
        retribution_fee=raw.get("retribution_fee"),
        candidates=list(raw.get("candidates") or []),
        fields=dict(raw.get("fields") or {}),
        documents=docs,
        last_score=raw.get("last_score"),
        override_offered=bool(raw.get("override_offered", False)),
        ticket=raw.get("ticket"),
        request_id=raw.get("request_id"),
        created_at=float(raw.get("created_at", time.time())),
        updated_at=float(raw.get("updated_at", time.time())),
    )


async def _get_session(user_id: str) -> Optional[SubmissionSession]:
    """Read a session: in-memory first, then Redis (rehydrating in-memory on a
    durable hit so a restarted process re-warms its LRU on first touch)."""
    sess = _sessions.get(user_id)
    if sess is None:
        # In-memory miss — try the durable store (e.g. after a restart).
        sess = await session_store.load(
            session_store.submission_key(user_id), decode=_decode_session
        )
        if sess is not None:
            _sessions[user_id] = sess
            _sessions.move_to_end(user_id)
    if sess is None:
        return None
    if time.time() - sess.updated_at > _SESSION_TTL_SECONDS:
        logger.info("Guided-submission session expired | user=%s", _mask(user_id))
        _sessions.pop(user_id, None)
        await session_store.delete(session_store.submission_key(user_id))
        return None
    return sess


async def _put_session(sess: SubmissionSession) -> None:
    """Write-through: in-memory LRU + best-effort Redis (TTL = session TTL)."""
    _sessions[sess.user_id] = sess
    _sessions.move_to_end(sess.user_id)
    while len(_sessions) > _MAX_SESSIONS:
        _sessions.popitem(last=False)
    saved = await session_store.save(
        session_store.submission_key(sess.user_id),
        sess,
        encode=_encode_session,
        ttl_seconds=_SESSION_TTL_SECONDS,
    )
    # Durable sessions are supposed to be ON but the write didn't land — the
    # in-memory copy is fine, but this session won't survive a restart. Make
    # the missed durable write observable. Masked key only, never the payload.
    if not saved and session_store.is_enabled():
        logger.warning(
            "Guided-submission durable write missed (flag on) | key=%s",
            _mask(sess.user_id),
        )


async def _clear_session(user_id: str) -> None:
    _sessions.pop(user_id, None)
    await session_store.delete(session_store.submission_key(user_id))


async def has_active_session(user_id: str) -> bool:
    """True when this user has an in-flight (non-terminal) submission."""
    sess = await _get_session(user_id)
    return sess is not None and sess.stage in (
        Stage.RESOLVING_LICENSE,
        Stage.COLLECTING_FIELDS,
        Stage.COLLECTING_DOCS,
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


# ===========================================================================
# In-session document intake + LIVE content-scoring (the demo centerpiece).
#
# When the chat transport delivers the citizen's documents (real APTANA /
# Telegram media download — DEFERRED, or the curated demo packet below), they
# land on the session as `SessionDocument`s. `_run_content_score()` then runs
# the suitability judge over the real bytes: completeness, type-correctness,
# and per-requirement suitability via Gemini Vision. This is "3/7 + does-it-
# comply", not a checkbox — and it runs on documents BIMA holds in memory.
#
# Provenance honesty: for the June-4 demo the bytes come from a CURATED
# PACKET (a clean set + a deliberately-flawed set), NOT the 1,032 real Beta
# citizen files (whose bytes are not on Beta's disk). The SCORING is 100%
# real; only the document provenance is staged. See PR notes.
# ===========================================================================

# A directory of files to auto-attach as the citizen's "uploaded" documents
# when the chat transport hasn't delivered real media. Each filename is the
# claimed type: e.g. "surat_permohonan.pdf", "ktp.jpg", "proposal.pdf". Blank
# → no packet (the flow falls back to the demo-fixture validation path).
def _demo_packet_dir() -> Optional[Path]:
    raw = os.getenv("GUIDED_SUBMISSION_DEMO_PACKET", "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


# Hard cap mirrors routers/validator.py so an oversized file can't blow memory.
_MAX_DOC_BYTES = 8 * 1024 * 1024
_MAX_DOCS = 10


def _claimed_type_from_filename(filename: str) -> str:
    """Derive a citizen-claimed doc label from a packet filename stem.

    'surat_permohonan_materai.pdf' -> 'surat permohonan materai'. The
    suitability judge canonicalises this to a DOC_CLASS; unknown labels stay
    as free text and are still type-checked by Gemini.
    """
    stem = Path(filename).stem
    return re.sub(r"[_\-]+", " ", stem).strip() or "dokumen"


async def handle_inbound_documents(
    user_id: str,
    docs: list[SessionDocument],
) -> Optional[str]:
    """Public seam for a real chat-transport media webhook (routers/aptana.py).

    Attaches `docs` to the citizen's active guided-submission session and
    returns a citizen-facing reply string:
      * If the flow is at/after document collection (COLLECTING_DOCS / REVIEW),
        re-run live content-scoring and return the score message + review
        summary (the same thing the field→review transition shows). A re-upload
        in REVIEW thus refreshes the score.
      * If the flow is still collecting applicant fields (COLLECTING_FIELDS),
        store the docs and acknowledge — they're scored automatically when the
        last field is collected.
      * Returns None when there is no active session to attach to (the caller
        decides how to nudge the citizen), or when the flag is off.

    Never raises — degrades to None on any internal error so the caller can
    fall back to a generic reply. Bytes are never logged."""
    if not is_enabled():
        return None
    try:
        attached = await attach_documents(user_id, docs)
        if not attached:
            return None
        sess = await _get_session(user_id)
        if sess is None:
            return None
        if sess.stage in (Stage.COLLECTING_DOCS, Stage.REVIEW):
            # Re-score with the freshly attached bytes and show the result.
            return await _enter_doc_scoring(sess)
        # Still collecting applicant fields — acknowledge; the docs are scored
        # when the last field lands. Nudge the citizen back to the question.
        nxt = sess.next_missing_field()
        ack = (
            f"📎 Dokumen diterima ({len(sess.documents)} berkas). "
            "Akan saya periksa setelah data pemohon lengkap."
        )
        if nxt is not None:
            ack += f"\n\n{nxt.question}"
        return ack
    except Exception:
        logger.exception(
            "Guided-submission inbound-document handling crashed | user=%s",
            _mask(user_id),
        )
        return None


async def attach_documents(
    user_id: str,
    docs: list[SessionDocument],
) -> bool:
    """Public seam: attach documents the chat transport received to a citizen's
    in-flight session. This is the ONE function a real APTANA/Telegram media
    webhook calls once it has downloaded + decoded the bytes — the state
    machine and scoring do not change when that lands.

    Returns True when the docs were attached to an active session, False when
    there is no active session to attach to (caller can decide what to do).
    Bytes are held in memory only; never logged.
    """
    sess = await _get_session(user_id)
    if sess is None or sess.stage in (Stage.DONE, Stage.FAILED):
        return False
    # De-dup by file_id; cap total count.
    have = {d.file_id for d in sess.documents}
    for d in docs:
        if d.file_id in have:
            continue
        if len(d.content) > _MAX_DOC_BYTES:
            logger.warning(
                "attach_documents skipped oversize | user=%s | file=%s | bytes=%d",
                _mask(user_id), d.filename, len(d.content),
            )
            continue
        if len(sess.documents) >= _MAX_DOCS:
            break
        sess.documents.append(d)
        have.add(d.file_id)
    sess.touch()
    await _put_session(sess)
    logger.info(
        "Guided-submission documents attached | user=%s | count=%d",
        _mask(user_id), len(sess.documents),
    )
    return True


def _load_demo_packet(sess: SubmissionSession) -> int:
    """Load the curated demo packet (if configured) onto the session as
    SessionDocuments. Returns the number of docs loaded. No-op + returns the
    existing count when a packet dir isn't configured or the session already
    has documents (real media won the race)."""
    if sess.documents:
        return len(sess.documents)
    pkt = _demo_packet_dir()
    if pkt is None:
        return 0
    loaded = 0
    for path in sorted(pkt.iterdir()):
        if loaded >= _MAX_DOCS:
            break
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            logger.warning("demo packet read failed | file=%s | err=%s", path.name, exc)
            continue
        if not data or len(data) > _MAX_DOC_BYTES:
            continue
        mime, _ = mimetypes.guess_type(path.name)
        sess.documents.append(SessionDocument(
            file_id=f"doc-{loaded + 1}",
            claimed_type=_claimed_type_from_filename(path.name),
            filename=path.name,
            mime_type=mime or "application/octet-stream",
            content=data,
        ))
        loaded += 1
    if loaded:
        logger.info(
            "Guided-submission demo packet loaded | user=%s | docs=%d",
            _mask(sess.user_id), loaded,
        )
    return loaded


async def _run_content_score(sess: SubmissionSession) -> Optional[dict[str, Any]]:
    """Run the LIVE suitability judge over the session's in-memory documents.

    Returns a normalised result dict the flow + officer brief reuse:
      {"ok": bool, "status": str, "score_percent": int, "summary": str,
       "message": str (rendered WhatsApp text), "issues": [{severity, message}],
       "result": SuitabilityResult}

    Returns None when there are no in-session documents — the caller then
    falls back to the demo-fixture validation path. Never raises.
    """
    if not sess.documents:
        return None

    from services import citizen_scorer
    from services.agents.suitability_judge import UploadedDoc

    uploaded = [
        UploadedDoc(
            file_id=d.file_id,
            claimed_type=d.claimed_type,
            filename=d.filename,
            mime_type=d.mime_type,
            content=d.content,
        )
        for d in sess.documents
    ]
    license_id = sess.license_id or _DEMO_LICENSE_ID
    try:
        result = await citizen_scorer.score_session_documents(
            license_id=license_id,
            documents=uploaded,
        )
    except Exception:
        logger.exception(
            "Guided-submission content-score crashed | user=%s", _mask(sess.user_id)
        )
        return None

    ready = citizen_scorer.is_submission_ready(result)
    percent = int(round(result.overall_suitability_score * 100))
    message = citizen_scorer.render_score_message(
        result, license_name=sess.license_name
    )
    issues = [
        {"severity": i.severity, "message": i.title}
        for i in result.issues
    ]
    logger.info(
        "Guided-submission content-score | user=%s | license_id=%s | "
        "percent=%d | ready=%s | issues=%d",
        _mask(sess.user_id), license_id, percent, ready, len(issues),
    )
    return {
        "ok": ready,
        "status": "ready" if ready else "needs_fix",
        "score_percent": percent,
        "summary": message,
        "message": message,
        "issues": issues,
        "result": result,
    }


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

# Indonesian one-word status labels for the compact score reminder.
_SCORE_STATUS_LABEL: dict[str, str] = {
    "ready": "siap dikirim",
    "needs_fix": "perlu diperbaiki",
    "unverified": "belum tervalidasi",
}


def _score_reminder_prefix(sess: SubmissionSession) -> str:
    """A compact one-line score reminder prepended to REVIEW-stage replies so
    the citizen keeps the context of their latest document score across turns
    (the full score message is only shown once, at the field→review
    transition). Empty string when no score has been computed yet.

    Survives a restart because it reads the JSON-safe `last_score` fields that
    `_encode_session` persists (the rich `result` dataclass is dropped, but
    score_percent + status are kept)."""
    score = sess.last_score
    if not isinstance(score, dict) or "score_percent" not in score:
        return ""
    pct = score.get("score_percent")
    status = _SCORE_STATUS_LABEL.get(str(score.get("status", "")), "")
    tail = f" ({status})" if status else ""
    return f"📊 Skor terkini: {pct}%{tail}\n\n"


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
    """Begin a new guided submission: resolve the licence the citizen named.

    Resolution is LLM-driven and grounded in the WHOLE SIAP licence catalogue
    (`services.license_resolver.resolve_license_intent`). We pass the RAW
    message — the model understands intent, expands acronyms, and tolerates
    typos/word-order/synonyms, so the old regex stopword-stripping (which
    polluted a substring ILIKE) is no longer needed. The resolver returns the
    same {found, matches, note} shape the previous `siap_lookup_license` path
    did, so the downstream logic below is unchanged.
    """
    from services.license_resolver import resolve_license_intent

    sess = SubmissionSession(user_id=user_id)

    lookup = await resolve_license_intent(message)

    if not lookup.get("found"):
        # Could not resolve — keep the session in RESOLVING_LICENSE and ask
        # the citizen to name the licence precisely.
        sess.stage = Stage.RESOLVING_LICENSE
        await _put_session(sess)
        note = lookup.get("note", "")
        logger.info(
            "Guided-submission start — licence unresolved | user=%s",
            _mask(user_id),
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
        await _put_session(sess)
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
    await _put_session(sess)
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
        await _put_session(sess)
        return _fmt_review(sess)

    ok, cleaned = spec.validate(message)
    if not ok:
        sess.touch()
        await _put_session(sess)
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
        # All applicant fields collected. Move into document content-scoring.
        return await _enter_doc_scoring(sess)

    await _put_session(sess)
    collected = len(sess.fields)
    return f"✅ Tercatat.\n\n{collected + 1}️⃣ {nxt.question}"


async def _enter_doc_scoring(sess: SubmissionSession) -> str:
    """Transition COLLECTING_FIELDS → (live content-score) → REVIEW.

    If the chat transport has delivered documents (or the curated demo packet
    is configured), BIMA CONTENT-scores them live here and shows the citizen
    the score before asking for confirmation. When no documents are available,
    fall back to the existing demo-fixture review path so the flow still works
    on a transport that hasn't wired media yet.
    """
    sess.stage = Stage.COLLECTING_DOCS
    sess.touch()

    # Pull in the curated demo packet if real media hasn't arrived.
    _load_demo_packet(sess)

    if sess.documents:
        score = await _run_content_score(sess)
        if score is not None:
            sess.last_score = score
            sess.stage = Stage.REVIEW
            await _put_session(sess)
            head = "✅ Semua data pemohon terkumpul.\n\n"
            # The live content-score message is the centerpiece the citizen sees.
            return (
                head
                + score["message"]
                + "\n\n"
                + _fmt_review(sess)
            )

    # No in-session documents (or scoring unavailable) → fixture review path.
    sess.stage = Stage.REVIEW
    await _put_session(sess)
    return "✅ Semua data pemohon terkumpul.\n\n" + _fmt_review(sess)


async def _submit(sess: SubmissionSession, *, force: bool = False) -> str:
    """REVIEW + citizen confirmed: run the validator, then submit to SIAP.

    Validation issues → tell the citizen what to fix, do NOT submit, and offer
    a "send as-is" override.
    `force=True` → the citizen has explicitly consented to send the
    sub-threshold packet anyway (second KIRIM); bypass the ok-gate and submit.
    Clean validation  → POST to SIAP, return ticket + portal track link.
    """
    from services.siap_submission_client import get_siap_submission_client

    # --- Step 1: score / validate the submission ------------------------
    # Prefer the LIVE content-score (real Gemini Vision over the documents the
    # citizen sent in-session). Reuse the score computed at the field→review
    # transition when present; otherwise score now. Fall back to the demo
    # fixture path only when there are no in-session documents at all.
    result: dict[str, Any]
    score = sess.last_score
    # Re-score when there's no score yet OR when only the JSON-safe score
    # survived a Redis rehydrate (the rich `result` dataclass is dropped on
    # encode — see _score_for_redis). Without the `result` the score can't drive
    # the officer brief / sub-threshold message correctly, so a post-restart
    # KIRIM must re-score from the rehydrated document bytes.
    if (score is None or "result" not in score) and sess.documents:
        score = await _run_content_score(sess)
        sess.last_score = score
    if score is not None:
        result = score
    else:
        result = _run_validation(sess)

    if not result["ok"] and not force:
        # Sub-threshold and not yet force-confirmed. Keep the session at REVIEW,
        # OFFER the "send as-is" override, and remember we offered it so the
        # next KIRIM/YA submits past the gate (prevents the KIRIM dead-end loop).
        sess.stage = Stage.REVIEW
        sess.override_offered = True
        sess.touch()
        await _put_session(sess)
        # The content-score path already renders a full WhatsApp message; the
        # fixture path uses the legacy issue formatter.
        if score is not None:
            return (
                result["message"]
                + "\n\nKetik *KIRIM* untuk mengirim apa adanya, atau *BATAL* "
                "untuk membatalkan."
            )
        return (
            _fmt_validation_issues(result)
            + "\n\nKetik *KIRIM* untuk mengirim apa adanya, atau *BATAL* "
            "untuk membatalkan."
        )

    # --- Step 2: submit to SIAP -----------------------------------------
    client = get_siap_submission_client()
    if not client.is_configured():
        sess.stage = Stage.FAILED
        sess.touch()
        await _put_session(sess)
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
        await _put_session(sess)
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
        await _put_session(sess)
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
    request_id = submit.get("request_id")

    # SIAP's create endpoint returns a ticket but can omit the id/request_id on
    # a successful submit. Flow-based officer resolution
    # (officer_bridge.notify_officer_of_submission → siap_db.resolve_step_officers)
    # is gated on a non-None request_id, so without it the officer notify
    # silently skips flow resolution and (absent a BIMA_OFFICER_WA_PHONE
    # fallback) notifies nobody. Recover the request_id from the freshly
    # allocated ticket BEFORE the hand-off. Never let this crash the submit path.
    if request_id is None and ticket:
        try:
            from services.siap_tools import siap_resolve_request_id

            request_id = await siap_resolve_request_id(ticket)
        except Exception:
            logger.exception(
                "Guided-submission: request_id resolution from ticket crashed "
                "(non-fatal) | user=%s", _mask(sess.user_id),
            )
            request_id = None
        if request_id is None:
            logger.warning(
                "officer notify: ticket=%s has no request_id; flow resolution "
                "skipped", _mask(ticket),
            )

    sess.ticket = ticket
    sess.request_id = request_id
    sess.stage = Stage.DONE
    sess.touch()
    logger.info(
        "Guided-submission SUBMITTED | user=%s | license_id=%s | ticket=%s",
        _mask(sess.user_id), sess.license_id, ticket,
    )

    # --- Officer hand-off: brief + score over WhatsApp/Telegram ---------
    # Fire-and-forget so a notification hiccup never blocks the citizen's
    # success reply. The officer bridge is feature-flagged + degrades to a
    # no-op when unconfigured. We pass the in-session documents + the live
    # score so the officer can chat with the docs and see the BIMA score.
    if ticket:
        try:
            from services import officer_bridge

            await officer_bridge.notify_officer_of_submission(
                ticket=ticket,
                request_id=request_id,
                license_id=sess.license_id,
                license_name=sess.license_name,
                applicant_name=sess.fields.get("applicant_name"),
                score=sess.last_score,
                documents=list(sess.documents),
            )
        except Exception:
            logger.exception(
                "Guided-submission officer hand-off failed (non-fatal) | user=%s",
                _mask(sess.user_id),
            )

    await _clear_session(sess.user_id)

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
    sess = await _get_session(user_id)

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
                await _clear_session(user_id)
                return None  # degrade: let ai_handler take the message
        return None

    # --- Active session: this message belongs to the flow ---------------
    # A citizen can bail out of the form at any step.
    if _CANCEL_PATTERN.match(msg):
        await _clear_session(user_id)
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
            # Pass the RAW message: the LLM resolver understands intent on its
            # own, so prepending "ajukan izin " (as before) only added filler
            # the old substring matcher then choked on — e.g. "PKPP" became
            # "ajukan izin PKPP" → 0 matches.
            return await _start_session(user_id, msg)

        if sess.stage == Stage.COLLECTING_FIELDS:
            return await _collect_field(sess, msg)

        if sess.stage == Stage.COLLECTING_DOCS:
            # Transient stage — normally we transition straight through to
            # REVIEW inside _enter_doc_scoring. If we land here (e.g. a process
            # restart left a session mid-flight, or real media intake is being
            # awaited), re-run the doc-scoring transition.
            return await _enter_doc_scoring(sess)

        if sess.stage == Stage.REVIEW:
            # If the "send as-is" override has already been offered for a
            # sub-threshold packet, the next YA/KIRIM force-submits past the
            # ok-gate. Otherwise it's the first confirm → validate normally.
            if _AFFIRM_PATTERN.match(msg):
                return await _submit(sess, force=sess.override_offered)
            # "KIRIM" retry after a validation-issues message.
            if re.match(r"^\s*kirim\s*$", msg, re.IGNORECASE):
                return await _submit(sess, force=sess.override_offered)
            # Any other REVIEW-stage message (e.g. a clarifying question before
            # confirming): re-anchor the citizen with the latest score so they
            # never lose the context of why validation passed/failed.
            return (
                _score_reminder_prefix(sess)
                + "Ketik *YA* untuk memvalidasi dan mengirim permohonan, atau "
                "*BATAL* untuk membatalkan. Jika ada data yang ingin diubah, "
                "ketik *BATAL* lalu mulai ulang pengajuan."
            )
    except Exception:
        logger.exception(
            "Guided-submission handler crashed | user=%s | stage=%s",
            _mask(user_id), sess.stage,
        )
        await _clear_session(user_id)
        return (
            "Maaf, terjadi gangguan saat memproses pengajuan Anda. "
            "Pengajuan dibatalkan — mohon mulai ulang dengan menyebutkan "
            "izin yang ingin Anda ajukan."
        )

    # Unknown stage — clear and let ai_handler handle the message.
    await _clear_session(user_id)
    return None
