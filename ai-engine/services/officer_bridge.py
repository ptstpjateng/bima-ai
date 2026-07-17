"""
Officer chat bridge — the officer/pokja/kepala UX over WhatsApp/Telegram.

This module is the genuinely-new piece of the June-4 slice. Until now the
Officer Copilot (`services/agents/officer_copilot.py`) only spoke through the
bima-admin dashboard (`POST /v1/copilot/chat`, called by admin-api). The
product vision is that the officer interacts with BIMA *in chat* — the same
WhatsApp/Telegram surface the citizen uses — not a separate portal.

So this module changes ONLY the transport. It does two things:

  1. notify_officer_of_submission()
     On a real SIAP request creation (called fire-and-forget from
     `guided_submission._submit`), send the validating officer a chat brief:
       • license + ticket + masked applicant
       • the BIMA content-score (the suitability-judge result)
       • "balas untuk mulai memeriksa"
     and register an in-memory officer-case session so the officer's replies
     route into the copilot. Feature-flagged + degrades to a no-op.

  2. maybe_handle_officer_reply()
     A FAST-PATH the inbound routers (routers/aptana.py, routers/telegram.py)
     call BEFORE the citizen AI. When the sender is a configured officer with
     an active case session, the message is routed into
     `officer_copilot.chat()` — injecting the score computed at submit (so
     `get_validation_summary` works) and the in-session document bytes (so
     `get_doc_summary` answers with real Gemini Vision). The copilot's
     confirm-gated `forward_case` / `record_decision` write tools are reused
     verbatim — typing "ya" forwards, a rejection note routes back one desk.
     Returns None when the sender isn't an officer-in-session, so the routers
     fall through to the normal citizen path.

SESSION STATE — in-memory, per officer-channel-id.
  admin-api owns durable copilot sessions (the `copilot_session` table); for
  the chat bridge an in-memory dict keyed by the officer's channel id is
  acceptable for the demo (same call as guided_submission._sessions). A
  restart drops in-flight officer chats — a documented follow-up.

FEATURE FLAG
  BIMA_OFFICER_NOTIFY_ENABLED (default "false"). While off,
  notify_officer_of_submission() is a no-op and maybe_handle_officer_reply()
  still works ONLY if a session somehow exists (it won't, since notify is the
  only thing that creates one) — so effectively the whole bridge is dark until
  the flag is flipped. Safe to merge before the demo officer number is wired.

OFFICER RESOLUTION — flow-based ([[BIMA Officer Notify Model]]).
  WHO gets notified is DATA, not config. On submission we resolve the
  officer(s) from SIAP's "Alur Izin": the license_request's CURRENT
  approval step → its owning role (group_id) + `properties.wa_notification`
  → the users assigned to that step on this izin (Privilege Izin ∩ the role).
  See services/siap_db.resolve_step_officers. Each resolved officer gets their
  own OfficerCaseSession (keyed by their normalized WhatsApp).

  BIMA_OFFICER_WA_PHONE / BIMA_OFFICER_TG_CHAT are DEMOTED to a fallback:
  used only when flow resolution yields no officer-with-a-number (wa not
  active, applicant step, no assignee, or a SIAP error). BIMA_OFFICER_NOTIFY_ENABLED
  still gates all sending.

  Inbound officer identification is likewise data-driven: a warm, TTL'd cache
  of every officer WhatsApp (services/siap_db.get_officer_directory) lets any
  registered petugas chat as themselves — no env allowlist editing.

PII
  Applicant name is masked in the brief and in all logs. Document bytes are
  never logged. The officer's own channel id is masked in logs. WhatsApp
  numbers, NIK, and names are never logged unmasked.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

from dotenv import load_dotenv

from services import session_store
from services.pii import mask_pii

load_dotenv()

logger = logging.getLogger("bima_ai.officer_bridge")


# ===========================================================================
# Feature flag + demo officer configuration
# ===========================================================================


def is_enabled() -> bool:
    """Master flag for the officer chat bridge. Default OFF."""
    return os.getenv("BIMA_OFFICER_NOTIFY_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _demo_officer_wa() -> str:
    """WhatsApp msisdn of the demo validating officer (blank → not on WA)."""
    return os.getenv("BIMA_OFFICER_WA_PHONE", "").strip()


def _demo_officer_tg() -> str:
    """Telegram chat id of the demo validating officer (blank → not on TG)."""
    return os.getenv("BIMA_OFFICER_TG_CHAT", "").strip()


# The SIAP signing magic-link the LAST-step officer (Kepala Dinas) receives.
# BIMA does NOT sign — SIAP owns TTE/BSRE — so the final step is a handoff: we
# send a link that opens SIAP's signing surface for THIS case. It is a single
# env-configurable Python format string with `{request_id}` and/or `{ticket}`
# placeholders. The exact SIAP signing URL pattern is still TBD (the SIAP team
# will supply it); until then the default points at SIAP's known "Tanda Tangan
# Berkas" Filament page pre-filtered by ticket (the same surface the existing
# copilot `get_siap_signing_link` tool builds), which is a working rehearsal
# link rather than a dead placeholder.
# TODO: user to confirm the exact SIAP signing URL (per-case deep link).
_SK_SIGN_URL_TEMPLATE = os.getenv(
    "BIMA_SK_SIGN_URL_TEMPLATE",
    "https://beta-siap.bimaptsp.com/admin/tanda-tangan-berkas?tableSearch={ticket}",
).strip()


def _build_sk_sign_url(*, request_id: Optional[int], ticket: Optional[str]) -> Optional[str]:
    """Render the SIAP signing magic-link from the env template. Never raises.

    Supports `{request_id}` and `{ticket}` placeholders; a template that
    references neither still renders (a static URL). Returns None when the
    template is blank OR a referenced field is missing, so the caller degrades
    to an honest "link belum tersedia" rather than emitting a broken URL."""
    tmpl = _SK_SIGN_URL_TEMPLATE
    if not tmpl:
        return None
    tkt = ""
    if ticket:
        digits = "".join(ch for ch in str(ticket) if ch.isdigit())
        tkt = digits.zfill(9) if digits else str(ticket).strip()
    rid = "" if request_id is None else str(request_id)
    try:
        url = tmpl.format(request_id=rid, ticket=tkt)
    except (KeyError, IndexError, ValueError):
        logger.warning("SK sign-url template has an unknown placeholder — skipping link")
        return None
    # A placeholder that resolved to empty means the datum we needed is missing.
    if ("{request_id}" in tmpl and not rid) or ("{ticket}" in tmpl and not tkt):
        return None
    return url


# ===========================================================================
# Number normalization
#
# APTANA delivers the inbound sender as `phoneNumber` in E.164-without-+ form
# (e.g. "6285117557091"). SIAP stores `public.users.whatsapp` as `62…` (no +),
# though some rows are `08…`. To match an inbound channel_id against a
# SIAP-stored / env-configured number we canonicalize BOTH sides to one form:
# strip non-digits, a leading 0 → 62, otherwise ensure a 62 prefix. This
# mirrors services.whatsapp_sender.normalize_phone (and siap_db._normalize_msisdn);
# kept local so the request-path helpers carry no outbound-sender import.
# Telegram chat ids are pure digits already and pass through unchanged.
# ===========================================================================


def _normalize_wa(value: Optional[str]) -> str:
    """Canonicalize a WhatsApp number to APTANA's inbound form (62…).

    Returns "" for empty/garbage input. Idempotent: 08…, +62…, 62… all
    collapse to the same 62… string.
    """
    if not value:
        return ""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return ""
    if digits.startswith("0"):
        return "62" + digits[1:]
    if digits.startswith("62"):
        return digits
    return "62" + digits


# ===========================================================================
# Inbound officer-identification cache — data-driven ([[BIMA Officer Notify Model]]).
#
# is_officer_channel_id() is called SYNCHRONOUSLY in the request path
# (routers/aptana.py media branch, maybe_handle_officer_reply). It must NOT do
# a blocking DB call. So we maintain an in-memory SET of normalized officer
# WhatsApp numbers with a TTL: is_officer_channel_id reads the warm set
# (membership only); a stale cache triggers a NON-BLOCKING background refresh
# while still answering from the last-known set. The cache is warmed at startup
# (main.py lifespan → warm_officer_cache).
# ===========================================================================

_OFFICER_CACHE_TTL_SECONDS = 300.0  # 5 min — officer roster is near-static

# Last-known normalized officer numbers (from siap_db.get_officer_directory).
_officer_cache: set[str] = set()
_officer_cache_at: float = 0.0          # monotonic timestamp of last refresh
_officer_cache_loaded: bool = False     # has a refresh ever completed?
_officer_refresh_inflight: bool = False  # guards against piling up refreshes


def _officer_cache_is_stale() -> bool:
    return (time.monotonic() - _officer_cache_at) > _OFFICER_CACHE_TTL_SECONDS


async def _refresh_officer_cache() -> None:
    """Reload the officer directory from SIAP into the in-memory cache.

    Never raises. On a SIAP error the LAST-KNOWN set is preserved (we only
    overwrite on a successful, non-empty-or-empty fetch — siap_db already
    returns [] safely on error, but we distinguish 'fetched' from 'failed' by
    catching here and leaving the set untouched on exception)."""
    global _officer_cache, _officer_cache_at, _officer_cache_loaded
    global _officer_refresh_inflight
    if _officer_refresh_inflight:
        return
    _officer_refresh_inflight = True
    try:
        from services.siap_db import get_officer_directory

        numbers = await get_officer_directory()  # already normalized + deduped
        _officer_cache = {n for n in numbers if n}
        _officer_cache_at = time.monotonic()
        _officer_cache_loaded = True
        logger.info("officer cache refreshed | officers=%d", len(_officer_cache))
    except Exception:
        # Preserve last-known set; surface the miss without PII.
        logger.exception("officer cache refresh failed (keeping last-known set)")
    finally:
        _officer_refresh_inflight = False


async def warm_officer_cache() -> None:
    """Startup hook (main.py lifespan): populate the officer cache once so the
    first inbound officer message hits a warm set. Never raises."""
    await _refresh_officer_cache()


def _maybe_kick_officer_refresh() -> None:
    """If the cache is stale (or never loaded), schedule a NON-BLOCKING refresh
    on the running event loop. Safe to call from the sync request path: it only
    creates a task, never awaits. No-op if there's no running loop (e.g. unit
    tests calling the sync predicate outside an event loop)."""
    if _officer_refresh_inflight:
        return
    if _officer_cache_loaded and not _officer_cache_is_stale():
        return
    try:
        import asyncio

        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no event loop (sync test context) — skip; cache stays as-is
    loop.create_task(_refresh_officer_cache())


def _reset_officer_cache() -> None:
    """Test helper — clear the officer cache so each test starts cold."""
    global _officer_cache, _officer_cache_at, _officer_cache_loaded
    global _officer_refresh_inflight
    _officer_cache = set()
    _officer_cache_at = 0.0
    _officer_cache_loaded = False
    _officer_refresh_inflight = False


# ===========================================================================
# Officer-case session — in-memory, keyed by the officer's channel id.
#
# The channel id is the SAME id the inbound routers key conversations on:
#   WhatsApp → the msisdn (e.g. "6285117557091")
#   Telegram → the chat id as a string (e.g. "12345678")
# We store the channel so the reply goes back the way it came.
# ===========================================================================

CHANNEL_WHATSAPP = "whatsapp"
CHANNEL_TELEGRAM = "telegram"


@dataclass
class OfficerCaseSession:
    """One officer reviewing one ticket over chat."""

    channel_id: str                       # officer's msisdn or tg chat id (str)
    channel: str                          # CHANNEL_WHATSAPP | CHANNEL_TELEGRAM
    ticket: str
    request_id: Optional[int] = None
    license_id: Optional[int] = None
    license_name: Optional[str] = None
    # The applicant's SIAP profile id — the authoritative source for the generic
    # output-doc fill engine's APPLICANT-identity placeholders (nama_pemohon,
    # alamat, nik, …) read from ptsp.person_profile.properties. Carried so
    # draft_sk can ground identity on real profile columns for ANY licence.
    profile_id: Optional[int] = None
    # The current step's own `stereotype` (TTE / SURVEY-LAPANGAN / …) — a
    # secondary signal for per-step output-template selection (Rekomtek vs SK).
    step_stereotype: Optional[str] = None
    # Applicant identity for the SK draft (draft_sk). The officer is authorized
    # to see these; they go into the SK docx but are NEVER logged / masked here.
    applicant_name: Optional[str] = None
    alamat: Optional[str] = None
    # The validator/suitability score for `ticket`, already shaped for the
    # copilot's `get_validation_summary` (score_percent/status/summary/issues).
    validation: Optional[dict[str, Any]] = None
    # In-session document bytes keyed by file_id, for `get_doc_summary`.
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Compact per-document read digest (Task F) — {filename, detected_type,
    # has_meterai, confidence, matches, claimed_type} snapshotted from the rich
    # SuitabilityResult so "what BIMA read per doc" survives a restart even
    # though the raw result is stripped before Redis. Threaded into the copilot
    # via the validation dict; PII masked at build time.
    documents_digest: list[dict[str, Any]] = field(default_factory=list)
    # Rolling copilot history (list of {role, text}) so the conversation has
    # memory across the officer's messages.
    history: list[dict[str, str]] = field(default_factory=list)
    # SIAP step position ([[Multi-step Officer Copilot]]) — carried so the
    # bridge knows whether THIS officer sits at the LAST (SK-signing) desk. When
    # True the copilot is steered to hand off the SIAP signing magic-link rather
    # than recommend "forward". Populated by load_case_from_siap; absent (False)
    # for a first-step submit-time notify (which is never the final desk).
    is_final_step: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()


_MAX_OFFICER_SESSIONS = 200
_OFFICER_SESSION_TTL_SECONDS = 12 * 60 * 60  # 12h
_MAX_HISTORY_TURNS = 30  # mirror copilot router cap headroom
_sessions: "OrderedDict[str, OfficerCaseSession]" = OrderedDict()


# ---------------------------------------------------------------------------
# Redis serialization (durable sessions — services/session_store.py).
#
# The validation dict is already JSON-safe (it's the projected copilot shape,
# not the raw SuitabilityResult). The only binary is each document's `content`
# bytes inside the `documents` map → base64 on encode, bytes on decode. Bytes
# are NEVER logged (session_store logs only payload lengths).
#
# SIZE GUARD (Edge 3): all docs share ONE Redis blob. A single large doc could
# bloat that blob past Redis's value limit / maxmemory and make the whole SET
# fail — dropping EVERY doc's bytes, not just the big one, so "lihat KTP" would
# work but "kirim surat permohonan" wouldn't after a restart. So we cap the
# bytes carried inline per doc AND in aggregate: docs whose bytes fit are
# persisted in full (send_document works after a restart); docs over the cap
# (or once the running total is spent) persist metadata WITHOUT the bytes
# (content_b64=""). Those degrade gracefully — the read-digest still describes
# them, get_doc_summary/send_document say "berkas tidak lagi tersimpan" rather
# than silently failing. Smaller docs (the common KTP/surat-permohonan case)
# stay inline, so the headline lihat/kirim flow survives the round-trip.
# ---------------------------------------------------------------------------

# Per-doc + aggregate caps on the RAW bytes persisted inline to Redis. Chosen
# well under a typical Redis value ceiling with room for the rest of the blob;
# base64 inflates ~4/3, so ~1.5 MB raw → ~2 MB encoded per doc, ~6 MB total.
_MAX_REDIS_DOC_BYTES = int(os.getenv("BIMA_OFFICER_REDIS_DOC_MAX_BYTES", str(1_500_000)))
_MAX_REDIS_DOCS_TOTAL_BYTES = int(
    os.getenv("BIMA_OFFICER_REDIS_DOCS_TOTAL_BYTES", str(6_000_000))
)


def _encode_officer_session(sess: OfficerCaseSession) -> str:
    import json

    docs: dict[str, dict[str, Any]] = {}
    budget = _MAX_REDIS_DOCS_TOTAL_BYTES
    dropped = 0
    for fid, d in (sess.documents or {}).items():
        content = d.get("content") or b""
        size = len(content)
        inline = size <= _MAX_REDIS_DOC_BYTES and size <= budget
        if inline:
            budget -= size
        else:
            dropped += 1
        docs[fid] = {
            "filename": d.get("filename", fid),
            "mime_type": d.get("mime_type", "application/octet-stream"),
            "claimed_type": d.get("claimed_type", ""),
            "detected_type": d.get("detected_type", ""),
            # Oversize / over-budget → persist metadata, DROP the bytes. The
            # in-memory session keeps the real bytes; only the durable copy
            # degrades. Never log the bytes.
            "content_b64": (
                base64.b64encode(content).decode("ascii") if inline else ""
            ),
        }
    if dropped:
        logger.info(
            "officer session: %d doc(s) exceeded Redis inline cap — bytes "
            "dropped from durable copy (digest retained) | key=%s",
            dropped, _mask(sess.channel_id),
        )
    payload = {
        "channel_id": sess.channel_id,
        "channel": sess.channel,
        "ticket": sess.ticket,
        "request_id": sess.request_id,
        "license_id": sess.license_id,
        "license_name": sess.license_name,
        "profile_id": sess.profile_id,
        "step_stereotype": sess.step_stereotype,
        "applicant_name": sess.applicant_name,
        "alamat": sess.alamat,
        "validation": sess.validation,
        "documents": docs,
        "documents_digest": sess.documents_digest,
        "history": sess.history,
        "is_final_step": sess.is_final_step,
        "created_at": sess.created_at,
        "updated_at": sess.updated_at,
    }
    return json.dumps(payload)


def _decode_officer_session(blob: str) -> OfficerCaseSession:
    import json

    raw = json.loads(blob)
    docs = {
        fid: {
            "filename": d.get("filename", fid),
            "mime_type": d.get("mime_type", "application/octet-stream"),
            "claimed_type": d.get("claimed_type", ""),
            "detected_type": d.get("detected_type", ""),
            "content": base64.b64decode(d["content_b64"]) if d.get("content_b64") else b"",
        }
        for fid, d in (raw.get("documents") or {}).items()
    }
    return OfficerCaseSession(
        channel_id=str(raw["channel_id"]),
        channel=str(raw.get("channel", CHANNEL_WHATSAPP)),
        ticket=str(raw.get("ticket", "")),
        request_id=raw.get("request_id"),
        license_id=raw.get("license_id"),
        license_name=raw.get("license_name"),
        profile_id=raw.get("profile_id"),
        step_stereotype=raw.get("step_stereotype"),
        applicant_name=raw.get("applicant_name"),
        alamat=raw.get("alamat"),
        validation=raw.get("validation"),
        documents=docs,
        documents_digest=list(raw.get("documents_digest") or []),
        history=list(raw.get("history") or []),
        is_final_step=bool(raw.get("is_final_step", False)),
        created_at=float(raw.get("created_at", time.time())),
        updated_at=float(raw.get("updated_at", time.time())),
    )


async def _get_session(channel_id: str) -> Optional[OfficerCaseSession]:
    """Read an officer case: in-memory first, then Redis (rehydrating the LRU
    on a durable hit so a restarted process re-warms on first officer reply)."""
    sess = _sessions.get(channel_id)
    if sess is None:
        sess = await session_store.load(
            session_store.officer_key(channel_id), decode=_decode_officer_session
        )
        if sess is not None:
            _sessions[channel_id] = sess
            _sessions.move_to_end(channel_id)
    if sess is None:
        return None
    if time.time() - sess.updated_at > _OFFICER_SESSION_TTL_SECONDS:
        _sessions.pop(channel_id, None)
        await session_store.delete(session_store.officer_key(channel_id))
        return None
    return sess


async def _put_session(sess: OfficerCaseSession) -> None:
    """Write-through: in-memory LRU + best-effort Redis (TTL = session TTL)."""
    _sessions[sess.channel_id] = sess
    _sessions.move_to_end(sess.channel_id)
    while len(_sessions) > _MAX_OFFICER_SESSIONS:
        _sessions.popitem(last=False)
    saved = await session_store.save(
        session_store.officer_key(sess.channel_id),
        sess,
        encode=_encode_officer_session,
        ttl_seconds=_OFFICER_SESSION_TTL_SECONDS,
    )
    # When durable sessions are supposed to be ON but the write didn't land,
    # surface it: the in-memory copy is fine, but the case won't survive a
    # restart. Masked key only — never the payload. (session_store already
    # warns-once on the underlying Redis error; this makes the missed durable
    # write observable at the call site too.)
    if not saved and session_store.is_enabled():
        logger.warning(
            "officer session durable write missed (flag on) | key=%s",
            _mask(sess.channel_id),
        )


async def clear_session(channel_id: str) -> None:
    _sessions.pop(channel_id, None)
    await session_store.delete(session_store.officer_key(channel_id))


def _mask(value: Optional[str]) -> str:
    if not value:
        return "<none>"
    s = str(value)
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


# ===========================================================================
# Score → copilot validation shape
# ===========================================================================


def _score_to_validation(score: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Project the guided-submission content-score dict (which carries a
    SuitabilityResult under "result") onto the shape the copilot's
    `get_validation_summary` reads: {score_percent, status, summary, issues:
    [{severity, field, message, related_docs}]}.

    Returns None when there's no score (officer brief then says validation is
    pending, and `get_validation_summary` will report it gracefully).

    PII: the SuitabilityResult's issue titles/ids (and the flattened-fallback
    messages) can echo Gemini Vision evidence quoted from the citizen's
    documents — a KTP NIK, a phone number. We mask every free-text field HERE,
    at the single point where the score is projected into the officer-facing
    validation dict. That dict is what gets stored in the OfficerCaseSession,
    forwarded into the copilot (`get_validation_summary`, which the model
    narrates to the officer verbatim), and rendered into the brief — so
    masking once here covers all three officer surfaces, matching the standing
    rule that NIK/phone/name must be masked for the officer."""
    if not score or not isinstance(score, dict):
        return None

    issues: list[dict[str, Any]] = []
    result = score.get("result")
    if result is not None and getattr(result, "issues", None) is not None:
        # Rich path — map the SuitabilityResult.Issue objects. Both the title
        # and the id (which embeds the requirement text) are masked.
        for it in result.issues:
            issues.append({
                "severity": getattr(it, "severity", "") or "",
                "field": mask_pii(getattr(it, "id", "") or ""),
                "message": mask_pii(getattr(it, "title", "") or ""),
                "related_docs": [],
            })
    else:
        # Fallback — the flattened {severity, message} list.
        for it in score.get("issues") or []:
            if isinstance(it, dict):
                issues.append({
                    "severity": str(it.get("severity", "") or ""),
                    "field": "",
                    "message": mask_pii(str(it.get("message", "") or "")),
                    "related_docs": [],
                })

    return {
        "score_percent": int(score.get("score_percent", 0) or 0),
        "status": str(score.get("status", "unverified") or "unverified"),
        "summary": mask_pii(str(score.get("summary", "") or "")),
        "issues": issues,
    }


def _documents_digest(score: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive a COMPACT per-document read digest from the rich SuitabilityResult
    carried under score["result"] (Task F).

    The full SuitabilityResult (per-doc type_correctness + suitability findings)
    is stripped before Redis — guided_submission._score_for_redis drops
    "result", and this bridge otherwise persists only the flattened validation.
    So a restart loses the granular "what BIMA read per doc" view. We snapshot
    just the fields the officer needs — {filename, detected_type, has_meterai,
    confidence, matches, claimed_type} — from `result.type_correctness`, so it
    survives the Redis round-trip while staying tiny (no Vision evidence text,
    no bytes).

    PII: filenames/type labels are validator enums or citizen-supplied names;
    we mask them defensively via services.pii.mask_pii (matching the rule that
    NIK/phone must never reach the officer in the clear). Returns [] when the
    rich result is absent (flattened-only score, or no score at all).
    """
    if not score or not isinstance(score, dict):
        return []
    result = score.get("result")
    findings = getattr(result, "type_correctness", None)
    if not findings:
        return []
    digest: list[dict[str, Any]] = []
    for f in findings:
        confidence = getattr(f, "confidence", None)
        doc_name = getattr(f, "document_name", None)
        digest.append({
            # file_id is an opaque validator/admin-api handle (not PII) — kept
            # UNMASKED so the copilot can cross-reference the digest back to the
            # in-session doc bytes (detected_type → doc context) and so a
            # bytes-gone summary can still be tied to the right document.
            "file_id": str(getattr(f, "file_id", "") or ""),
            "filename": mask_pii(str(getattr(f, "file", "") or "")),
            "detected_type": mask_pii(str(getattr(f, "detected_type", "") or "")),
            "document_name": mask_pii(str(doc_name)) if doc_name else None,
            "claimed_type": mask_pii(str(getattr(f, "claimed_type", "") or "")),
            "has_meterai": getattr(f, "has_meterai", None),
            "has_signature": getattr(f, "has_signature", None),
            "has_stamp": getattr(f, "has_stamp", None),
            "confidence": (
                round(float(confidence), 2)
                if isinstance(confidence, (int, float))
                else None
            ),
            "matches": getattr(f, "matches", None),
        })
    return digest


def _documents_for_copilot(
    documents: list,
    digest: Optional[list[dict[str, Any]]] = None,
) -> dict[str, dict[str, Any]]:
    """Convert a list of guided_submission.SessionDocument (or anything with
    file_id/filename/mime_type/content/claimed_type attrs) into the dict the
    copilot's `_doc_context` expects, keyed by file_id.

    When the read `digest` is supplied, each doc is enriched with the
    `detected_type` BIMA read it as (cross-referenced by file_id), so the
    copilot's `_resolve_doc_ref` can match the officer's "KTP" even when the
    citizen mislabelled the upload — detected_type is what BIMA actually saw.
    detected_type values are validator enums (KTP, NIB, …), not PII.
    """
    detected_by_fid: dict[str, str] = {}
    for entry in digest or []:
        if not isinstance(entry, dict):
            continue
        fid = str(entry.get("file_id") or "")
        det = str(entry.get("detected_type") or "")
        if fid and det:
            detected_by_fid[fid] = det

    out: dict[str, dict[str, Any]] = {}
    for d in documents or []:
        fid = getattr(d, "file_id", None)
        content = getattr(d, "content", None)
        if not fid or content is None:
            continue
        out[fid] = {
            "filename": getattr(d, "filename", fid),
            "mime_type": getattr(d, "mime_type", "application/octet-stream"),
            "content": content,
            "claimed_type": getattr(d, "claimed_type", ""),
            "detected_type": detected_by_fid.get(fid, ""),
        }
    return out


# ===========================================================================
# SIAP-grounded case loader ([[Multi-step Officer Copilot]]) — the CORE.
#
# The first-step officer sees the case via the citizen's in-memory session
# (notify_officer_of_submission threads the live docs + validator score). But
# every SUBSEQUENT desk in the SIAP approval chain never saw that session — so
# to be the officer's copilot at EVERY step, BIMA must assemble the case PURELY
# from SIAP:
#   (a) the request's submission documents — resolved via profile_requirements,
#       bytes fetched on demand from Beta storage (siap_templates), so
#       get_doc_summary / send_document / compare_identity work verbatim;
#   (b) ALL prior-step notes — read LIVE by the copilot's get_case_log_notes
#       tool (keyed by ticket → vw_license_log / get_license_request_log), so we
#       only need to carry the ticket, not snapshot the notes here;
#   (c) the current step + whether it is the FINAL (SK-signing) desk;
#   (d) validation/score — SIAP holds none; left absent (get_validation_summary
#       then honestly reports "belum tersedia").
#
# Everything is grounded in SIAP. On a miss / DB-down every read degrades to an
# empty result, and the loader returns a case with whatever it COULD read (or
# None when even the request identity is unreadable) — never a fabrication.
# ===========================================================================

# Cap on how many submission-doc bodies we pull per case (across BOTH sources),
# and — separately, per doc — an 8 MB byte ceiling on each body. BOTH sources now
# enforce that ceiling: source (a) via siap_templates.fetch_submission_file_bytes
# (_MAX_TEMPLATE_BYTES), source (b) via siap_document_client.download_document
# (_SIAP_DOCUMENT_MAX_BYTES, same 8 MB default). Keeps a pathological upload set
# from bloating the in-memory session / Redis blob.
_MAX_SIAP_DOCS = int(os.getenv("BIMA_OFFICER_SIAP_MAX_DOCS", "12"))


async def _load_siap_documents(
    profile_id: Optional[int],
    request_id: Optional[int] = None,
) -> dict[str, dict[str, Any]]:
    """Fetch the applicant's uploaded requirement files from SIAP into the
    copilot `_doc_context` shape ({file_id: {filename, mime_type, content,
    claimed_type, detected_type}}). Bytes pulled on demand from Beta storage.

    Two sources are merged:
      (a) profile_requirements — the requirement files linked to the applicant's
          SIAP profile, bytes fetched from Beta storage (the original source);
      (b) the per-request document API (siap_document_client) — the applicant-
          upload docs that BIMA itself pushed on submit and that
          profile_requirements does NOT contain for BIMA-created requests.
    Both are cross-referenced into the SAME `_doc_context` shape and DEDUPED by
    (filename/label), so a doc present in both sources appears once.

    Source (b) only runs when a `request_id` is supplied AND the document client
    is configured — otherwise this behaves byte-for-byte as before (source (a)
    only). NEVER raises. A doc whose bytes can't be fetched is SKIPPED (not
    faked) — the officer is never shown a document BIMA couldn't actually read.
    File names/bytes are never logged.
    """
    if profile_id is None and request_id is None:
        return {}
    from services.siap_templates import fetch_submission_file_bytes

    out: dict[str, dict[str, Any]] = {}
    # Dedupe keys — normalised filename/label already claimed by an emitted doc.
    seen_labels: set[str] = set()
    fetched = 0

    # --- Source (a): profile_requirements-backed docs (bytes from storage) ---
    if profile_id is not None:
        from services.siap_db import get_submission_doc_refs

        refs = await get_submission_doc_refs(int(profile_id))
        for ref in refs:
            if fetched >= _MAX_SIAP_DOCS:
                break
            file_ref = ref.get("file_ref")
            if not file_ref:
                continue
            try:
                content = await fetch_submission_file_bytes(file_ref)
            except Exception:  # pragma: no cover — defensive; fetch never raises but guard anyway
                logger.exception("SIAP submission-doc fetch crashed (skipping one doc)")
                content = None
            if not content:
                continue
            # Synthetic, stable file_id keyed on the requirement so the copilot
            # can cross-reference it; the requirement label is the human filename
            # the officer recognises ("Fotokopi KTP"). claimed_type mirrors the
            # label so _resolve_doc_ref can match "KTP" etc.
            req_id = ref.get("requirements_id")
            fid = f"siap:req:{req_id}" if req_id is not None else f"siap:{fetched}"
            label = ref.get("requirement_name") or _basename(str(file_ref))
            out[fid] = {
                "filename": label,
                "mime_type": _guess_mime(str(file_ref)),
                "content": content,
                "claimed_type": ref.get("requirement_name") or "",
                "detected_type": "",
            }
            seen_labels.add(_dedupe_key(label))
            fetched += 1

    profile_docs = len(out)

    # --- Source (b): per-request document API (applicant-upload docs) ---
    # For a BIMA-created request the applicant uploads live behind the document
    # API, not profile_requirements. Merge them in, deduping by filename/label
    # so a doc present in both sources is emitted once. Best-effort: the client
    # degrades (list → {ok:False}, download → None) rather than raising, so a
    # not-configured / unreachable doc API leaves source (a) untouched.
    doc_api_docs = 0
    if request_id is not None:
        from services.siap_document_client import get_siap_document_client

        doc_client = get_siap_document_client()
        if doc_client.is_configured():
            listing = await doc_client.list_documents(int(request_id))
            for entry in listing.get("documents", []):
                if fetched >= _MAX_SIAP_DOCS:
                    break
                if not isinstance(entry, dict):
                    continue
                file_id = entry.get("id") or entry.get("file_id")
                if file_id is None:
                    continue
                label = (
                    str(entry.get("filename") or "").strip()
                    or f"dokumen-{file_id}"
                )
                # DEDUPE — skip a doc already emitted from source (a).
                if _dedupe_key(label) in seen_labels:
                    continue
                content = await doc_client.download_document(int(file_id))
                if not content:
                    continue
                fid = f"siap:doc:{file_id}"
                out[fid] = {
                    "filename": label,
                    "mime_type": (
                        str(entry.get("mime") or "").strip()
                        or _guess_mime(label)
                    ),
                    "content": content,
                    "claimed_type": label,
                    "detected_type": "",
                }
                seen_labels.add(_dedupe_key(label))
                fetched += 1
                doc_api_docs += 1

    logger.info(
        "officer case: SIAP submission docs loaded | profile_id=%s request_id=%s "
        "| profile_docs=%d doc_api_docs=%d total=%d",
        profile_id, request_id, profile_docs, doc_api_docs, len(out),
    )
    return out


def _dedupe_key(label: str) -> str:
    """Normalise a filename/label for cross-source dedupe: lowercased, with the
    formatting differences the two sources introduce (underscores vs spaces,
    surrounding whitespace) collapsed. Mirrors the tolerance officer_copilot's
    `_resolve_doc_ref` applies so a doc that would resolve to the same ref is
    treated as the same doc here.
    """
    return " ".join((label or "").replace("_", " ").lower().split())


def _basename(path: str) -> str:
    """Last path segment of a storage key, for a human filename fallback."""
    cleaned = (path or "").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] or "dokumen"


def _guess_mime(file_ref: str) -> str:
    ext = (file_ref or "").rsplit(".", 1)[-1].lower()
    return {
        "pdf": "application/pdf",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(ext, "application/octet-stream")


async def load_case_from_siap(
    request_id: int,
    *,
    channel: str = CHANNEL_WHATSAPP,
    channel_id: str = "",
) -> Optional[OfficerCaseSession]:
    """Assemble an officer case context PURELY from SIAP for `request_id`.

    Returns an OfficerCaseSession (NOT persisted — the caller registers it for
    the specific officer) grounded entirely in SIAP: submission docs (a),
    ticket for the live prior-step-notes tool (b), current step + is_final_step
    (c); validation is left None (SIAP has none) (d).

    Returns None only when SIAP can't even identify the request (no ticket / DB
    down) — the caller then skips notifying rather than sending an empty brief.
    NEVER raises. PII is never logged.
    """
    from services.siap_db import get_request_case_meta

    meta = await get_request_case_meta(int(request_id))
    if not meta.get("found") or not meta.get("ticket"):
        logger.info(
            "load_case_from_siap: request not resolvable in SIAP | request_id=%s",
            request_id,
        )
        return None

    documents = await _load_siap_documents(meta.get("profile_id"), int(request_id))

    # Resolve the licence's real display name so the next-desk brief/notification
    # reads "Persetujuan Pengadaan Kapal Perikanan (PKPP)…" instead of a bare "-"
    # or the generic type. Best-effort; degrades to None (brief then shows "-").
    license_name = None
    _lid = meta.get("license_id")
    if _lid is not None:
        try:
            from services.siap_db import get_license_name
            license_name = await get_license_name(int(_lid))
        except Exception:
            logger.exception(
                "load_case_from_siap: license_name resolve failed | request_id=%s",
                request_id,
            )
            license_name = None

    sess = OfficerCaseSession(
        channel_id=channel_id,
        channel=channel,
        ticket=str(meta["ticket"]),
        request_id=int(request_id),
        license_id=meta.get("license_id"),
        license_name=license_name,  # real name for the officer brief/notification
        profile_id=meta.get("profile_id"),  # grounds draft_sk identity on profile
        step_stereotype=meta.get("stereotype"),  # per-step template selection
        applicant_name=None,        # copilot reads identity from profile, grounded
        alamat=None,
        validation=None,            # SIAP holds no BIMA score
        documents=documents,
        documents_digest=[],
        is_final_step=bool(meta.get("is_final_step")),
    )
    logger.info(
        "load_case_from_siap | request_id=%s ticket=%s docs=%d final_step=%s",
        request_id, _mask(str(meta["ticket"])), len(documents), sess.is_final_step,
    )
    return sess


# ===========================================================================
# Officer brief rendering
# ===========================================================================

_PORTAL_TRACK_URL = "https://beta-siap.bimaptsp.com/track/{ticket}"


def _render_brief(
    *,
    ticket: str,
    license_name: Optional[str],
    applicant_name: Optional[str],
    validation: Optional[dict[str, Any]],
) -> str:
    """The first message the officer receives — a brief + the BIMA score."""
    lines = [
        "🗂️ *Berkas baru untuk diperiksa*",
        "",
        f"📋 *Izin:* {license_name or '-'}",
        f"🎟️ *Tiket:* {ticket}",
        f"👤 *Pemohon:* {_mask(applicant_name)}",
    ]
    if validation:
        pct = validation.get("score_percent", 0)
        issues = validation.get("issues") or []
        crit = sum(1 for i in issues if str(i.get("severity")) == "critical")
        high = sum(1 for i in issues if str(i.get("severity")) == "high")
        lines += [
            "",
            f"🤖 *Skor BIMA:* {pct}%",
        ]
        if crit or high:
            lines.append(
                f"⚠️ Temuan: {crit} kritis, {high} tinggi — perlu diperiksa."
            )
        else:
            lines.append("✅ Tidak ada temuan kritis dari BIMA.")
    else:
        lines += ["", "🤖 Skor BIMA belum tersedia untuk tiket ini."]

    lines += [
        "",
        "Balas pesan ini untuk mulai memeriksa. Anda bisa bertanya, mis. "
        "_\"apa isi proposalnya?\"_, _\"ringkas temuan validasi\"_, atau "
        "_\"bandingkan nama di KTP dan surat permohonan\"_.",
        "",
        "Ketik *YA* untuk meneruskan berkas ke tahap berikutnya, atau "
        "jelaskan alasan jika ingin *menolak*.",
    ]
    return "\n".join(lines)


# ===========================================================================
# 1) Officer notification on submission
# ===========================================================================


async def notify_officer_of_submission(
    *,
    ticket: str,
    request_id: Optional[int],
    license_id: Optional[int],
    license_name: Optional[str],
    applicant_name: Optional[str],
    score: Optional[dict[str, Any]],
    documents: Optional[list] = None,
    applicant_alamat: Optional[str] = None,
) -> bool:
    """Send the validating officer a chat brief + register their case session.

    Called fire-and-forget from guided_submission._submit on a real SIAP
    request creation. Feature-flagged; returns False (no-op) when disabled or
    when no demo officer channel is configured. Never raises out to the caller
    (the caller already wraps in try/except, but we degrade internally too).
    """
    if not is_enabled():
        logger.info("officer notify suppressed (flag off) | ticket=%s", ticket)
        return False

    validation = _score_to_validation(score)
    # Compact per-doc read digest (Task F) — snapshotted from the rich
    # SuitabilityResult now, before `score["result"]` is dropped for Redis, so
    # "what BIMA read per doc" survives a restart. Threaded onto the validation
    # dict so the copilot's get_validation_summary / get_case_full surface it
    # (via _validation_context) without a second injection channel. Built BEFORE
    # docs_map so the map can be enriched with each doc's detected_type.
    docs_digest = _documents_digest(score)
    docs_map = _documents_for_copilot(documents or [], docs_digest)
    if validation is not None and docs_digest:
        validation["documents_digest"] = docs_digest
    brief = _render_brief(
        ticket=ticket,
        license_name=license_name,
        applicant_name=applicant_name,
        validation=validation,
    )

    # --- Flow-based resolution (primary) -------------------------------------
    # Resolve the officer(s) who own the request's CURRENT approval step on
    # this izin (Privilege Izin ∩ the step's role), gated by the step's
    # wa_notification flag. Never raises — returns an empty result on any error.
    officer_whatsapps: list[str] = []
    if request_id is not None:
        try:
            from services.siap_db import resolve_step_officers

            resolution = await resolve_step_officers(int(request_id))
            officer_whatsapps = list(resolution.get("officer_whatsapps") or [])
            logger.info(
                "officer notify resolution | ticket=%s | wa_active=%s | "
                "applicant_step=%s | group_id=%s | resolved=%d",
                ticket, resolution.get("wa_active"),
                resolution.get("is_applicant_step"),
                resolution.get("group_id"), len(officer_whatsapps),
            )
        except Exception:
            # Defensive — resolve_step_officers already swallows DB errors, but
            # never let resolution crash the citizen's success reply.
            logger.exception(
                "officer notify: resolution crashed (falling back) | ticket=%s",
                ticket,
            )
            officer_whatsapps = []

    if officer_whatsapps:
        # Make every just-resolved officer immediately recognizable on their
        # FIRST reply: union the normalized numbers into the same _officer_cache
        # set is_officer_channel_id reads. Without this, an officer whose roster
        # row changed after the last 5-min refresh (or if the startup warm
        # failed) would fall through to the citizen path until the next refresh.
        # resolve_step_officers already returns APTANA-normalized (62…) numbers,
        # so they're directly comparable to the cache. We only ADD — the TTL /
        # background-refresh logic is untouched (a later refresh still rebuilds
        # the authoritative set from the directory).
        global _officer_cache
        _officer_cache |= {n for n in officer_whatsapps if n}

        # Register a session + send the brief for EACH resolved officer. A
        # send/session failure for one officer must not block the others.
        any_sent = False
        for wa_num in officer_whatsapps:
            sess = OfficerCaseSession(
                channel_id=wa_num,
                channel=CHANNEL_WHATSAPP,
                ticket=ticket,
                request_id=request_id,
                license_id=license_id,
                license_name=license_name,
                applicant_name=applicant_name,
                alamat=applicant_alamat,
                validation=validation,
                documents=dict(docs_map),  # per-officer copy
                documents_digest=list(docs_digest),
            )
            await _put_session(sess)
            sent = await _send_officer_notify(
                CHANNEL_WHATSAPP, wa_num,
                license_name=license_name, ticket=ticket,
                validation=validation, brief=brief,
            )
            any_sent = any_sent or sent
            logger.info(
                "officer notify (flow) | ticket=%s | officer=%s | sent=%s | "
                "has_score=%s | docs=%d",
                ticket, _mask(wa_num), sent,
                validation is not None, len(docs_map),
            )
        return any_sent

    # --- Fallback: static env officer (BIMA_OFFICER_WA_PHONE / _TG_CHAT) ------
    # No flow-resolved officer (wa not active, applicant step, no assignee with
    # a number, or a SIAP error). Fall back to the demo env number if set.
    wa = _demo_officer_wa()
    tg = _demo_officer_tg()
    if not wa and not tg:
        logger.warning(
            "officer notify: no officer resolved from flow AND no fallback "
            "channel configured (BIMA_OFFICER_WA_PHONE / BIMA_OFFICER_TG_CHAT) "
            "| ticket=%s",
            ticket,
        )
        return False

    # Prefer WhatsApp when configured; Telegram otherwise.
    channel = CHANNEL_WHATSAPP if wa else CHANNEL_TELEGRAM
    channel_id = _normalize_wa(wa) if wa else tg

    sess = OfficerCaseSession(
        channel_id=channel_id,
        channel=channel,
        ticket=ticket,
        request_id=request_id,
        license_id=license_id,
        license_name=license_name,
        applicant_name=applicant_name,
        alamat=applicant_alamat,
        validation=validation,
        documents=docs_map,
        documents_digest=list(docs_digest),
    )
    await _put_session(sess)

    sent = await _send_officer_notify(
        channel, channel_id,
        license_name=license_name, ticket=ticket,
        validation=validation, brief=brief,
    )
    logger.info(
        "officer notify (fallback env) | ticket=%s | channel=%s | officer=%s | "
        "sent=%s | has_score=%s | docs=%d",
        ticket, channel, _mask(channel_id), sent,
        validation is not None, len(docs_map),
    )
    return sent


# ===========================================================================
# 1c) Citizen asked for a HUMAN — the hard gate's safety valve
# ===========================================================================


def _render_escalation_brief(
    *,
    citizen_wa: Optional[str],
    license_name: Optional[str],
    score: Optional[dict[str, Any]],
    blocking: Optional[list[dict[str, str]]],
) -> str:
    """The message an officer gets when BIMA's gate blocked a citizen who
    believes it is wrong. Carries the citizen's contact (the actionable bit) and
    what BIMA objected to — deliberately NOT masked for the number, because the
    whole point is that a human can call them back. No NIK is included.
    """
    pct = score.get("score_percent") if isinstance(score, dict) else None
    lines = [
        "PERMINTAAN TINJAUAN PETUGAS",
        "",
        "Seorang pemohon meminta permohonannya ditinjau petugas. "
        "BIMA menilai berkasnya belum lengkap, namun pemohon merasa sudah benar.",
        "",
        f"Izin: {license_name or '-'}",
    ]
    if isinstance(pct, int):
        lines.append(f"Estimasi kelayakan BIMA: {pct}%")
    if citizen_wa:
        lines.append(f"Kontak pemohon (WhatsApp): {citizen_wa}")

    if blocking:
        lines += ["", "Yang BIMA nilai belum sesuai:"]
        for b in blocking:
            msg = mask_pii(str(b.get("message", "")).strip())
            if msg:
                lines.append(f"- {msg}")
    elif isinstance(score, dict) and score.get("message"):
        lines += ["", mask_pii(str(score["message"]).strip())]

    lines += [
        "",
        "Belum ada permohonan yang dibuat di SIAP — berkas ini tertahan di "
        "BIMA. Mohon hubungi pemohon untuk memastikan.",
    ]
    return "\n".join(lines).rstrip()


async def notify_officer_of_escalation(
    *,
    citizen_wa: Optional[str],
    license_name: Optional[str],
    license_id: Optional[int] = None,
    score: Optional[dict[str, Any]] = None,
    blocking: Optional[list[dict[str, str]]] = None,
) -> bool:
    """Hand a gate-blocked citizen to a human officer.

    There is **no SIAP request** here — the packet never passed the gate, so
    there is no ticket and no request_id. But the officer is NOT a static env
    phone: SIAP's Alur Izin (`license_approval_step`) hangs off the LICENCE, so
    the desk that WOULD have received this submission is resolvable from
    `license_id` alone (`resolve_license_first_officers`). For PKPP that is
    sort_order=1, "Petugas SKPD" — the same verificator a real submission would
    reach, read live from SIAP's roster, so a staffing change in SIAP needs no
    redeploy and no env edit. BIMA_OFFICER_WA_PHONE remains only as a
    last-resort fallback for when the SIAP DB is unreachable or the licence has
    no officer desk.

    ⚠️ Known limit (WhatsApp): this is a FREE-FORM send, so it only reaches an
    officer inside Meta's 24h service window. No approved template exists for an
    escalation, and the new-submission template's arity is (izin, tiket, skor) —
    there is no ticket to put in it. Telegram has no window and always lands.
    Best-effort by design: never raises; returns False when it could not send.
    """
    if not is_enabled():
        logger.info("escalation notify suppressed (flag off)")
        return False

    # 1) Preferred: the licence's real first officer desk, straight from SIAP.
    officer_whatsapps: list[str] = []
    if license_id is not None:
        try:
            from services.siap_db import resolve_license_first_officers

            officer_whatsapps = await resolve_license_first_officers(int(license_id))
        except Exception:
            logger.exception(
                "escalation notify: officer resolution crashed | license_id=%s",
                license_id,
            )
            officer_whatsapps = []

    brief = _render_escalation_brief(
        citizen_wa=citizen_wa,
        license_name=license_name,
        score=score,
        blocking=blocking,
    )

    if officer_whatsapps:
        # Make every resolved officer recognisable on their first reply, exactly
        # as the submission path does — otherwise their answer would fall through
        # to the citizen router until the next directory refresh.
        global _officer_cache
        _officer_cache |= {n for n in officer_whatsapps if n}
        any_sent = False
        for wa_num in officer_whatsapps:
            sent_one = await _send(CHANNEL_WHATSAPP, wa_num, brief)
            any_sent = any_sent or sent_one
            logger.info(
                "escalation notify (SIAP roster) | license_id=%s | officer=%s | "
                "sent=%s | citizen=%s | blocking=%d",
                license_id, _mask(wa_num), sent_one, _mask(citizen_wa or ""),
                len(blocking or []),
            )
        return any_sent

    # 2) Fallback: the configured demo channel (SIAP DB down / no officer desk).
    wa = _demo_officer_wa()
    tg = _demo_officer_tg()
    if not wa and not tg:
        logger.warning(
            "escalation notify: no officer resolved from SIAP (license_id=%s) "
            "AND no fallback channel configured "
            "(BIMA_OFFICER_WA_PHONE / BIMA_OFFICER_TG_CHAT)",
            license_id,
        )
        return False

    channel = CHANNEL_WHATSAPP if wa else CHANNEL_TELEGRAM
    channel_id = _normalize_wa(wa) if wa else tg

    # `brief` was already rendered above — the fallback sends the same text.
    sent = await _send(channel, channel_id, brief)
    logger.info(
        "escalation notify | channel=%s | officer=%s | citizen=%s | sent=%s | "
        "blocking=%d",
        channel, _mask(channel_id), _mask(citizen_wa or ""), sent,
        len(blocking or []),
    )
    return sent


# ===========================================================================
# 1b) Auto-notify the NEXT desk when a request ADVANCES ([[Multi-step Copilot]])
# ===========================================================================


async def notify_next_step(request_id: int) -> bool:
    """Notify the officer(s) at the request's now-CURRENT step, chaining the
    copilot to the next desk in the SIAP approval chain.

    Called after BIMA's own confirmed forward / approved-decision write moves a
    file forward. SIAP has already advanced `license_request.approval_step_id`,
    so `resolve_step_officers` now resolves the NEW current desk. For each
    resolved officer we register a SIAP-grounded case session
    (load_case_from_siap → docs + step + is_final_step, all from SIAP) and send
    the approved new-submission template exactly like the first step.

    Returns True if at least one next-step officer was notified. NEVER raises;
    a SIAP miss / DB-down / applicant-step (chain ended at the citizen) all
    degrade to a logged False. PII never logged.
    """
    if not is_enabled():
        logger.info("next-step notify suppressed (flag off) | request_id=%s", request_id)
        return False
    if request_id is None:
        return False

    try:
        from services.siap_db import resolve_step_officers

        resolution = await resolve_step_officers(int(request_id))
    except Exception:
        logger.exception(
            "next-step notify: resolution crashed | request_id=%s", request_id
        )
        return False

    officer_whatsapps = list(resolution.get("officer_whatsapps") or [])
    logger.info(
        "next-step notify resolution | request_id=%s wa_active=%s applicant_step=%s "
        "group_id=%s resolved=%d",
        request_id, resolution.get("wa_active"), resolution.get("is_applicant_step"),
        resolution.get("group_id"), len(officer_whatsapps),
    )
    if not officer_whatsapps:
        # Chain terminus, non-active desk, or applicant step → nobody to notify.
        return False

    # Build ONE SIAP-grounded case; each officer gets their own doc copy + session.
    base_case = await load_case_from_siap(int(request_id))
    if base_case is None:
        logger.info(
            "next-step notify: case not loadable from SIAP | request_id=%s", request_id
        )
        return False

    # Make each just-resolved officer immediately recognizable on their first
    # reply (same union the submit-time notify does).
    global _officer_cache
    _officer_cache |= {n for n in officer_whatsapps if n}

    brief = _render_brief(
        ticket=base_case.ticket,
        license_name=base_case.license_name,
        applicant_name=None,
        validation=None,
    )

    any_sent = False
    for wa_num in officer_whatsapps:
        sess = OfficerCaseSession(
            channel_id=wa_num,
            channel=CHANNEL_WHATSAPP,
            ticket=base_case.ticket,
            request_id=base_case.request_id,
            license_id=base_case.license_id,
            license_name=base_case.license_name,
            applicant_name=base_case.applicant_name,
            alamat=base_case.alamat,
            validation=None,
            documents=dict(base_case.documents),  # per-officer copy
            documents_digest=[],
            is_final_step=base_case.is_final_step,
        )
        await _put_session(sess)

        # LAST step (Kepala Dinas): send the SK signing magic-link so the Kepala
        # can sign in SIAP; earlier desks get the standard new-submission alert.
        if base_case.is_final_step:
            sent = await _send_final_step_notify(
                CHANNEL_WHATSAPP, wa_num,
                license_name=base_case.license_name,
                ticket=base_case.ticket,
                request_id=base_case.request_id,
            )
        else:
            sent = await _send_officer_notify(
                CHANNEL_WHATSAPP, wa_num,
                license_name=base_case.license_name, ticket=base_case.ticket,
                validation=None, brief=brief,
            )
        any_sent = any_sent or sent
        logger.info(
            "next-step notify | request_id=%s officer=%s sent=%s final_step=%s docs=%d",
            request_id, _mask(wa_num), sent, base_case.is_final_step,
            len(base_case.documents),
        )
    return any_sent


# ===========================================================================
# 2) Officer reply → copilot bridge (inbound FAST-PATH)
# ===========================================================================


def is_officer_channel_id(channel_id: str) -> bool:
    """True when this inbound sender is a REGISTERED officer AND the bridge is
    enabled. Data-driven: matches the warm officer-directory cache (any petugas
    whose SIAP role owns an approval step), plus the env fallback numbers.

    SYNC by contract — called in the request path (routers/aptana.py media
    branch + maybe_handle_officer_reply). It must NOT block on a DB call: it
    reads the in-memory cached set and, if that set is stale, kicks a
    NON-BLOCKING background refresh while answering from the last-known set.
    Numbers are normalized on both sides so 08…, +62…, 62… all match.
    """
    if not is_enabled():
        return False
    cid = (channel_id or "").strip()
    if not cid:
        return False

    # Refresh opportunistically (non-blocking) so the set stays warm without a
    # synchronous DB round-trip on the hot path.
    _maybe_kick_officer_refresh()

    norm = _normalize_wa(cid)

    # Env fallback numbers (WhatsApp normalized; Telegram chat id is digits and
    # compared raw too). Kept so BIMA_OFFICER_WA_PHONE keeps working.
    env_wa = _normalize_wa(_demo_officer_wa())
    env_tg = _demo_officer_tg()
    if env_wa and norm == env_wa:
        return True
    if env_tg and cid == env_tg:
        return True

    # Data-driven set (normalized officer WhatsApps from SIAP).
    return bool(norm) and norm in _officer_cache


# The two copilot tools that take a case OFF this officer's desk. A confirmed,
# successful call to either means the session should be cleared.
_CASE_CLOSING_TOOLS = {"forward_case", "record_decision"}


def _case_was_closed(tool_calls: list) -> bool:
    """True when the copilot turn included a SUCCESSFUL, CONFIRMED forward or
    decision write — i.e. the case left this desk and the session is stale.

    The copilot reports each call as {name, args, result_preview}; result_preview
    is the JSON-dumped tool result. A successful confirmed write looks like
    `{"executed": true, "ok": true, ...}` AND was invoked with
    `confirmed=true`. A draft (`"executed": false`) or a failed write
    (`"ok": false`) must NOT clear the session, so we require all three signals.
    """
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        if call.get("name") not in _CASE_CLOSING_TOOLS:
            continue
        args = call.get("args")
        if not (isinstance(args, dict) and args.get("confirmed") is True):
            continue
        preview = str(call.get("result_preview") or "")
        # Normalise whitespace so we match the json.dumps form regardless of
        # spacing ("ok": true / "ok":true).
        compact = preview.replace(" ", "")
        if '"executed":true' in compact and '"ok":true' in compact:
            return True
    return False


def _case_advanced(tool_calls: list) -> bool:
    """True when the confirmed write MOVED THE FILE FORWARD to the next desk —
    a successful `forward_case`, or a `record_decision(approved)`. A `rejected`
    decision routes the file BACK a desk (SIAP-side), so it must NOT trigger a
    next-step notify (there is no new forward desk). Same three-signal guard as
    `_case_was_closed`, plus: for record_decision the decision must be
    'approved'."""
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        if name not in _CASE_CLOSING_TOOLS:
            continue
        args = call.get("args")
        if not (isinstance(args, dict) and args.get("confirmed") is True):
            continue
        preview = str(call.get("result_preview") or "").replace(" ", "")
        if not ('"executed":true' in preview and '"ok":true' in preview):
            continue
        if name == "record_decision":
            decision = str(args.get("decision") or "").strip().lower()
            if decision != "approved":
                continue  # rejected → routed back, not advanced
        return True
    return False


async def maybe_handle_officer_reply(
    *,
    channel: str,
    channel_id: str,
    message: str,
) -> Optional[str]:
    """Inbound FAST-PATH: route an officer's chat reply into the copilot.

    Returns:
      * a reply string — the sender is the demo officer with an active case
        session; the message was handled by the copilot and the caller should
        send this reply back (and NOT run the citizen AI).
      * None — not an officer-in-session message; the caller proceeds with the
        normal citizen path.

    Never raises — on any internal failure it returns a polite Indonesian
    apology string (so the officer isn't stranded) rather than None, because
    None would leak the officer's message into the citizen AI.
    """
    if not is_enabled():
        return None
    if not is_officer_channel_id(channel_id):
        return None

    sess = await _get_session(channel_id)
    if sess is None:
        # Officer texted but no active case — tell them there's nothing queued
        # rather than dropping into the citizen RAG (which would be confusing
        # for an officer). This keeps the officer surface coherent.
        logger.info(
            "officer reply with no active case | officer=%s", _mask(channel_id)
        )
        return (
            "Belum ada berkas yang menunggu pemeriksaan Anda saat ini. "
            "BIMA akan mengirim brief otomatis begitu ada permohonan baru "
            "di meja Anda."
        )

    msg = (message or "").strip()
    if not msg:
        return None

    try:
        from services.agents.officer_copilot import get_copilot

        # Cap history so the turn stays bounded.
        history = sess.history[-(_MAX_HISTORY_TURNS * 2):]

        # LAST step (Kepala Dinas / SK-signing): drive the copilot in
        # "signature" mode — it exposes the SIAP signing handoff (no "forward"
        # recommendation, no write tools) so BIMA tells the Kepala the SK is
        # ready and links out to SIAP for the TTE. Every earlier desk uses
        # "officer" mode (forward/decision writes).
        copilot_mode = "signature" if sess.is_final_step else "officer"

        result = await get_copilot().chat(
            message=msg,
            ticket=sess.ticket,
            history=history,
            officer_id=None,           # masked-logging only; we hold no JWT here
            validation=sess.validation,
            mode=copilot_mode,
            documents=sess.documents,  # in-session bytes → real get_doc_summary
            # SK-draft context (draft_sk) — licence id + applicant identity so
            # the deputy can render SIAP's PKPP approval-letter template. The
            # applicant fields go INTO the SK docx (authorized officer) and are
            # never logged here.
            sk_context={
                "license_id": sess.license_id,
                "ticket": sess.ticket,
                "license_name": sess.license_name,
                "applicant_name": sess.applicant_name,
                "alamat": sess.alamat,
                # Generic step-aware output-doc drafting: profile_id grounds the
                # applicant-identity placeholders on the REAL person_profile;
                # is_final_step + step_stereotype select Rekomtek vs SK.
                "profile_id": sess.profile_id,
                "is_final_step": sess.is_final_step,
                "step_stereotype": sess.step_stereotype,
            },
        )
    except Exception:
        logger.exception(
            "officer copilot bridge crashed | officer=%s | ticket=%s",
            _mask(channel_id), sess.ticket,
        )
        return (
            "Maaf, terjadi gangguan saat memproses pertanyaan Anda. "
            "Mohon coba lagi sebentar."
        )

    reply = result.get("reply") or (
        "Maaf, saya belum bisa menjawab itu. Mohon ulangi dengan kalimat lain."
    )

    tool_calls = result.get("tool_calls") or []
    closed = _case_was_closed(tool_calls)

    # Task G — the officer asked for the real document file(s). The copilot's
    # `send_document` tool resolved them to file_ids and surfaced them here; we
    # deliver the actual bytes out-of-band on the officer's own channel. Runs
    # before the closed/persist branch so a doc requested in the same turn as a
    # confirm still goes out. Never raises out.
    docs_to_send = result.get("documents_to_send") or []
    if docs_to_send:
        await _deliver_documents(
            channel=channel, channel_id=channel_id, sess=sess,
            file_ids=docs_to_send,
        )

    logger.info(
        "officer copilot bridge turn | officer=%s | ticket=%s | "
        "tool_calls=%d | reply_len=%d | closed=%s | docs_sent=%d",
        _mask(channel_id), sess.ticket,
        len(tool_calls), len(reply), closed, len(docs_to_send),
    )

    if closed:
        # A confirmed forward/decision write succeeded → the case left this
        # officer's desk. Clear the session so a stale ticket can't hijack the
        # officer's next, unrelated message for up to the 12h TTL. The desk
        # returns to "nothing queued" until the next brief arrives.
        await clear_session(channel_id)
        # If the file ADVANCED (forward, or an approved decision — NOT a reject,
        # which routes back), auto-notify the NEXT desk's officer(s), chaining
        # the copilot down the SIAP approval chain to the last step. Never
        # raises out of the officer reply path.
        if _case_advanced(tool_calls):
            # Recover request_id from the ticket when the session never carried
            # it. A ticket-only officer session can have sess.request_id == None
            # even though the forward SUCCEEDED (forward_case resolves the id
            # locally), and the old `sess.request_id is not None` guard then
            # SILENTLY skipped the next-desk notify — which is why a forwarded
            # case's next officer was never pinged.
            rid = sess.request_id
            if rid is None and sess.ticket:
                try:
                    from services.siap_tools import siap_resolve_request_id
                    rid = await siap_resolve_request_id(str(sess.ticket))
                except Exception:
                    logger.exception(
                        "next-step notify: request_id recovery failed | ticket=%s",
                        sess.ticket,
                    )
                    rid = None
            if rid is not None:
                try:
                    await notify_next_step(int(rid))
                except Exception:
                    logger.exception(
                        "next-step officer notify crashed (non-fatal) | ticket=%s",
                        sess.ticket,
                    )
            else:
                logger.warning(
                    "next-step notify skipped: no request_id | ticket=%s", sess.ticket
                )
        return reply

    # Otherwise persist the rolling history the copilot returned (it includes
    # the new user turn + the model reply) so follow-up Q&A keeps context.
    new_history = result.get("history")
    if isinstance(new_history, list):
        sess.history = new_history[-(_MAX_HISTORY_TURNS * 2):]
    sess.touch()
    await _put_session(sess)
    return reply


# ===========================================================================
# Document delivery (Task G) — send the citizen's real file to the officer.
# ===========================================================================


def _public_base_url() -> str:
    """Public base for the `/dl/{token}` short-lived download links (mirrors
    guided_submission._public_base_url).

    MUST be the host where Caddy routes `/dl/*` to ai-engine — i.e. bimaptsp.com.
    It is NOT the tracking host: `beta-siap.bimaptsp.com` is the SIAP Laravel app,
    which has no `/dl` route and answers every such link with its own 404 page.
    Tracking moved to beta-siap in the domain cutover; this default came along
    for the ride and silently broke every document delivery — APTANA fetched
    Laravel, never ai-engine, so the bytes were never even asked for. Verified
    2026-07-16: beta-siap/dl/<tok> -> Laravel 404; bimaptsp.com/dl/<tok> -> 200 PDF.
    """
    return os.getenv("BIMA_PUBLIC_BASE_URL", "https://bimaptsp.com").rstrip("/")


async def _deliver_documents(
    *,
    channel: str,
    channel_id: str,
    sess: OfficerCaseSession,
    file_ids: list,
) -> None:
    """Deliver one or more requested in-session documents to the AUTHORIZED
    officer on their channel. Never raises.

    SECURITY: the document is the citizen's PII, but it is going to the
    authorized reviewing officer, who MUST be able to open it — so it is NOT
    encrypted (unlike the citizen PPKP path). The security is the delivery
    envelope: `generated_docs.store` hosts the bytes at an unguessable
    `/dl/{token}` URL with a short TTL that burns after a few fetches. WhatsApp
    gets a native document message (APTANA fetches the link); Telegram (which
    has no doc-send helper wired here) gets the link as text. Bytes/PII are
    never logged — only the masked channel + filename length.
    """
    from services import generated_docs

    base = _public_base_url()
    for entry in file_ids:
        # An entry is EITHER a file_id string (an in-session upload doc, resolved
        # against sess.documents by send_document) OR an inline dict carrying
        # freshly-generated bytes (e.g. the SK draft from draft_sk):
        # {"filename", "content" (bytes), "mime_type"}. Both deliver the same way.
        if isinstance(entry, dict):
            content = entry.get("content")
            filename = entry.get("filename") or "dokumen.docx"
        else:
            fid = entry
            doc = (sess.documents or {}).get(fid)
            if not doc:
                logger.info(
                    "officer doc-send: requested file_id not held | officer=%s | "
                    "ticket=%s",
                    _mask(channel_id), sess.ticket,
                )
                continue
            content = doc.get("content")
            filename = doc.get("filename") or f"{fid}.pdf"
        if not content:
            continue
        try:
            token = generated_docs.store(content, filename)
            url = f"{base}/dl/{token}"
        except Exception:
            logger.exception(
                "officer doc-send: hosting failed | officer=%s | ticket=%s",
                _mask(channel_id), sess.ticket,
            )
            continue

        sent = False
        try:
            if channel == CHANNEL_WHATSAPP:
                from services.whatsapp_sender import send_document as wa_doc
                sent = await wa_doc(
                    recipient_phone=channel_id, link=url, filename=filename
                )
            else:
                # Telegram has no document-send helper wired in the bridge —
                # deliver the link as a text message via the same path replies
                # use, so the officer can still open the file.
                sent = await _send(
                    channel, channel_id,
                    f"Dokumen: {filename}\n{url}",
                )
        except Exception:
            logger.exception(
                "officer doc-send failed | channel=%s | officer=%s | ticket=%s",
                channel, _mask(channel_id), sess.ticket,
            )
            continue

        logger.info(
            "officer doc-send | channel=%s | officer=%s | ticket=%s | "
            "filename_len=%d | sent=%s",
            channel, _mask(channel_id), sess.ticket, len(filename), sent,
        )


# ===========================================================================
# Channel send helper
# ===========================================================================


async def _send(channel: str, channel_id: str, body: str) -> bool:
    """Send `body` to the officer on the right channel. Never raises."""
    try:
        if channel == CHANNEL_TELEGRAM:
            from services.telegram_sender import send_text as tg_send
            return await tg_send(chat_id=channel_id, body=body)
        from services.whatsapp_sender import send_text as wa_send
        return await wa_send(recipient_phone=channel_id, body=body)
    except Exception:
        logger.exception(
            "officer bridge send failed | channel=%s | officer=%s",
            channel, _mask(channel_id),
        )
        return False


# Approved WhatsApp UTILITY template that alerts an officer OUTSIDE the 24h
# window. A free-form send to a cold officer number bounces with WhatsApp error
# 131047 ("re-engagement"); a template is the only way in. Body variables (no
# PII): {{1}} izin, {{2}} tiket, {{3}} skor. Empty name → fall back to free-form.
_OFFICER_TEMPLATE_NAME = os.getenv("BIMA_OFFICER_TEMPLATE_NAME", "bima_officer_new_submission").strip()
_OFFICER_TEMPLATE_LANG = os.getenv("BIMA_OFFICER_TEMPLATE_LANG", "id").strip() or "id"


async def _send_officer_notify(
    channel: str,
    channel_id: str,
    *,
    license_name: Optional[str],
    ticket: str,
    validation: Optional[dict[str, Any]],
    brief: str,
) -> bool:
    """Deliver the new-submission alert on the officer's channel.

    WhatsApp uses the approved template (`bima_officer_new_submission`) so it
    reaches a COLD officer number outside the 24h window; the rich, PII-masked
    brief then arrives when the officer replies (the copilot renders it on the
    first inbound). Telegram has no such window, so it gets the brief directly.
    Never raises.
    """
    if channel == CHANNEL_WHATSAPP and _OFFICER_TEMPLATE_NAME:
        pct = (validation or {}).get("score_percent")
        score_str = f"{pct}%" if isinstance(pct, int) else "-"
        try:
            from services.whatsapp_template import send_template

            return await send_template(
                recipient_phone=channel_id,
                template_name=_OFFICER_TEMPLATE_NAME,
                # Exact arity the template was approved with (izin, tiket, skor).
                body_params=[license_name or "Perizinan", ticket or "-", score_str],
                language_code=_OFFICER_TEMPLATE_LANG,
            )
        except Exception:
            logger.exception(
                "officer template send failed | officer=%s", _mask(channel_id)
            )
            return False
    # Telegram, or template disabled → free-form brief.
    return await _send(channel, channel_id, brief)


# ===========================================================================
# 3) Final step → SIAP signing magic-link (Kepala Dinas)
# ===========================================================================


def _render_sk_sign_brief(
    *, license_name: Optional[str], ticket: str, sign_url: Optional[str]
) -> str:
    """The message the LAST-step signer (Kepala Dinas) receives: the SK is ready
    and here is the SIAP signing link. BIMA does NOT sign — it links out."""
    lines = [
        "🖋️ *Berkas siap ditandatangani*",
        "",
        f"📋 *Izin:* {license_name or '-'}",
        f"🎟️ *Tiket:* {ticket}",
        "",
        "Surat Keputusan (SK) untuk permohonan ini sudah siap. "
        "Penandatanganan ber-TTE dilakukan langsung di SIAP.",
    ]
    if sign_url:
        lines += ["", f"🔗 Tautan tanda tangan SIAP:\n{sign_url}"]
    else:
        lines += [
            "",
            "Tautan tanda tangan SIAP belum tersedia di lingkungan ini. "
            "Silakan buka SIAP untuk menandatangani.",
        ]
    lines += [
        "",
        "Balas pesan ini bila ingin BIMA meringkas catatan meja-meja sebelumnya, "
        "menyoroti temuan validasi dan dasar hukumnya, atau membuka kembali "
        "tautan tanda tangan SIAP sebelum Anda menandatangani.",
    ]
    return "\n".join(lines)


async def _send_final_step_notify(
    channel: str,
    channel_id: str,
    *,
    license_name: Optional[str],
    ticket: str,
    request_id: Optional[int],
) -> bool:
    """Alert the LAST-step signer + hand them the SIAP signing magic-link.

    WhatsApp: fire the approved template first (reaches a cold number outside
    the 24h window), then best-effort a free-form message carrying the magic
    link (delivered when inside the window; a bounce is harmless — the copilot
    also surfaces the link on reply via get_siap_signing_link). Telegram gets
    the link brief directly. Never raises."""
    sign_url = _build_sk_sign_url(request_id=request_id, ticket=ticket)
    brief = _render_sk_sign_brief(
        license_name=license_name, ticket=ticket, sign_url=sign_url
    )

    if channel == CHANNEL_WHATSAPP and _OFFICER_TEMPLATE_NAME:
        # Template alert (cold-window safe). Reuse the approved arity; the third
        # param is a status label rather than a score at the signing desk.
        template_sent = False
        try:
            from services.whatsapp_template import send_template

            template_sent = await send_template(
                recipient_phone=channel_id,
                template_name=_OFFICER_TEMPLATE_NAME,
                body_params=[license_name or "Perizinan", ticket or "-", "Siap TTE"],
                language_code=_OFFICER_TEMPLATE_LANG,
            )
        except Exception:
            logger.exception(
                "final-step template send failed | officer=%s", _mask(channel_id)
            )
        # Best-effort in-window link delivery; a bounce outside 24h is fine.
        link_sent = await _send(channel, channel_id, brief)
        return bool(template_sent or link_sent)

    # Telegram, or template disabled → free-form signing brief directly.
    return await _send(channel, channel_id, brief)
