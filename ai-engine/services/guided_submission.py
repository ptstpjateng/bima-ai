"""
Guided submission — BIMA walks a citizen through filing a real SIAP licence
application end to end, conversationally, like a human assistant.

────────────────────────────────────────────────────────────────────────────
THE REDESIGN (2026-06) — warm, document-first, no rigid keywords
────────────────────────────────────────────────────────────────────────────
The previous flow gated every step on an exact keyword the citizen had to type
(SIAP to start, then a 4-question applicant Q&A, then SELESAI to score, then YA
to submit, BATAL to cancel). The product owner wants BIMA to feel like a human
clerk: warm Bahasa Indonesia, intent understood (never "Ketik X"), and the
applicant fields READ FROM the uploaded documents rather than asked one by one.

The new shape is three stages:

  RESOLVING_LICENSE — the citizen names a licence; the LLM resolver
                      (`license_resolver.resolve_license_intent`) maps it
                      against the whole SIAP catalogue. On a single match BIMA
                      warmly lists the requirements and invites an upload right
                      away — NO readiness gate, NO field questions.

  COLLECTING_DOCS   — the citizen sends photos/PDFs. WhatsApp delivers each
                      media item as a SEPARATE webhook, so BIMA attaches each
                      file SILENTLY (no per-file "received N" reply) and runs a
                      per-session DEBOUNCE: once uploads settle (~8 s with no
                      new file) it does ONE consolidated pass — score + extract
                      the applicant fields from the docs — and sends ONE warm
                      reply with the completeness, score, the data it read back
                      (NIK masked), and a natural confirmation question.

  CONFIRM           — the citizen replies in natural language. A small LLM
                      intent classifier (`submission_intent.classify_confirm_intent`)
                      maps it to AFFIRM (submit), DECLINE (hold/cancel),
                      CORRECT (fix a field → re-show), or QUESTION (answer →
                      re-ask). No "YA" keyword required; the literal "batal"
                      still cancels as a strong decline.

  DONE / FAILED     — terminal; the session is cleared.

────────────────────────────────────────────────────────────────────────────
THE DEBOUNCE (the key fix) + OUT-OF-BAND SEND
────────────────────────────────────────────────────────────────────────────
`routers/aptana.py:_process_inbound_media` runs in a BackgroundTask per media
webhook and calls `handle_inbound_documents`. A burst of N files would, in the
old code, produce N "Dokumen diterima (k total)" replies. Now:

  * each file is attached SILENTLY (handle_inbound_documents returns None at
    COLLECTING_DOCS), `sess.last_doc_at` is stamped + persisted, and
  * a SINGLE debounce task is ensured per session (guarded by the module-level
    `_debounce_tasks` set so 7 files spawn 1 task, not 7).

The debounce task loops { sleep(DEBOUNCE_SECONDS); reload; break when settled },
then runs the consolidated processing and SENDS the reply DIRECTLY via the
channel sender (`_send_to_user`, picked by the `wa-`/`tg-` user_id prefix) —
because it runs out-of-band, not as a router return value.

Restart tolerance: if the process dies mid-debounce the task is lost, but the
documents are still attached in the durable session — a later upload re-arms the
debounce, and a later text at COLLECTING_DOCS also triggers the consolidated
pass. Nothing is stranded.

────────────────────────────────────────────────────────────────────────────
FIELDS — extracted from the documents, not asked
────────────────────────────────────────────────────────────────────────────
In the consolidated pass we extract:
  * name + NIK from the KTP (Gemini Vision via `gemini_vision.extract_ktp_fields`),
  * business name from the NIB / SIUP when present, else the person's name,
  * phone = the citizen's OWN WhatsApp msisdn (from user_id 'wa-{msisdn}') —
    never asked.
A REQUIRED field that can't be read (no KTP / unreadable) is asked for
naturally, without restarting the flow.

────────────────────────────────────────────────────────────────────────────
PRESERVED INVARIANTS
────────────────────────────────────────────────────────────────────────────
* Durable Redis sessions (services/session_store.py) — write-through, the
  (encode, decode) pair lives here. `last_doc_at` and `fields` are persisted;
  document bytes go base64 inside the JSON; the rich `last_score["result"]`
  dataclass is dropped on encode (re-scored on demand).
* Officer notify on submit (`officer_bridge.notify_officer_of_submission`) with
  the score + documents — fire-and-forget, never blocks the success reply.
* PII masking (`services.pii.mask_pii`) on every NIK shown / forwarded / logged;
  `_mask` for user_id / NIK in log lines.
* The LLM licence resolver.
* Graceful fallbacks everywhere (Vision down → ask for the field; LLM intent
  down → keyword yes/no; SIAP down → friendly "belum terkirim"). NEVER raises
  into the chat path.
* No emoji in any citizen-facing string.

FEATURE FLAG: `BIMA_GUIDED_SUBMISSION_ENABLED` (default "false"). Off → every
public entrypoint returns None / no-ops; ai_handler behaves as before.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
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
from services.pii import mask_pii

load_dotenv()

logger = logging.getLogger("bima_ai.guided_submission")

_PORTAL_TRACK_URL = "https://portal.nolongin.com/track/{ticket}"

# The demo-slice license: "Surat Keterangan Penelitian" (Izin Penelitian) on
# Beta-SIAP. Used as the default license_id for content-scoring when the
# resolved session has no license_id yet (shouldn't happen in the normal flow,
# but the scorer needs a registry key). Overridable via env.
_DEMO_LICENSE_ID = int(os.getenv("GUIDED_SUBMISSION_DEMO_LICENSE_ID", "358"))

# How long (seconds) a burst of uploads must be quiet before BIMA processes the
# whole set as ONE reply. WhatsApp delivers media as separate webhooks that can
# arrive several seconds apart, so this needs to be generous enough to gather a
# realistic multi-file send. Read at call time so ops/tests can tune it.
_DEFAULT_DEBOUNCE_SECONDS = 8.0


def _debounce_seconds() -> float:
    raw = os.getenv("GUIDED_SUBMISSION_DEBOUNCE_SECONDS", "").strip()
    try:
        val = float(raw) if raw else _DEFAULT_DEBOUNCE_SECONDS
    except ValueError:
        val = _DEFAULT_DEBOUNCE_SECONDS
    # Clamp to a sane band; a 0 would defeat the debounce, a huge value would
    # strand the citizen waiting.
    return max(0.5, min(val, 120.0))


# ===========================================================================
# Feature flag
# ===========================================================================

def is_enabled() -> bool:
    """Read at call time, not import time — lets tests/ops flip the env."""
    return os.getenv("BIMA_GUIDED_SUBMISSION_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


# ===========================================================================
# Submission-intent detection — "I want to file a licence application".
#
# Used at the NO-active-session boundary to decide whether to START a flow, and
# mid-form to intercept a clear "saya mau ajukan izin ..." so it isn't eaten as
# a correction. Intentionally conservative — a false negative just routes to the
# normal chat path; a false positive hijacks a message into the form.
# ===========================================================================

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

_LICENSING_OBJECT_PATTERN = re.compile(
    r"\b(izin|perizinan|permohonan|permit|licen[cs]e|lisensi|nib|"
    r"sertifikat\s+standar|sertifikat)\b",
    re.IGNORECASE,
)


def detect_submission_intent(message: str) -> bool:
    """True when the message reads as 'I want to file a licence application'.

    Requires BOTH a filing verb-phrase AND a licensing-object noun, so a bare
    "saya mau daftar" or "apa itu izin usaha" does not start a flow.
    """
    if not message or not message.strip():
        return False
    return bool(
        _SUBMISSION_INTENT_PATTERN.search(message)
        and _LICENSING_OBJECT_PATTERN.search(message)
    )


# A numeric pick from a disambiguation shortlist ("2").
_NUMERIC_PICK_RE = re.compile(r"^\s*(\d{1,2})\s*$")


# ===========================================================================
# Applicant fields — now EXTRACTED from documents, not asked via Q&A.
#
# The keys are unchanged from the old per-field flow so the review summary,
# the SIAP description, the Redis encode/decode, and the officer brief all keep
# working. What changed is HOW they get populated: from the KTP / NIB / msisdn.
# ===========================================================================

_FIELD_KEYS = ("applicant_name", "nik", "business_name", "phone")

_FIELD_LABEL = {
    "applicant_name": "Nama",
    "nik": "NIK",
    "business_name": "Nama usaha",
    "phone": "No. WhatsApp",
}


def _v_nik(raw: str) -> Optional[str]:
    """Return the 16-digit NIK from free text, or None if not exactly 16."""
    digits = re.sub(r"\D", "", raw or "")
    return digits if len(digits) == 16 else None


def _v_name(raw: str) -> Optional[str]:
    """Clean a name; reject too-short / no-letter / intent-phrase values."""
    s = re.sub(r"\s+", " ", (raw or "")).strip()
    if len(s) < 3 or not re.search(r"[A-Za-z]", s):
        return None
    # Defence in depth: a new-submission intent must never be stored as a name.
    if _SUBMISSION_INTENT_PATTERN.search(s) or re.search(r"ajukan\s+izin", s, re.IGNORECASE):
        return None
    return s


def _v_business_name(raw: str) -> Optional[str]:
    s = re.sub(r"\s+", " ", (raw or "")).strip()
    return s if len(s) >= 2 else None


def _msisdn_from_user_id(user_id: str) -> Optional[str]:
    """Extract the citizen's own phone from a 'wa-{msisdn}' user_id.

    Returns the digits, or None for non-WhatsApp channels (e.g. 'tg-123', or a
    bare web-chat session id) where there is no phone to infer.
    """
    if not user_id:
        return None
    if user_id.startswith("wa-"):
        digits = re.sub(r"\D", "", user_id[3:])
        return digits or None
    return None


# ===========================================================================
# Session state
# ===========================================================================

class Stage(str, Enum):
    RESOLVING_LICENSE = "resolving_license"
    # PREPARING_DOCS: a curated licence (e.g. PPKP) is locked; BIMA collects the
    # data its sign-required documents need, drafts + delivers them, and hands
    # off to Mekari for e-signature + e-meterai before the upload step.
    PREPARING_DOCS = "preparing_docs"
    # COLLECTING_DOCS: the licence is locked and its requirements shown; BIMA is
    # gathering the citizen's uploaded files. Uploads attach SILENTLY and a
    # per-session debounce produces ONE consolidated reply once they settle.
    COLLECTING_DOCS = "collecting_docs"
    # CONFIRM: BIMA has scored the docs, extracted the applicant fields, and
    # shown the summary + confirmation question. The next message is classified
    # by intent (AFFIRM/DECLINE/CORRECT/QUESTION) — no rigid keyword.
    CONFIRM = "confirm"
    DONE = "done"
    FAILED = "failed"


@dataclass
class SessionDocument:
    """One document the citizen sent in-session, held in memory for live
    content-scoring + field extraction. Bytes never touch disk and are never
    logged."""

    file_id: str
    claimed_type: str
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
    candidates: list[dict[str, Any]] = field(default_factory=list)
    # collected applicant fields: key -> value (EXTRACTED from docs / msisdn).
    fields: dict[str, str] = field(default_factory=dict)
    # documents the citizen sent in-session (bytes in memory only).
    documents: list[SessionDocument] = field(default_factory=list)
    # the last content-scoring result (so a re-confirm + officer brief reuse it).
    last_score: Optional[dict[str, Any]] = None
    # True once a sub-threshold "ajukan apa adanya?" override has been offered,
    # so the NEXT AFFIRM force-submits past the ok-gate (no submit dead-end).
    override_offered: bool = False
    # When a citizen sends a clear NEW-submission intent mid-form, we stash the
    # raw text here and offer to switch; a later AFFIRM-to-switch re-resolves.
    pending_new_intent: Optional[str] = None
    # unix ts of the most recent attached document — drives the upload debounce.
    last_doc_at: float = 0.0
    ticket: Optional[str] = None
    request_id: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def missing_required_fields(self) -> list[str]:
        """Required applicant fields not yet populated. business_name is NOT
        required (it falls back to the person's name); phone comes from the
        msisdn. So 'required' for asking-the-citizen purposes is name + NIK."""
        required = ("applicant_name", "nik")
        return [k for k in required if not self.fields.get(k)]

    def touch(self) -> None:
        self.updated_at = time.time()


_MAX_SESSIONS = 500
_SESSION_TTL_SECONDS = 6 * 60 * 60  # 6 hours
_sessions: "OrderedDict[str, SubmissionSession]" = OrderedDict()

# Module-level guard so a burst of N uploads spawns exactly ONE debounce task
# per session, not N. Holds the user_ids that currently have a live debounce
# task in flight. Mutated only from the event loop (single-threaded asyncio).
_debounce_tasks: dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Redis serialization (durable sessions — services/session_store.py).
# ---------------------------------------------------------------------------

def _score_for_redis(score: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Strip the non-JSON `result` dataclass so the score dict can persist."""
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
        "pending_new_intent": sess.pending_new_intent,
        "last_doc_at": sess.last_doc_at,
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
    # `stage` may be an OLD value from a pre-redesign blob (confirming_start /
    # collecting_fields / review). Map any unknown legacy stage onto the closest
    # new stage so a restart across the deploy doesn't strand a citizen.
    stage = _coerce_stage(raw.get("stage"))
    return SubmissionSession(
        user_id=str(raw["user_id"]),
        stage=stage,
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
        pending_new_intent=raw.get("pending_new_intent"),
        last_doc_at=float(raw.get("last_doc_at", 0.0) or 0.0),
        ticket=raw.get("ticket"),
        request_id=raw.get("request_id"),
        created_at=float(raw.get("created_at", time.time())),
        updated_at=float(raw.get("updated_at", time.time())),
    )


# Legacy stage values that may appear in a durable blob written by the
# pre-redesign code. We don't want a Stage(...) ValueError to strand the
# citizen, so map them to the closest current stage.
_LEGACY_STAGE_MAP = {
    "confirming_start": Stage.COLLECTING_DOCS,
    "collecting_fields": Stage.COLLECTING_DOCS,
    "review": Stage.CONFIRM,
}


def _coerce_stage(value: Any) -> Stage:
    s = str(value or Stage.RESOLVING_LICENSE.value)
    if s in _LEGACY_STAGE_MAP:
        return _LEGACY_STAGE_MAP[s]
    try:
        return Stage(s)
    except ValueError:
        return Stage.RESOLVING_LICENSE


async def _get_session(user_id: str) -> Optional[SubmissionSession]:
    """Read a session: in-memory first, then Redis (rehydrating in-memory)."""
    sess = _sessions.get(user_id)
    if sess is None:
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
    if not saved and session_store.is_enabled():
        logger.warning(
            "Guided-submission durable write missed (flag on) | key=%s",
            _mask(sess.user_id),
        )


async def _clear_session(user_id: str) -> None:
    _sessions.pop(user_id, None)
    _debounce_tasks.pop(user_id, None)
    await session_store.delete(session_store.submission_key(user_id))


async def has_active_session(user_id: str) -> bool:
    """True when this user has an in-flight (non-terminal) submission."""
    sess = await _get_session(user_id)
    return sess is not None and sess.stage in (
        Stage.RESOLVING_LICENSE,
        Stage.PREPARING_DOCS,
        Stage.COLLECTING_DOCS,
        Stage.CONFIRM,
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
# Demo packet + document intake
# ===========================================================================

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _demo_packet_dir() -> Optional[Path]:
    raw = os.getenv("GUIDED_SUBMISSION_DEMO_PACKET", "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


_MAX_DOC_BYTES = 8 * 1024 * 1024
_MAX_DOCS = 10


def _claimed_type_from_filename(filename: str) -> str:
    """Derive a citizen-claimed doc label from a packet filename stem.

    'surat_permohonan_materai.pdf' -> 'surat permohonan materai'.
    """
    stem = Path(filename).stem
    return re.sub(r"[_\-]+", " ", stem).strip() or "dokumen"


async def handle_inbound_documents(
    user_id: str,
    docs: list[SessionDocument],
) -> Optional[str]:
    """Public seam for a real chat-transport media webhook (routers/aptana.py).

    NEW BEHAVIOUR (the debounce fix): attach the docs SILENTLY and arm a
    per-session debounce so a BURST of uploads yields exactly ONE consolidated
    reply (sent out-of-band by the debounce task). This entrypoint therefore
    returns None at COLLECTING_DOCS — there is no per-file acknowledgment.

    Returns:
      * None  — at COLLECTING_DOCS / CONFIRM: the doc was attached, the debounce
        was armed, and the consolidated reply will be SENT out-of-band. (None
        also for: flag off, no active session, terminal session.) The aptana
        caller already treats None as "nothing to send here".
      * a reply string — only at RESOLVING_LICENSE (the citizen uploaded before
        naming a licence): a gentle nudge to name the licence first.

    Never raises — degrades to None on any internal error. Bytes never logged.
    """
    if not is_enabled():
        return None
    try:
        sess = await _get_session(user_id)
        if sess is None or sess.stage in (Stage.DONE, Stage.FAILED):
            return None

        if sess.stage == Stage.RESOLVING_LICENSE:
            # The citizen sent a file before telling us which licence. Don't
            # attach to a license-less session — gently ask for the licence.
            return (
                "Terima kasih sudah mengirim dokumennya, Bapak/Ibu. Sebelum "
                "saya periksa, boleh sebutkan dulu izin apa yang ingin "
                "diajukan? Contohnya \"Izin Pemakaian Tanah\"."
            )

        if sess.stage == Stage.PREPARING_DOCS:
            # The citizen uploaded their OWN documents instead of sending data
            # for BIMA to draft — they already have them. Skip generation and
            # fall through to the normal collect + score flow (the debounce
            # processor only runs at COLLECTING_DOCS/CONFIRM).
            sess.stage = Stage.COLLECTING_DOCS
            sess.touch()
            await _put_session(sess)
            logger.info(
                "Guided-submission: upload during doc-prep -> COLLECTING_DOCS "
                "(citizen brought own docs) | user=%s", _mask(user_id),
            )

        # COLLECTING_DOCS or CONFIRM (a late upload): attach silently + debounce.
        attached = await _attach_documents(user_id, docs)
        if not attached:
            return None
        await _arm_debounce(user_id)
        return None
    except Exception:
        logger.exception(
            "Guided-submission inbound-document handling crashed | user=%s",
            _mask(user_id),
        )
        return None


async def attach_documents(user_id: str, docs: list[SessionDocument]) -> bool:
    """Public seam (kept for compatibility): attach docs to an active session.

    Returns True when attached to an active session, False otherwise. Does NOT
    arm the debounce — `handle_inbound_documents` is the webhook seam that does
    that. Bytes are held in memory only; never logged.
    """
    return await _attach_documents(user_id, docs)


async def _attach_documents(user_id: str, docs: list[SessionDocument]) -> bool:
    sess = await _get_session(user_id)
    if sess is None or sess.stage in (Stage.DONE, Stage.FAILED):
        return False
    have = {d.file_id for d in sess.documents}
    have_hashes = {
        hashlib.sha256(d.content).hexdigest() for d in sess.documents if d.content
    }
    added = 0
    for d in docs:
        if d.file_id in have:
            continue
        chash = hashlib.sha256(d.content).hexdigest() if d.content else None
        if chash is not None and chash in have_hashes:
            # Same bytes already attached under a DIFFERENT file_id — an APTANA
            # re-delivery or a re-sent identical file. Skip it so the packet
            # (and the score) isn't inflated with repeats.
            continue
        if len(d.content) > _MAX_DOC_BYTES:
            logger.warning(
                "attach_documents skipped oversize | user=%s | file_id=%s | bytes=%d",
                _mask(user_id), d.file_id, len(d.content),
            )
            continue
        if len(sess.documents) >= _MAX_DOCS:
            break
        sess.documents.append(d)
        have.add(d.file_id)
        if chash is not None:
            have_hashes.add(chash)
        added += 1
    sess.last_doc_at = time.time()
    sess.touch()
    await _put_session(sess)
    logger.info(
        "Guided-submission documents attached | user=%s | count=%d | added=%d",
        _mask(user_id), len(sess.documents), added,
    )
    return True


def _load_demo_packet(sess: SubmissionSession) -> int:
    """Load the curated demo packet (if configured) onto the session. Returns
    the number of docs loaded. No-op when no packet dir is configured or the
    session already has documents (real media won the race)."""
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


# ===========================================================================
# Out-of-band channel send (the debounce task replies directly).
# ===========================================================================

async def _send_to_user(user_id: str, text: str) -> bool:
    """Send `text` to the citizen on whatever channel their user_id namespaces.

    The consolidated upload reply is produced OUT-OF-BAND (in the debounce
    task), not as a router return value, so it must be pushed to the channel
    here. The sender is chosen by the user_id prefix:
      * 'wa-{msisdn}' → whatsapp_sender.send_text
      * 'tg-{chat_id}' → telegram sender (best-effort; skipped if unavailable)
    Returns True on a successful send. Never raises.
    """
    if not text:
        return False
    try:
        if user_id.startswith("wa-"):
            from services.whatsapp_sender import send_text
            msisdn = user_id[3:]
            return await send_text(recipient_phone=msisdn, body=text)
        if user_id.startswith("tg-"):
            # Best-effort Telegram support — only if a sender module exists.
            try:
                from services.telegram_sender import send_message  # type: ignore
            except Exception:
                logger.warning(
                    "Guided-submission: no telegram sender for out-of-band reply "
                    "| user=%s", _mask(user_id),
                )
                return False
            chat_id = user_id[3:]
            return await send_message(chat_id=chat_id, text=text)
        logger.warning(
            "Guided-submission: unknown channel for out-of-band reply | user=%s",
            _mask(user_id),
        )
        return False
    except Exception:
        logger.exception(
            "Guided-submission out-of-band send crashed | user=%s", _mask(user_id)
        )
        return False


# ===========================================================================
# Debounce — one task per session; consolidated reply once uploads settle.
# ===========================================================================

# Scoring acknowledgment: once an upload burst settles, the consolidated pass
# runs Gemini Vision over every page and can take 10-30s. Combined with the ~8s
# debounce wait, the citizen would otherwise stare at silence for the better
# part of a minute after their last upload (a live rehearsal on 2026-07-01
# surfaced exactly this "did it break?" gap). Fire ONE deferred acknowledgment —
# only if scoring actually runs long — so fast/edge cases stay clutter-free
# (Decisions §9). One debounce task per session ⇒ at most one ack per burst.
_SCORING_ACK_ENABLED = os.getenv("BIMA_SCORING_ACK_ENABLED", "true").lower() in ("1", "true", "yes")
_SCORING_ACK_DELAY_SECONDS = float(os.getenv("BIMA_SCORING_ACK_DELAY_SECONDS", "2.5"))
_SCORING_ACK_TEXT = os.getenv(
    "BIMA_SCORING_ACK_TEXT",
    "🔎 Dokumen Anda sedang saya periksa, mohon tunggu sebentar ya…",
)


def _start_scoring_ack(user_id: str) -> "Optional[asyncio.Task]":
    """Schedule a deferred 'still checking' bubble for the scoring pass.

    Returns the task so the caller can cancel it once scoring finishes; if
    scoring is unexpectedly fast the bubble never fires. Best-effort — swallows
    every error except cancellation, and reuses the channel-agnostic
    `_send_to_user` so it works for WhatsApp and Telegram alike.
    """
    if not _SCORING_ACK_ENABLED:
        return None

    async def _run() -> None:
        try:
            await asyncio.sleep(_SCORING_ACK_DELAY_SECONDS)
            await _send_to_user(user_id, _SCORING_ACK_TEXT)
        except asyncio.CancelledError:
            raise
        except Exception:  # never let a flaky ack disturb the scoring reply
            logger.debug("Scoring ack failed | user=%s", _mask(user_id), exc_info=True)

    return asyncio.create_task(_run())


async def _arm_debounce(user_id: str) -> None:
    """Ensure exactly ONE debounce task is running for this session.

    Guarded by the module-level `_debounce_tasks` dict so a burst of N uploads
    (each its own webhook → its own handle_inbound_documents call) spawns ONE
    task, not N. Subsequent uploads just re-stamp `last_doc_at`; the already-
    running task observes the later stamp and waits again.

    We store the actual `asyncio.Task` (not just the user_id) and hold that
    strong reference for the task's whole lifetime: the event loop only keeps a
    WEAK reference to pending tasks, so a fire-and-forget `create_task` whose
    handle is dropped can be garbage-collected mid-`sleep`, silently killing the
    one consolidated "here's your score" reply the citizen is waiting for.
    """
    if user_id in _debounce_tasks:
        return  # a task is already watching this session
    try:
        task = asyncio.create_task(_debounce_then_process(user_id))
    except RuntimeError:
        # No running event loop (shouldn't happen on the webhook path). The docs
        # are safely attached; a later upload can retry.
        logger.warning(
            "Guided-submission: could not schedule debounce (no loop) | user=%s",
            _mask(user_id),
        )
        return
    _debounce_tasks[user_id] = task  # strong ref until _debounce_then_process pops it


async def _debounce_then_process(user_id: str) -> None:
    """Wait for the upload burst to settle, then run the consolidated pass and
    SEND the reply out-of-band. Single task per session (see `_arm_debounce`).

    Loop: sleep(DEBOUNCE); reload session; if quiet long enough → break and
    process; else loop again (a new file arrived during the sleep). Never
    raises — always clears its `_debounce_tasks` guard on exit.
    """
    debounce = _debounce_seconds()
    try:
        # Bound the number of extra waits so a pathological drip of uploads
        # can't keep the task alive forever; after the cap we process anyway.
        for _ in range(60):
            await asyncio.sleep(debounce)
            sess = await _get_session(user_id)
            if sess is None or sess.stage in (Stage.DONE, Stage.FAILED):
                return
            if sess.stage not in (Stage.COLLECTING_DOCS, Stage.CONFIRM):
                return
            quiet_for = time.time() - sess.last_doc_at
            if quiet_for >= debounce:
                break
        else:
            sess = await _get_session(user_id)
            if sess is None:
                return

        # Uploads settled → the consolidated Vision pass can take 10-30s. Fire a
        # deferred ack so the citizen isn't left in silence, but cancel it in
        # `finally` if scoring returns fast (edge cases) so we don't clutter.
        ack_task = _start_scoring_ack(user_id)
        try:
            reply = await _process_collected_documents(sess)
        finally:
            if ack_task is not None and not ack_task.done():
                ack_task.cancel()
        if reply:
            await _send_to_user(user_id, reply)
    except Exception:
        logger.exception(
            "Guided-submission debounce processing crashed | user=%s",
            _mask(user_id),
        )
    finally:
        _debounce_tasks.pop(user_id, None)


# ===========================================================================
# Content scoring (LIVE suitability judge over in-session documents).
# ===========================================================================

async def _run_content_score(sess: SubmissionSession) -> Optional[dict[str, Any]]:
    """Run the LIVE suitability judge over the session's in-memory documents.

    Returns a normalised result dict the flow + officer brief reuse, or None
    when there are no documents. Never raises.
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
    license_id = sess.license_id
    if not license_id:
        # Never score real documents against the hardcoded demo permit (358 has
        # no structured requirements) — that yields a confidently-wrong %. With
        # no resolved licence we skip scoring; the confirm message renders
        # without a score and the citizen can still proceed.
        logger.warning(
            "Guided-submission content-score skipped: session has no license_id "
            "| user=%s", _mask(sess.user_id),
        )
        return None
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
    message = citizen_scorer.render_score_message(result, license_name=sess.license_name)
    issues = [{"severity": i.severity, "message": i.title} for i in result.issues]
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


# ===========================================================================
# Field extraction from the uploaded documents (replaces the 4-question Q&A).
# ===========================================================================

# Claimed-type / detected-type hints for picking the business-name source.
_BUSINESS_DOC_HINTS = ("nib", "siup", "akta", "nomor induk berusaha")


def _looks_like_ktp(doc: SessionDocument) -> bool:
    label = (doc.claimed_type or "").lower() + " " + (doc.filename or "").lower()
    return "ktp" in label or "kartu tanda penduduk" in label


def _looks_like_business_doc(doc: SessionDocument) -> bool:
    label = (doc.claimed_type or "").lower() + " " + (doc.filename or "").lower()
    return any(h in label for h in _BUSINESS_DOC_HINTS)


async def _extract_fields_from_documents(sess: SubmissionSession) -> None:
    """Populate sess.fields from the uploaded docs + the citizen's msisdn.

    * name + NIK: from the document that looks like a KTP (Gemini Vision). If no
      doc is obviously a KTP, try EVERY image/pdf until one extracts as a KTP.
    * business name: from a NIB/SIUP/Akta filename hint when present; otherwise
      left empty here and defaulted to the applicant name at summary time.
    * phone: the citizen's OWN msisdn from 'wa-{msisdn}' — never asked.

    Best-effort + defensive: a failed Vision call simply leaves a field empty
    (the caller asks for it naturally). Never raises. NIK is masked in logs.
    """
    # Phone — always from the channel id, never asked.
    msisdn = _msisdn_from_user_id(sess.user_id)
    if msisdn and not sess.fields.get("phone"):
        sess.fields["phone"] = msisdn

    # Name + NIK from the KTP — only if we don't already have BOTH (a CORRECT
    # earlier may have set one). Try the obvious KTP doc first, else any doc.
    need_name = not sess.fields.get("applicant_name")
    need_nik = not sess.fields.get("nik")
    if (need_name or need_nik) and sess.documents:
        ordered = sorted(sess.documents, key=lambda d: 0 if _looks_like_ktp(d) else 1)
        from services.gemini_vision import extract_ktp_fields, is_configured as _vision_ok

        if _vision_ok():
            for d in ordered:
                # Only attempt on image/pdf bytes the model can read.
                if not (d.mime_type.startswith("image/") or d.mime_type == "application/pdf"):
                    continue
                try:
                    ktp = await extract_ktp_fields(image_bytes=d.content, mime_type=d.mime_type)
                except Exception:
                    logger.exception(
                        "Guided-submission KTP extraction crashed | user=%s | file_id=%s",
                        _mask(sess.user_id), d.file_id,
                    )
                    ktp = None
                if not ktp or not ktp.get("is_ktp"):
                    continue
                if need_name and ktp.get("name"):
                    cleaned = _v_name(ktp["name"])
                    if cleaned:
                        sess.fields["applicant_name"] = cleaned
                        need_name = False
                if need_nik and ktp.get("nik"):
                    sess.fields["nik"] = ktp["nik"]  # already 16-digit-validated
                    need_nik = False
                # Optional gender for later Pak/Bu tailoring.
                if ktp.get("gender") and not sess.fields.get("gender"):
                    sess.fields["gender"] = ktp["gender"]
                # Address from the KTP → pre-fills the "Alamat" field on the
                # official SIAP templates (identity-only auto-fill).
                if ktp.get("alamat") and not sess.fields.get("alamat"):
                    sess.fields["alamat"] = ktp["alamat"]
                if not need_name and not need_nik:
                    break

    # Business name — read from the NIB/SIUP (izin berusaha) via Vision, NOT a
    # filename. For a Usaha Perorangan the nama usaha is legitimately the owner's
    # own name; for a Badan (PT/CV) it is the entity name — jenis_usaha labels
    # which. The filename hint only picks the CANDIDATE; Vision's `is_nib` is the
    # real gate. If no NIB is present or Vision fails, business_name stays unset
    # and falls back to the person's name at render (correct for a perorangan).
    if not sess.fields.get("business_name"):
        nib_doc = next(
            (
                d for d in sess.documents
                if any(
                    k in f"{d.claimed_type} {d.filename}".lower()
                    for k in ("siup", "nib", "izin berusaha", "oss")
                )
            ),
            None,
        )
        if nib_doc is not None:
            try:
                from services.gemini_vision import extract_business_fields
                biz = await extract_business_fields(
                    image_bytes=nib_doc.content, mime_type=nib_doc.mime_type
                )
            except Exception:
                logger.exception(
                    "Business-doc extraction crashed | user=%s | file_id=%s",
                    _mask(sess.user_id), nib_doc.file_id,
                )
                biz = None
            if biz and biz.get("is_nib"):
                if biz.get("nama_usaha"):
                    sess.fields["business_name"] = biz["nama_usaha"]
                if biz.get("jenis_usaha"):
                    sess.fields["jenis_usaha"] = biz["jenis_usaha"]

    logger.info(
        "Guided-submission fields extracted | user=%s | name=%s | nik=%s | "
        "business=%s | phone=%s",
        _mask(sess.user_id),
        bool(sess.fields.get("applicant_name")),
        _mask(sess.fields.get("nik")),
        bool(sess.fields.get("business_name")),
        _mask(sess.fields.get("phone")),
    )


# ===========================================================================
# Reply builders — warm Bahasa Indonesia, WhatsApp-friendly, NO emoji.
# ===========================================================================

def _fmt_requirements_invite(sess: SubmissionSession) -> str:
    """The warm requirements + upload invite shown right after the licence is
    locked. No readiness gate, no field questions — straight to "upload here"."""
    lines = [
        f"Baik Bapak/Ibu, untuk mengajukan {sess.license_name} lewat SIAP "
        "Jateng ada beberapa syarat yang harus dipenuhi.",
    ]
    if sess.requirements:
        lines += ["", "Berikut dokumen persyaratan yang perlu disiapkan:"]
        for r in sess.requirements[:10]:
            lines.append(f"- {r}")
    lines += [
        "",
        "Pastikan semua dokumen tersedia ya, lalu silakan upload dokumennya "
        "di sini (boleh beberapa sekaligus).",
    ]
    return "\n".join(lines).rstrip()


def _data_readback_lines(sess: SubmissionSession) -> list[str]:
    """The 'data pemohon yang kami baca' block — NIK masked via mask_pii."""
    name = sess.fields.get("applicant_name") or "-"
    nik = sess.fields.get("nik")
    nik_shown = mask_pii(nik) if nik else "-"
    usaha = sess.fields.get("business_name") or sess.fields.get("applicant_name") or "-"
    # Label the business form so "Nama usaha: CASMO" reads as a legitimate Usaha
    # Perorangan (owner name = business name), not a bug.
    jenis = sess.fields.get("jenis_usaha")
    usaha_line = f"- Nama usaha: {usaha}"
    if jenis == "Perorangan":
        usaha_line += " (Usaha Perorangan)"
    elif jenis == "Badan":
        usaha_line += " (Badan Usaha)"
    return [
        "Data pemohon yang kami baca:",
        f"- Nama: {name}",
        f"- NIK: {nik_shown}",
        usaha_line,
    ]


def _fmt_confirm_message(sess: SubmissionSession, score: Optional[dict[str, Any]]) -> str:
    """The consolidated, warm reply after the upload burst settles: how many
    docs / completeness / score + the data read back (NIK masked) + a natural
    confirmation question. The score's own rendered message carries the
    per-document detail; we add the warm framing + the read-back + the question.
    """
    pct = score.get("score_percent") if isinstance(score, dict) else None
    complete = bool(score and score.get("ok"))

    lines: list[str] = []
    if complete:
        head = (
            "Baik Bapak/Ibu, dokumen sudah kami terima dan sudah lengkap "
            "sesuai persyaratan"
        )
        if pct is not None:
            head += f", dengan estimasi kelayakan {pct}%"
        head += "."
        lines.append(head)
    else:
        head = "Baik Bapak/Ibu, dokumen sudah kami terima"
        if pct is not None:
            head += f", dengan estimasi kelayakan {pct}%"
        head += "."
        lines.append(head)

    # The detailed per-document score message (completeness, type checks,
    # suitability, issues) — already PII-masked + emoji-free by citizen_scorer.
    if score and score.get("message"):
        lines += ["", score["message"]]

    lines += [""] + _data_readback_lines(sess)

    if complete:
        lines += [
            "",
            "Apakah Bapak/Ibu yakin untuk mengajukan izin ini sekarang?",
        ]
    else:
        lines += [
            "",
            "Jika berkenan, Bapak/Ibu bisa melengkapi atau memperbaiki dokumen "
            "di atas lalu mengirimnya lagi. Atau jika ingin tetap mengajukan apa "
            "adanya, beri tahu saya saja.",
        ]
    return "\n".join(lines).rstrip()


def _ask_missing_field(sess: SubmissionSession, missing: list[str]) -> str:
    """Warmly ask for a required field BIMA couldn't read from the documents,
    without restarting the flow."""
    if "applicant_name" in missing and "nik" in missing:
        return (
            "Dokumen sudah saya terima, terima kasih. Hanya saja saya belum bisa "
            "membaca KTP-nya dengan jelas. Boleh kirim ulang foto KTP yang lebih "
            "jelas, atau ketik nama lengkap dan NIK (16 digit) Bapak/Ibu di sini?"
        )
    if "nik" in missing:
        name = sess.fields.get("applicant_name") or "Bapak/Ibu"
        return (
            f"Terima kasih, {name}. Saya belum bisa membaca NIK dari dokumennya. "
            "Boleh kirim ulang foto KTP yang lebih jelas, atau ketik NIK "
            "(16 digit) di sini?"
        )
    if "applicant_name" in missing:
        return (
            "Terima kasih, dokumennya sudah saya terima. Saya belum bisa membaca "
            "nama lengkapnya dengan jelas. Boleh sebutkan nama lengkap sesuai KTP?"
        )
    # Shouldn't reach here (only name/NIK are required), but be safe.
    return (
        "Terima kasih, dokumennya sudah saya terima. Boleh lengkapi sedikit lagi "
        "data Bapak/Ibu supaya bisa saya proses?"
    )


# ===========================================================================
# State transitions
# ===========================================================================

async def _start_session(user_id: str, message: str) -> str:
    """Begin a new guided submission: resolve the licence the citizen named via
    the LLM resolver grounded in the whole SIAP catalogue."""
    from services.license_resolver import resolve_license_intent

    sess = SubmissionSession(user_id=user_id)
    lookup = await resolve_license_intent(message)

    if not lookup.get("found"):
        sess.stage = Stage.RESOLVING_LICENSE
        await _put_session(sess)
        note = lookup.get("note", "")
        logger.info("Guided-submission start — licence unresolved | user=%s", _mask(user_id))
        return (
            "Dengan senang hati saya bantu pengajuan izin Bapak/Ibu lewat SIAP "
            "Jateng. Boleh sebutkan nama izin yang ingin diajukan? "
            "Contohnya \"Izin Pemakaian Tanah\"."
            + (f"\n\n{note}" if note else "")
        )

    matches = lookup.get("matches") or []
    if len(matches) > 1:
        sess.candidates = matches[:8]
        sess.stage = Stage.RESOLVING_LICENSE
        await _put_session(sess)
        lines = [
            "Ada beberapa jenis izin yang sepertinya cocok. Boleh pilih salah "
            "satu dengan membalas nomornya:",
            "",
        ]
        for idx, m in enumerate(matches[:8], start=1):
            sektor = f" (Bidang: {m['sektor']})" if m.get("sektor") else ""
            lines.append(f"{idx}. {m['name']}{sektor}")
        return "\n".join(lines)

    return await _lock_license(sess, matches[0])


async def _lock_license(sess: SubmissionSession, match: dict[str, Any]) -> str:
    """Pin the chosen licence, fetch requirements, and go STRAIGHT to document
    collection with a warm requirements + upload invite. No readiness gate."""
    from services.siap_tools import siap_get_requirements

    sess.license_id = int(match["license_id"])
    sess.license_name = match["name"]
    sess.candidates = []

    reqs = await siap_get_requirements(license_id=sess.license_id)
    if reqs.get("found"):
        sess.requirements = list(reqs.get("requirements") or [])
        sess.sla_working_days = reqs.get("sla_working_days")
        sess.retribution_fee = reqs.get("retribution_fee")

    sess.touch()

    # Curated licences (e.g. PPKP) get the doc-prep co-pilot: BIMA drafts the
    # sign-required documents, so collect their data + generate them BEFORE the
    # upload step. Non-curated licences keep the plain upload flow untouched.
    from services import license_guides

    guide = license_guides.get_guide(sess.license_id)
    if guide is not None and not sess.documents:
        sess.stage = Stage.PREPARING_DOCS
        await _put_session(sess)
        logger.info(
            "Guided-submission locked (curated -> doc-prep) | user=%s | license_id=%s",
            _mask(sess.user_id), sess.license_id,
        )
        return _fmt_doc_prep_intro(sess, guide)

    sess.stage = Stage.COLLECTING_DOCS

    # Demo-rehearsal fallback ONLY (production never sets the packet env): if a
    # curated packet is configured, auto-load + process it now so the rehearsal
    # doesn't require live uploads.
    if not sess.documents and _demo_packet_dir() is not None:
        loaded = _load_demo_packet(sess)
        if loaded:
            await _put_session(sess)
            logger.info(
                "Guided-submission licence locked (+demo packet) | user=%s | license_id=%s",
                _mask(sess.user_id), sess.license_id,
            )
            return await _process_collected_documents(sess)

    await _put_session(sess)
    logger.info(
        "Guided-submission licence locked | user=%s | license_id=%s",
        _mask(sess.user_id), sess.license_id,
    )
    return _fmt_requirements_invite(sess)


async def _process_collected_documents(sess: SubmissionSession) -> str:
    """The consolidated pass after the upload burst settles (or on an explicit
    'I'm done' text / demo packet): score ALL docs once, extract the applicant
    fields, and move to CONFIRM with ONE warm reply.

    If a REQUIRED field can't be extracted, ask for JUST that field (stay at
    COLLECTING_DOCS so a re-upload / typed answer continues the same flow).

    Returns the citizen-facing reply string. Never raises (callers — the
    debounce task and maybe_handle — both rely on this).
    """
    # Pull in the curated demo packet if real media hasn't arrived (no-op in
    # production where the packet env is unset).
    _load_demo_packet(sess)

    if not sess.documents:
        # Nothing to process — stay collecting, gently nudge.
        sess.stage = Stage.COLLECTING_DOCS
        await _put_session(sess)
        return (
            "Saya belum menerima dokumennya, Bapak/Ibu. Silakan upload foto "
            "(JPG/PNG) atau PDF dokumen persyaratan di sini ya."
        )

    # Score + extract in this single pass.
    score = await _run_content_score(sess)
    if score is not None:
        sess.last_score = score
    await _extract_fields_from_documents(sess)

    missing = sess.missing_required_fields()
    if missing:
        # Keep the docs + score, stay at COLLECTING_DOCS, ask for the gap. A
        # typed name/NIK is consumed by the COLLECTING_DOCS text handler.
        sess.stage = Stage.COLLECTING_DOCS
        sess.touch()
        await _put_session(sess)
        logger.info(
            "Guided-submission missing fields after extract | user=%s | missing=%s",
            _mask(sess.user_id), missing,
        )
        return _ask_missing_field(sess, missing)

    sess.stage = Stage.CONFIRM
    sess.touch()
    await _put_session(sess)
    return _fmt_confirm_message(sess, score)


# ===========================================================================
# Submit to SIAP — preserved end to end (officer notify, PII, fallbacks).
# ===========================================================================

async def _submit(sess: SubmissionSession, *, force: bool = False) -> str:
    """CONFIRM + citizen affirmed: score/validate, then submit to SIAP.

    Sub-threshold + not forced → offer "ajukan apa adanya" and remember it, so
    the next AFFIRM force-submits (no submit dead-end). Clean → POST to SIAP,
    return ticket + portal track link. Fires the officer hand-off on success.
    """
    from services.siap_submission_client import get_siap_submission_client

    # --- Step 1: score / validate ---
    result: dict[str, Any]
    score = sess.last_score
    # Re-score when there's no score yet OR only the JSON-safe fields survived a
    # Redis rehydrate (the rich `result` was dropped on encode).
    if (score is None or "result" not in score) and sess.documents:
        score = await _run_content_score(sess)
        sess.last_score = score
    if score is not None:
        result = score
    else:
        result = {"ok": False, "status": "unverified", "score_percent": 0,
                  "message": "", "issues": []}

    if not result.get("ok") and not force:
        sess.stage = Stage.CONFIRM
        sess.override_offered = True
        sess.touch()
        await _put_session(sess)
        body = result.get("message") or (
            "Dokumen Bapak/Ibu belum sepenuhnya lengkap sesuai persyaratan."
        )
        return (
            body
            + "\n\nKalau Bapak/Ibu ingin tetap mengajukan apa adanya, beri tahu "
            "saya saja, atau silakan lengkapi dulu dokumennya lalu kirim lagi."
        )

    # --- Step 2: submit to SIAP ---
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
            "Dokumen Bapak/Ibu sudah lengkap dan tervalidasi. Namun kanal "
            "pengiriman otomatis ke SIAP belum aktif saat ini, jadi permohonan "
            "belum terkirim. Mohon hubungi petugas DPMPTSP untuk menyelesaikan "
            "pengajuan, atau coba lagi nanti. Mohon maaf atas ketidaknyamanannya."
        )

    profile_id_raw = os.getenv("GUIDED_SUBMISSION_PROFILE_ID", "").strip()
    if not profile_id_raw.isdigit():
        sess.stage = Stage.FAILED
        sess.touch()
        await _put_session(sess)
        logger.warning(
            "Guided-submission: no GUIDED_SUBMISSION_PROFILE_ID configured | user=%s",
            _mask(sess.user_id),
        )
        return (
            "Dokumen Bapak/Ibu sudah lengkap dan tervalidasi. Namun pemetaan "
            "profil pemohon ke SIAP belum dikonfigurasi saat ini, jadi "
            "permohonan belum terkirim. Mohon hubungi petugas DPMPTSP untuk "
            "menyelesaikan pengajuan."
        )

    description = (
        f"Permohonan {sess.license_name} via BIMA-AI untuk usaha "
        f"'{sess.fields.get('business_name') or sess.fields.get('applicant_name') or '-'}'."
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
            "Dokumen Bapak/Ibu sudah tervalidasi, tetapi pengiriman ke SIAP "
            f"belum berhasil:\n\n{note}\n\nPermohonan belum terkirim. Beri tahu "
            "saya jika ingin saya coba kirim lagi, atau hubungi petugas DPMPTSP."
        )

    # --- Success ---
    ticket = submit.get("ticket")
    request_id = submit.get("request_id")

    # SIAP can omit request_id on a successful submit; recover it from the
    # ticket so flow-based officer resolution (gated on request_id) still runs.
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
                "officer notify: ticket=%s has no request_id; flow resolution skipped",
                _mask(ticket),
            )

    sess.ticket = ticket
    sess.request_id = request_id
    sess.stage = Stage.DONE
    sess.touch()
    logger.info(
        "Guided-submission SUBMITTED | user=%s | license_id=%s | ticket=%s",
        _mask(sess.user_id), sess.license_id, _mask(ticket),
    )

    # --- Officer hand-off: brief + score (fire-and-forget) ---
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
            f"Permohonan Bapak/Ibu berhasil dikirim ke SIAP Jateng.\n\n"
            f"Jenis izin: {sess.license_name}\n"
            f"Nomor tiket: {ticket}\n\n"
            f"Pantau status permohonan kapan saja di:\n{track}\n\n"
            "Mohon simpan nomor tiket ini. Kami akan kabari saat status "
            "permohonan berubah. Terima kasih."
        )
    return (
        f"Permohonan Bapak/Ibu berhasil dikirim ke SIAP Jateng.\n\n"
        f"Jenis izin: {sess.license_name}\n\n"
        "Nomor tiket akan tersedia sebentar lagi. Bapak/Ibu dapat memantau "
        f"status di {_PORTAL_TRACK_URL.format(ticket='')[:-1]}. Terima kasih."
    )


# ===========================================================================
# CONFIRM-stage handlers (intent-based, no rigid keyword).
# ===========================================================================

async def _handle_confirm(sess: SubmissionSession, message: str) -> str:
    """Classify the citizen's CONFIRM-stage reply by INTENT and act:
      AFFIRM   → submit
      DECLINE  → warmly hold/cancel (clears the session)
      CORRECT  → update the field, re-show the summary, re-ask
      QUESTION → brief answer, then re-ask
      UNCLEAR  → gentle clarifying question
    """
    from services import submission_intent

    intent = await submission_intent.classify_confirm_intent(message)

    if intent == submission_intent.AFFIRM:
        return await _submit(sess, force=sess.override_offered)

    if intent == submission_intent.DECLINE:
        await _clear_session(sess.user_id)
        logger.info("Guided-submission declined at confirm | user=%s", _mask(sess.user_id))
        return (
            "Baik Bapak/Ibu, pengajuannya kita tahan dulu. Kapan pun siap untuk "
            "melanjutkan, cukup beri tahu saya ya."
        )

    if intent == submission_intent.CORRECT:
        applied = _apply_correction(sess, message)
        sess.touch()
        await _put_session(sess)
        if applied:
            lines = ["Baik, sudah saya perbarui."]
            lines += [""] + _data_readback_lines(sess)
            lines += ["", "Apakah Bapak/Ibu yakin untuk mengajukan izin ini sekarang?"]
            return "\n".join(lines)
        # Couldn't parse a concrete correction — ask what to fix.
        return (
            "Baik, ada data yang ingin diperbaiki ya. Boleh sebutkan datanya, "
            "misalnya \"NIK saya yang benar 3374...\" atau \"nama usaha PT Maju "
            "Jaya\"?"
        )

    if intent == submission_intent.QUESTION:
        answer = await _answer_confirm_question(sess, message)
        return (
            f"{answer}\n\n"
            "Apakah Bapak/Ibu yakin untuk mengajukan izin ini sekarang?"
        )

    # UNCLEAR → gentle clarifier (re-anchor with a compact score reminder).
    return (
        _score_reminder_prefix(sess)
        + "Mohon maaf, saya belum sepenuhnya menangkap maksud Bapak/Ibu. Apakah "
        "kita lanjutkan pengajuan izin ini sekarang? Jika ada data yang ingin "
        "diperbaiki, sebutkan saja datanya."
    )


# A correction message that supplies a NIK, e.g. "NIK saya 3374..." — extract
# a 16-digit run anywhere in the text.
_NIK_RUN_RE = re.compile(r"(?<!\d)(\d[\d .\-]{14,30}\d)(?!\d)")

# A "nama usaha ..." correction — capture the trailing value.
_BUSINESS_CORRECTION_RE = re.compile(
    r"(?:nama\s+usaha|usaha(?:nya)?|badan\s+usaha|nama\s+pt|nama\s+cv)\s*"
    r"(?:saya\s+)?(?:adalah\s+|yaitu\s+|:\s*)?(.+)$",
    re.IGNORECASE,
)

# A "nama (saya) ..." correction (the person's name) — only when it doesn't
# mention "usaha" (that's the business-name case above).
_NAME_CORRECTION_RE = re.compile(
    r"(?:nama(?:\s+lengkap)?|namanya)\s*(?:saya\s+)?(?:adalah\s+|yaitu\s+|:\s*)?(.+)$",
    re.IGNORECASE,
)


def _apply_correction(sess: SubmissionSession, message: str) -> bool:
    """Parse a free-text correction and update sess.fields. Returns True when at
    least one field was changed.

    Handles the common shapes:
      * a 16-digit NIK anywhere ("NIK saya yang benar 3374...") → nik
      * "nama usaha PT Maju Jaya" / "usaha saya ..." → business_name
      * "nama saya Budi" / "namanya Budi" → applicant_name
    Conservative: if nothing parses cleanly, returns False and the caller asks.
    """
    msg = (message or "").strip()
    changed = False

    # NIK first — a 16-digit run is unambiguous.
    m = _NIK_RUN_RE.search(msg)
    if m:
        nik = _v_nik(m.group(1))
        if nik:
            sess.fields["nik"] = nik
            changed = True

    # Business name (must check before the bare-name pattern, since "nama usaha"
    # also matches the name regex).
    mb = _BUSINESS_CORRECTION_RE.search(msg)
    if mb:
        val = _v_business_name(mb.group(1))
        if val:
            sess.fields["business_name"] = val
            changed = True
    elif "usaha" not in msg.lower():
        mn = _NAME_CORRECTION_RE.search(msg)
        if mn:
            val = _v_name(_name_candidate_cleanup(mn.group(1)))
            if val:
                sess.fields["applicant_name"] = val
                changed = True

    return changed


async def _answer_confirm_question(sess: SubmissionSession, message: str) -> str:
    """A brief, grounded answer to a citizen question at the CONFIRM stage.

    Kept deterministic + safe: we answer from the session's own facts (licence
    name, SLA, retribution, data privacy) rather than a free LLM generation, so
    a question never derails the flow or hallucinates. Falls back to a warm
    generic reassurance.
    """
    q = (message or "").lower()
    if any(w in q for w in ("berapa lama", "lama", "waktu", "proses berapa", "kapan selesai")):
        if sess.sla_working_days:
            return (
                f"Perkiraan waktu prosesnya sekitar {sess.sla_working_days} hari "
                "kerja setelah permohonan masuk dan diverifikasi petugas."
            )
        return (
            "Lama prosesnya mengikuti ketentuan SIAP Jateng untuk izin ini; "
            "setelah terkirim, Bapak/Ibu bisa memantau statusnya lewat portal."
        )
    if any(w in q for w in ("biaya", "bayar", "retribusi", "gratis", "harga")):
        if sess.retribution_fee:
            return f"Untuk biaya/retribusi izin ini: {sess.retribution_fee}."
        return (
            "Soal biaya/retribusi mengikuti ketentuan resmi SIAP Jateng untuk "
            "izin ini. Petugas akan mengonfirmasi rinciannya bila ada."
        )
    if any(w in q for w in ("aman", "privasi", "data saya", "bocor", "rahasia")):
        return (
            "Data Bapak/Ibu hanya kami gunakan untuk memproses permohonan izin "
            "ini ke SIAP Jateng dan tidak kami sebarkan."
        )
    if any(w in q for w in ("untuk apa", "buat apa", "kenapa", "kegunaan")):
        return (
            f"Pengajuan ini untuk menerbitkan {sess.license_name} Bapak/Ibu "
            "lewat SIAP Jateng, sistem perizinan resmi DPMPTSP Jawa Tengah."
        )
    return (
        "Tentu, saya bantu sebisanya. Untuk pertanyaan lebih rinci, petugas "
        "DPMPTSP juga bisa membantu setelah permohonan masuk."
    )


# ===========================================================================
# Score reminder (compact one-liner for CONFIRM re-prompts).
# ===========================================================================

_SCORE_STATUS_LABEL: dict[str, str] = {
    "ready": "siap dikirim",
    "needs_fix": "perlu diperbaiki",
    "unverified": "belum tervalidasi",
}


def _score_reminder_prefix(sess: SubmissionSession) -> str:
    """A compact 'Skor terkini: X% (status)' line prepended to a CONFIRM re-
    prompt so the citizen keeps the score context across turns. Empty string
    when no score has been computed yet. Survives a restart via the JSON-safe
    last_score fields."""
    score = sess.last_score
    if not isinstance(score, dict) or "score_percent" not in score:
        return ""
    pct = score.get("score_percent")
    status = _SCORE_STATUS_LABEL.get(str(score.get("status", "")), "")
    tail = f" ({status})" if status else ""
    return f"Skor terkini: {pct}%{tail}\n\n"


# ===========================================================================
# COLLECTING_DOCS text handler — the citizen typed something while we wait for
# (or have already gathered) documents.
# ===========================================================================

# Soft "I'm done sending documents" signal. Unlike the old hard SELESAI gate,
# this just lets the citizen nudge BIMA to process now if the debounce hasn't
# fired yet; it's not required.
_DONE_HINT_RE = re.compile(
    r"\b(selesai|sudah\s+semua|sudah\s+lengkap|udah\s+semua|udah\s+selesai|"
    r"itu\s+saja|sekian|cukup|done|finish(?:ed)?|sudah\s+saya\s+kirim|"
    r"sudah\s+upload)\b",
    re.IGNORECASE,
)


async def _handle_collecting_docs_text(sess: SubmissionSession, message: str) -> str:
    """A text message arrived at COLLECTING_DOCS. Possibilities:
      * the citizen is supplying a missing field we asked for (a name or NIK) →
        capture it, then process if both are now present;
      * the citizen says 'sudah semua' → process now if docs exist;
      * a typed name+NIK pair before any doc → capture what we can, nudge;
      * otherwise → a warm reminder to upload.
    Document arrival itself is handled out-of-band by the debounce, not here.
    """
    msg = message.strip()

    # Capture a typed correction/answer for a missing field (name / NIK).
    captured = _capture_typed_field(sess, msg)
    if captured:
        sess.touch()
        await _put_session(sess)
        missing = sess.missing_required_fields()
        if not missing and sess.documents:
            # All required fields present + docs on hand → consolidate to CONFIRM.
            return await _process_collected_documents(sess)
        if missing:
            return _ask_missing_field(sess, missing)
        # Fields complete but no docs yet — ask for the upload.
        return (
            "Terima kasih, datanya sudah saya catat. Tinggal upload dokumen "
            "persyaratannya ya (foto JPG/PNG atau PDF, boleh beberapa sekaligus)."
        )

    # Soft "I'm done" → process whatever we have now.
    if _DONE_HINT_RE.search(msg):
        if sess.documents:
            return await _process_collected_documents(sess)
        return (
            "Saya belum menerima dokumennya, Bapak/Ibu. Silakan upload foto "
            "(JPG/PNG) atau PDF dokumen persyaratan di sini terlebih dahulu ya."
        )

    # Otherwise — a warm reminder. If documents are already attached but the
    # debounce somehow hasn't fired (e.g. lost on a restart), processing them is
    # the most helpful response.
    if sess.documents and (time.time() - sess.last_doc_at) >= _debounce_seconds():
        return await _process_collected_documents(sess)

    return (
        f"Baik Bapak/Ibu. Untuk mengajukan {sess.license_name}, silakan upload "
        "dokumen persyaratannya di sini sebagai foto (JPG/PNG) atau PDF "
        "(boleh beberapa sekaligus). Saya periksa setelah semuanya masuk."
    )


def _capture_typed_field(sess: SubmissionSession, message: str) -> bool:
    """If the citizen TYPED a missing field (a bare NIK, a name, or 'NIK 33..'),
    store it. Returns True when a field was captured.

    Only fills fields that are currently MISSING — a typed value never silently
    overwrites a value already read from the KTP (a CORRECT at CONFIRM does that
    explicitly).
    """
    msg = (message or "").strip()
    changed = False

    if not sess.fields.get("nik"):
        m = _NIK_RUN_RE.search(msg)
        if m:
            nik = _v_nik(m.group(1))
            if nik:
                sess.fields["nik"] = nik
                changed = True

    if not sess.fields.get("applicant_name"):
        # Try a "nama ..." pattern first; else, if the whole message reads like
        # a plain name (and isn't just a NIK), take it as the name.
        mn = _NAME_CORRECTION_RE.search(msg)
        candidate = mn.group(1) if mn else msg
        candidate = _name_candidate_cleanup(candidate)
        # Don't treat a pure-digits message (a typed NIK) as a name.
        if candidate and not re.fullmatch(r"[\d .\-]+", candidate.strip()):
            cleaned = _v_name(candidate)
            if cleaned:
                sess.fields["applicant_name"] = cleaned
                changed = True

    return changed


def _name_candidate_cleanup(candidate: str) -> str:
    """Trim a name candidate so a co-located NIK / second clause doesn't bleed
    into the name. "Andi Wijaya, NIK 3312..." -> "Andi Wijaya".

    Cuts at the first comma, semicolon, or a 'NIK'/digit-run boundary — names
    don't contain those, so this only ever removes accidental run-on.
    """
    s = (candidate or "").strip()
    # Cut at an explicit separator first.
    s = re.split(r"[,;\n]", s, maxsplit=1)[0]
    # Cut before a 'NIK' clause or a long digit run.
    s = re.split(r"\b(?:nik|nomor)\b", s, maxsplit=1, flags=re.IGNORECASE)[0]
    s = re.split(r"\d{6,}", s, maxsplit=1)[0]
    return s.strip(" .-")


# ===========================================================================
# RESOLVING_LICENSE handler.
# ===========================================================================

def _handle_license_choice(sess: SubmissionSession, message: str) -> Optional[dict]:
    """At RESOLVING_LICENSE with candidates: parse the citizen's numeric pick."""
    if not sess.candidates:
        return None
    m = _NUMERIC_PICK_RE.match(message.strip())
    if not m:
        return None
    idx = int(m.group(1))
    if 1 <= idx <= len(sess.candidates):
        return sess.candidates[idx - 1]
    return None


# ===========================================================================
# Public entry point — called by ai_handler as a FAST-PATH.
# ===========================================================================

# --------------------------------------------------------------------------- #
# PPKP doc-prep co-pilot (curated licences)
#
# After a curated licence is locked, BIMA collects the data its sign-required
# documents need, drafts them with `doc_generator`, delivers them as WhatsApp
# *document* messages (hosted at /dl/{token}), and hands off to Mekari for
# e-signature + e-meterai — THEN rejoins the normal upload / score / submit
# flow. Everything here is gated behind `license_guides.get_guide(...)`, so a
# non-curated submission never enters this path.
# --------------------------------------------------------------------------- #

# Data the generated docs need, in the sess.fields key convention (shared with
# the applicant extraction, so name/NIK collected here pre-fill the KTP step).
_DOC_REQUIRED_FIELDS = [
    "applicant_name", "nik", "jabatan", "business_name", "alamat",
    "nama_kapal", "galangan",
]
_DOC_OPTIONAL_FIELDS = ["gt_kapal", "bahan_kapal"]
_DOC_FIELD_LABELS = {
    "applicant_name": "Nama lengkap (sesuai KTP)",
    "nik": "NIK (16 digit)",
    "jabatan": "Jabatan (mis. Direktur / Pemilik)",
    "business_name": "Nama perusahaan",
    "alamat": "Alamat perusahaan",
    "nama_kapal": "Nama kapal",
    "galangan": "Nama galangan pembuat kapal",
    "gt_kapal": "Ukuran kapal (GT)",
    "bahan_kapal": "Bahan kapal (Baja / Kayu / Fiber)",
}

_DOC_EXTRACT_SYSTEM = (
    "Anda mengekstrak data dari pesan warga untuk mengisi formulir izin. "
    "Kembalikan HANYA satu objek JSON, tanpa penjelasan dan tanpa code fence. "
    "Sertakan sebuah kunci HANYA jika nilainya jelas tersurat di pesan; bila "
    "tidak ada, jangan sertakan kuncinya (jangan mengarang). Kunci yang mungkin: "
    "applicant_name (nama orang), nik (16 digit angka), jabatan, business_name "
    "(nama perusahaan/usaha), alamat, nama_kapal, gt_kapal (ukuran GT), "
    "bahan_kapal, galangan (nama galangan kapal)."
)


def _public_base_url() -> str:
    return os.getenv("BIMA_PUBLIC_BASE_URL", "https://nolongin.com").rstrip("/")


def _mekari_sign_url() -> str:
    # Stub deep-link for the Mekari e-sign + e-meterai hand-off. The real
    # integration lands later; the demo shows the intended flow via a
    # configurable URL.
    return os.getenv("BIMA_MEKARI_SIGN_URL", "https://mekari.com/esign").rstrip("/")


def _fmt_doc_prep_intro(sess: SubmissionSession, guide: dict[str, Any]) -> str:
    gen = guide.get("generate_docs") or []
    up = guide.get("upload_docs") or []
    lines = [
        f"Baik Bapak/Ibu, untuk *{sess.license_name}* ada "
        f"{len(gen) + len(up)} persyaratan. Kabar baiknya, {len(gen)} dokumen "
        "bisa *saya buatkan drafnya* sekarang:",
        "",
    ]
    lines += [f"{i}. {d['label']}" for i, d in enumerate(gen, 1)]
    lines += ["", f"dan {len(up)} dokumen tinggal Bapak/Ibu unggah nanti:"]
    lines += [f"{i}. {d['label']}" for i, d in enumerate(up, 1)]
    lines += [
        "",
        "Untuk membuat draf dokumennya, saya perlu beberapa data. Boleh kirim "
        "sekaligus dalam satu pesan:",
    ]
    lines += [f"- {_DOC_FIELD_LABELS[k]}" for k in _DOC_REQUIRED_FIELDS + _DOC_OPTIONAL_FIELDS]
    return "\n".join(lines)


def _missing_doc_fields(sess: SubmissionSession) -> list[str]:
    return [k for k in _DOC_REQUIRED_FIELDS if not str(sess.fields.get(k) or "").strip()]


def _ask_doc_fields(sess: SubmissionSession, missing: list[str]) -> str:
    labels = [_DOC_FIELD_LABELS[k] for k in missing]
    if len(labels) == 1:
        return f"Terima kasih. Tinggal satu lagi: *{labels[0]}*?"
    listed = "\n".join(f"- {lab}" for lab in labels)
    return (
        "Terima kasih. Masih ada data yang saya perlukan untuk melengkapi "
        f"dokumennya:\n{listed}"
    )


async def _extract_doc_fields(text: str, sess: SubmissionSession) -> dict[str, str]:
    """Pull whatever doc fields the citizen just typed, via a JSON-only LLM call.
    Returns only keys clearly present (never invents). Logs no field values."""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        from services.ai_handler import _call_gemma, _strip_json_fences

        raw = await _call_gemma(
            _DOC_EXTRACT_SYSTEM,
            text[:1500],
            max_tokens=256,
            temperature=0.0,
            disable_thinking=True,
        )
        data = json.loads(_strip_json_fences(raw))
    except Exception:
        logger.warning("doc-field extraction failed | user=%s", _mask(sess.user_id))
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k in _DOC_REQUIRED_FIELDS + _DOC_OPTIONAL_FIELDS:
        v = data.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            out[k] = str(v).strip()
    if "nik" in out:  # NIK must be 16 digits, else drop it (never invent identity)
        digits = re.sub(r"\D", "", out["nik"])
        if len(digits) == 16:
            out["nik"] = digits
        else:
            out.pop("nik", None)
    return out


async def _send_doc_to_user(
    user_id: str, link: str, filename: str, *, caption: str = ""
) -> bool:
    """Deliver a generated PDF on the citizen's channel — WhatsApp document, or a
    text link on Telegram (no doc-send wired there yet). Never raises."""
    try:
        if user_id.startswith("wa-"):
            from services.whatsapp_sender import send_document

            return await send_document(
                recipient_phone=user_id[3:], link=link, filename=filename,
                caption=caption or None,
            )
        if user_id.startswith("tg-"):
            return await _send_to_user(user_id, f"{caption}\n{link}".strip())
    except Exception:
        logger.exception("doc send failed | user=%s", _mask(user_id))
    return False


async def _render_doc_content(
    sess: "SubmissionSession", doc_type: str, label: str, data: dict, nik: str
) -> Optional[tuple[bytes, str, str]]:
    """Render one sign-doc, preferring the OFFICIAL SIAP template.

    1. If SIAP hosts a fillable template for this doc_type (or BIMA hand-builds
       the legacy .doc one), deliver an editable DOCX with identity fields
       pre-filled — NOT encrypted (it is meant to be opened + filled).
    2. Otherwise fall back to the fpdf2 PDF, AES-locked with the citizen's NIK.

    Returns (content_bytes, filename, lock_note) or None on total failure.
    """
    try:
        from services import siap_templates
        official = await siap_templates.render_official_docx(
            sess.license_id, doc_type, sess.fields
        )
    except Exception:
        logger.exception("official template render failed | user=%s doc=%s",
                         _mask(sess.user_id), doc_type)
        official = None
    if official is not None:
        content, out_name = official
        return content, out_name, ""  # editable DOCX — no NIK lock note

    from services import doc_generator
    try:
        pdf = doc_generator.generate(doc_type, data, encrypt_password=nik or None)
    except Exception:
        logger.exception("doc generate failed | user=%s doc=%s",
                         _mask(sess.user_id), doc_type)
        return None
    safe_label = label.replace(" ", "_").replace("/", "-")
    lock = " (terkunci - buka dengan NIK Anda)" if nik else ""
    return pdf, f"{safe_label}.pdf", lock


async def _generate_and_send_docs(sess: SubmissionSession) -> str:
    """All required data present: draft the docs, deliver them, hand off to
    Mekari, and rejoin COLLECTING_DOCS for the signed + uploaded set."""
    from services import doc_generator, generated_docs, license_guides

    guide = license_guides.get_guide(sess.license_id) or {}
    data = {k: sess.fields.get(k, "") for k in (_DOC_REQUIRED_FIELDS + _DOC_OPTIONAL_FIELDS)}
    data["license_name"] = sess.license_name or ""

    base = _public_base_url()
    # The PDF is AES-encrypted with the citizen's NIK (they know it; it is never
    # transmitted), so a leaked /dl link is an unreadable file. The filename is
    # neutral — no name/NIK in the URL or the Content-Disposition header.
    nik = (sess.fields.get("nik") or "").strip()
    sent = 0
    any_locked = False  # only the fpdf2-PDF fallback is NIK-encrypted; DOCX is not
    for d in (guide.get("generate_docs") or []):
        rendered = await _render_doc_content(sess, d["doc_type"], d["label"], data, nik)
        if rendered is None:
            continue
        content, filename, lock = rendered
        any_locked = any_locked or bool(lock)
        token = generated_docs.store(content, filename)  # mime inferred from ext
        if await _send_doc_to_user(
            sess.user_id, f"{base}/dl/{token}", filename,
            caption=f"Draf {d['label']}{lock}.",
        ):
            sent += 1

    sess.stage = Stage.COLLECTING_DOCS
    sess.touch()
    await _put_session(sess)
    logger.info(
        "Guided-submission docs drafted | user=%s | license_id=%s | sent=%d",
        _mask(sess.user_id), sess.license_id, sent,
    )

    up = guide.get("upload_docs") or []
    up_list = "\n".join(f"{i}. {d['label']}" for i, d in enumerate(up, 1))
    lead = (
        f"Selesai - {sent} draf dokumen sudah saya kirim di atas. "
        if sent
        else "Maaf, ada kendala saat mengirim draf dokumennya. Tim kami akan bantu. "
    )
    lock_note = (
        "_Demi keamanan data Anda, dokumennya terkunci - buka dengan *NIK* Anda "
        "(16 digit)._\n\n"
        if any_locked else ""
    )
    return (
        f"{lead}\n\n{lock_note}Langkah berikutnya:\n\n"
        "1) *Tanda tangani + bubuhi meterai Rp10.000* pada ketiga dokumen. "
        "Pilih cara yang paling mudah - keduanya sama-sah secara hukum:\n"
        f"   - *Digital*: tanda tangan elektronik + e-meterai lewat Mekari: {_mekari_sign_url()}\n"
        "   - *Manual*: cetak ketiga dokumen, lalu di tiap dokumen bubuhkan "
        "*tanda tangan yang sama* (konsisten di semua dokumen) dan tempel "
        "*meterai Rp10.000 tersendiri* - tiap dokumen wajib meterainya sendiri "
        "(kode/seri tiap meterai berbeda); jangan pakai satu meterai untuk "
        "beberapa dokumen.\n"
        "2) Unggah kembali ketiga dokumen yang sudah ditandatangani (foto/scan "
        "yang jelas bila manual) ke sini.\n"
        f"3) Lengkapi juga {len(up)} dokumen berikut:\n{up_list}\n\n"
        "Kirim semua dokumennya di sini ya, Bapak/Ibu - saya periksa "
        "kelengkapannya sebelum diajukan ke SIAP."
    )


# --------------------------------------------------------------------------- #
# Single-doc generate-on-request ("buatkan surat permohonannya")
#
# After the packet is scored, the citizen may ask BIMA to DRAFT one of the
# sign-required documents it can make (e.g. a missing Surat Permohonan). This
# recognises "buatkan <doc>", generates that ONE PDF (encrypted with the NIK),
# delivers it, and asks the citizen to sign + meterai + re-upload. Requires BOTH
# a generate-verb AND a doc-name so raw data typed during doc-prep never fires.
# --------------------------------------------------------------------------- #

# Doc-name fragments → doc_type (lowercased substring). Distinctive multi-word
# phrases only; bare "permohonan"/"pesanan" are dropped — they collide with
# everyday words ("permohonan maaf", "pesanan makan") and with the licence-object
# detector. "pakta" is rare enough to keep bare.
_DOC_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "pakta_integritas": ("pakta integritas", "pakta"),
    "surat_permohonan": ("surat permohonan",),
    "surat_pesanan": ("surat pesanan", "kontrak galangan", "kontrak dengan galangan"),
}

# Only IMPERATIVE / explicitly-requested generate verbs — "buatkan/bikinin/
# dibuatkan" or "tolong|minta|mohon buat". Deliberately NOT bare "buat"/"bikin",
# which trip on "buat apa" (for what), "mau buat izin" (a new submission), and
# "sudah buat" (already made).
_GEN_VERB_RE = re.compile(
    r"\b(?:di)?(?:buatkan|buatin|bikinkan|bikinin)\b"
    r"|\b(?:tolong|minta|mohon)\s+(?:di)?(?:buat|bikin)(?:kan|in)?\b"
    r"|\bgenerate(?:kan)?\b",
    re.IGNORECASE,
)

# Cues that a doc mention is NOT a generate request — "sudah/udah/telah [di]buat"
# (already made), "sendiri" (made it myself), or an interrogative about the doc.
_NOT_A_REQUEST_RE = re.compile(
    r"\b(sudah|udah|telah|sendiri|kenapa|mengapa|apa\s+itu|buat\s+apa|untuk\s+apa)\b",
    re.IGNORECASE,
)


def _match_generate_doc_request(msg: str, guide: Optional[dict[str, Any]]) -> Optional[str]:
    """If `msg` is a 'buatkan <doc>' request for a doc THIS guide can generate,
    return that doc_type, else None. Requires an imperative generate-verb AND a
    doc-name hit, and rejects 'already made / question' cues, so plain data,
    questions ("surat permohonan buat apa?"), a new-submission switch, and
    "sudah saya buat sendiri" never trigger. Longest fragment wins; a tie between
    two different docs bails out (ask to clarify)."""
    if not guide:
        return None
    low = (msg or "").strip().lower()
    if not low or not _GEN_VERB_RE.search(low):
        return None
    if _NOT_A_REQUEST_RE.search(low):
        return None
    generatable = {d.get("doc_type") for d in (guide.get("generate_docs") or [])}
    hits: list[tuple[int, str]] = []
    for doc_type, aliases in _DOC_NAME_ALIASES.items():
        if doc_type not in generatable:
            continue
        for frag in aliases:
            if frag in low:
                hits.append((len(frag), doc_type))
    if not hits:
        return None
    hits.sort(reverse=True)
    best_len = hits[0][0]
    top = {d for length, d in hits if length == best_len}
    return hits[0][1] if len(top) == 1 else None


async def _generate_and_send_one_doc(sess: SubmissionSession, doc_type: str) -> str:
    """Draft ONE sign-required doc on request, deliver it (encrypted with the
    NIK), and tell the citizen to sign + meterai + upload it back. Mirrors
    _generate_and_send_docs for a single doc; does NOT change the stage. Missing
    data keys render as fill-by-hand blanks — never invented."""
    from services import doc_generator, generated_docs, license_guides

    guide = license_guides.get_guide(sess.license_id) or {}
    entry = next(
        (d for d in (guide.get("generate_docs") or []) if d.get("doc_type") == doc_type),
        None,
    )
    if entry is None:  # defensive — the matcher only returns generatable docs
        names = ", ".join(d["label"] for d in (guide.get("generate_docs") or [])) or "-"
        return f"Saya bisa membuatkan: {names}. Dokumen mana yang ingin dibuatkan?"

    data = {k: sess.fields.get(k, "") for k in (_DOC_REQUIRED_FIELDS + _DOC_OPTIONAL_FIELDS)}
    data["license_name"] = sess.license_name or ""
    # Sensible default: a Usaha Perorangan's signatory is the owner.
    if not data.get("jabatan") and sess.fields.get("jenis_usaha") == "Perorangan":
        data["jabatan"] = "Pemilik"
    nik = (sess.fields.get("nik") or "").strip()

    label = entry.get("label") or doc_type.replace("_", " ").title()
    rendered = await _render_doc_content(sess, doc_type, label, data, nik)
    if rendered is None:
        return "Maaf, ada kendala saat menyiapkan drafnya. Tim kami akan bantu."
    content, filename, lock = rendered
    token = generated_docs.store(content, filename)  # mime inferred from ext
    sent = await _send_doc_to_user(
        sess.user_id, f"{_public_base_url()}/dl/{token}", filename,
        caption=f"Draf {label}{lock}.",
    )
    logger.info(
        "Single-doc drafted | user=%s | license_id=%s | doc=%s | sent=%s",
        _mask(sess.user_id), sess.license_id, doc_type, sent,
    )
    if not sent:
        return "Maaf, ada kendala saat mengirim drafnya. Tim kami akan bantu."
    lock_note = (
        "_Dokumen terkunci - buka dengan *NIK* Anda (16 digit)._\n\n" if lock else ""
    )
    return (
        f"Selesai - draf *{label}* sudah saya kirim di atas.\n\n{lock_note}"
        "Silakan periksa/lengkapi bagian yang masih kosong, *tanda tangani + "
        "bubuhi meterai Rp10.000*, lalu *unggah kembali* ke sini agar berkasnya "
        "lengkap."
    )


async def _handle_preparing_docs(sess: SubmissionSession, msg: str) -> str:
    """Collect the data the generated docs need; once complete, draft + deliver."""
    text = (msg or "").strip()
    missing_before = _missing_doc_fields(sess)

    extracted = await _extract_doc_fields(text, sess)
    for k, v in extracted.items():
        sess.fields[k] = v

    missing_now = _missing_doc_fields(sess)
    # Single-field follow-up the LLM didn't catch: take the short reply verbatim
    # (never for NIK — it needs 16 validated digits).
    if (
        len(missing_before) == 1
        and missing_before == missing_now
        and text
        and missing_before[0] != "nik"
        and len(text) <= 60
    ):
        sess.fields[missing_before[0]] = text
        missing_now = _missing_doc_fields(sess)

    sess.touch()
    if missing_now:
        await _put_session(sess)
        return _ask_doc_fields(sess, missing_now)
    return await _generate_and_send_docs(sess)


async def maybe_handle(user_id: str, message: str) -> Optional[str]:
    """Guided-submission FAST-PATH for `ai_handler`.

    Returns a reply string when the message belonged to (or started) a guided
    submission; None when ai_handler should proceed normally. Engages only when
    `BIMA_GUIDED_SUBMISSION_ENABLED` is on. NEVER raises into the chat path.
    """
    if not is_enabled():
        return None

    msg = (message or "").strip()
    sess = await _get_session(user_id)

    # --- No active session: only engage on a fresh submission intent ----
    if sess is None or sess.stage in (Stage.DONE, Stage.FAILED):
        if detect_submission_intent(msg):
            logger.info("Guided-submission intent detected — starting flow | user=%s", _mask(user_id))
            try:
                return await _start_session(user_id, msg)
            except Exception:
                logger.exception("Guided-submission start crashed | user=%s", _mask(user_id))
                await _clear_session(user_id)
                return None
        return None

    # --- Active session ---
    from services import submission_intent

    # Strong literal cancel ('batal'/'cancel'/'stop') works at ANY stage,
    # regardless of the LLM (the owner kept 'batal' as a hard decline).
    if submission_intent.is_hard_cancel(msg):
        await _clear_session(user_id)
        logger.info("Guided-submission cancelled (hard) | user=%s", _mask(user_id))
        return (
            "Baik Bapak/Ibu, pengajuan izin dibatalkan. Kapan pun ingin "
            "mengajukan lagi, cukup beri tahu saya."
        )

    # --- Single-doc generate request ("buatkan surat permohonannya") --------
    # A curated-licence citizen at COLLECTING_DOCS / CONFIRM asking BIMA to DRAFT
    # a sign-doc it can make. MUST run BEFORE the new-intent intercept below —
    # "tolong buatkan surat permohonan" also trips detect_submission_intent, so
    # the intercept would otherwise offer to switch instead of generating. Gated
    # on no pending switch. The matcher needs BOTH a generate-verb AND a
    # generatable-doc name, so a genuine "saya mau ajukan izin lain" (no doc
    # name) falls through to the intercept. Without this the message got NO reply.
    if sess.pending_new_intent is None and sess.stage in (
        Stage.COLLECTING_DOCS, Stage.CONFIRM
    ):
        from services import license_guides
        target = _match_generate_doc_request(
            msg, license_guides.get_guide(sess.license_id)
        )
        if target is not None:
            try:
                return await _generate_and_send_one_doc(sess, target)
            except Exception:
                logger.exception(
                    "Single-doc generate crashed | user=%s | doc=%s",
                    _mask(user_id), target,
                )
                return (
                    "Mohon maaf, ada kendala saat membuat dokumennya. Coba lagi "
                    "sebentar ya, Bapak/Ibu."
                )

    # New-submission intent INTERCEPT while a form is ACTIVE (COLLECTING_DOCS /
    # CONFIRM, NOT RESOLVING_LICENSE): a clear "saya mau ajukan izin ..." must
    # not be eaten as a correction/answer. Offer to switch; stash the text.
    if sess.stage in (Stage.COLLECTING_DOCS, Stage.CONFIRM) and detect_submission_intent(msg):
        sess.pending_new_intent = msg
        sess.touch()
        await _put_session(sess)
        logger.info(
            "Guided-submission new-intent intercepted mid-form | user=%s | stage=%s",
            _mask(user_id), sess.stage.value,
        )
        return (
            f"Bapak/Ibu sedang mengajukan {sess.license_name}. Jika ingin "
            "mengganti dengan pengajuan yang baru, beri tahu saya \"ya, ganti\"; "
            "atau lanjutkan saja yang sekarang."
        )

    # If a new-intent was previously stashed and the citizen now confirms the
    # switch, re-resolve from the stashed text (a fresh session).
    if sess.pending_new_intent is not None:
        pending = sess.pending_new_intent
        low = (msg or "").strip().lower()
        # BIMA explicitly told the citizen to reply "ganti" / "lanjutkan" (and
        # earlier "ya, ganti"), so honour those literal words FIRST — the generic
        # AFFIRM/DECLINE classifier doesn't map "ganti"/"lanjutkan" and looped the
        # disambiguation forever ("ya, ganti" -> re-ask -> "ganti" -> re-ask ...).
        if re.search(r"\bganti\b", low):
            switch_intent = submission_intent.AFFIRM
        elif re.search(r"\blanjut", low) or re.search(r"\btetap\b", low):
            switch_intent = submission_intent.DECLINE
        else:
            switch_intent = await submission_intent.classify_confirm_intent(msg)
        if switch_intent == submission_intent.AFFIRM:
            logger.info(
                "Guided-submission switching to stashed new intent | user=%s",
                _mask(user_id),
            )
            try:
                return await _start_session(user_id, pending)
            except Exception:
                logger.exception("Guided-submission switch crashed | user=%s", _mask(user_id))
                await _clear_session(user_id)
                return None
        if switch_intent == submission_intent.DECLINE:
            # They chose to continue the current form — clear the stash + re-prompt.
            sess.pending_new_intent = None
            sess.touch()
            await _put_session(sess)
            # Fall through to the normal stage handler below with the cleared stash.
        else:
            # Ambiguous about the switch — keep the stash, ask plainly.
            return (
                f"Untuk memastikan: apakah Bapak/Ibu ingin mengganti ke "
                "pengajuan yang baru, atau melanjutkan "
                f"{sess.license_name} yang sekarang? Balas \"ganti\" atau "
                "\"lanjutkan\"."
            )

    try:
        if sess.stage == Stage.RESOLVING_LICENSE:
            choice = _handle_license_choice(sess, msg)
            if choice is not None:
                return await _lock_license(sess, choice)
            if sess.candidates:
                return (
                    "Boleh pilih dengan membalas salah satu nomor dari daftar "
                    "di atas ya, Bapak/Ibu."
                )
            # No candidates pending → treat the message as a fresh licence query.
            return await _start_session(user_id, msg)

        if sess.stage == Stage.PREPARING_DOCS:
            return await _handle_preparing_docs(sess, msg)

        if sess.stage == Stage.COLLECTING_DOCS:
            return await _handle_collecting_docs_text(sess, msg)

        if sess.stage == Stage.CONFIRM:
            return await _handle_confirm(sess, msg)
    except Exception:
        logger.exception(
            "Guided-submission handler crashed | user=%s | stage=%s",
            _mask(user_id), sess.stage,
        )
        await _clear_session(user_id)
        return (
            "Mohon maaf, terjadi gangguan saat memproses pengajuan Bapak/Ibu. "
            "Pengajuan dibatalkan — silakan mulai lagi dengan menyebutkan izin "
            "yang ingin diajukan."
        )

    # Unknown stage — clear and let ai_handler handle the message.
    await _clear_session(user_id)
    return None
