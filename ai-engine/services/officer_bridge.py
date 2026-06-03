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

DEMO OFFICER MAPPING — honest limitation.
  Mapping a SIAP desk role (group_id) to a real officer's phone is not cleanly
  modelled (person_profile has no exposed phone join) and would touch PII. For
  the demo the validating officer is a single configured test number:
    BIMA_OFFICER_WA_PHONE   — WhatsApp msisdn of the demo officer, or
    BIMA_OFFICER_TG_CHAT    — Telegram chat id of the demo officer.
  Real desk→officer resolution is out of scope for this slice (flagged).

PII
  Applicant name is masked in the brief and in all logs. Document bytes are
  never logged. The officer's own channel id is masked in logs.
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
    license_name: Optional[str] = None
    # The validator/suitability score for `ticket`, already shaped for the
    # copilot's `get_validation_summary` (score_percent/status/summary/issues).
    validation: Optional[dict[str, Any]] = None
    # In-session document bytes keyed by file_id, for `get_doc_summary`.
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Rolling copilot history (list of {role, text}) so the conversation has
    # memory across the officer's messages.
    history: list[dict[str, str]] = field(default_factory=list)
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
# ---------------------------------------------------------------------------

def _encode_officer_session(sess: OfficerCaseSession) -> str:
    import json

    docs = {
        fid: {
            "filename": d.get("filename", fid),
            "mime_type": d.get("mime_type", "application/octet-stream"),
            "claimed_type": d.get("claimed_type", ""),
            "content_b64": base64.b64encode(d.get("content") or b"").decode("ascii"),
        }
        for fid, d in (sess.documents or {}).items()
    }
    payload = {
        "channel_id": sess.channel_id,
        "channel": sess.channel,
        "ticket": sess.ticket,
        "request_id": sess.request_id,
        "license_name": sess.license_name,
        "validation": sess.validation,
        "documents": docs,
        "history": sess.history,
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
            "content": base64.b64decode(d["content_b64"]) if d.get("content_b64") else b"",
        }
        for fid, d in (raw.get("documents") or {}).items()
    }
    return OfficerCaseSession(
        channel_id=str(raw["channel_id"]),
        channel=str(raw.get("channel", CHANNEL_WHATSAPP)),
        ticket=str(raw.get("ticket", "")),
        request_id=raw.get("request_id"),
        license_name=raw.get("license_name"),
        validation=raw.get("validation"),
        documents=docs,
        history=list(raw.get("history") or []),
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
    pending, and `get_validation_summary` will report it gracefully)."""
    if not score or not isinstance(score, dict):
        return None

    issues: list[dict[str, Any]] = []
    result = score.get("result")
    if result is not None and getattr(result, "issues", None) is not None:
        # Rich path — map the SuitabilityResult.Issue objects.
        for it in result.issues:
            issues.append({
                "severity": getattr(it, "severity", "") or "",
                "field": getattr(it, "id", "") or "",
                "message": getattr(it, "title", "") or "",
                "related_docs": [],
            })
    else:
        # Fallback — the flattened {severity, message} list.
        for it in score.get("issues") or []:
            if isinstance(it, dict):
                issues.append({
                    "severity": str(it.get("severity", "") or ""),
                    "field": "",
                    "message": str(it.get("message", "") or ""),
                    "related_docs": [],
                })

    return {
        "score_percent": int(score.get("score_percent", 0) or 0),
        "status": str(score.get("status", "unverified") or "unverified"),
        "summary": str(score.get("summary", "") or ""),
        "issues": issues,
    }


def _documents_for_copilot(documents: list) -> dict[str, dict[str, Any]]:
    """Convert a list of guided_submission.SessionDocument (or anything with
    file_id/filename/mime_type/content/claimed_type attrs) into the dict the
    copilot's `_doc_context` expects, keyed by file_id."""
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
        }
    return out


# ===========================================================================
# Officer brief rendering
# ===========================================================================

_PORTAL_TRACK_URL = "https://portal.nolongin.com/track/{ticket}"


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

    wa = _demo_officer_wa()
    tg = _demo_officer_tg()
    if not wa and not tg:
        logger.warning(
            "officer notify: no demo officer channel configured "
            "(BIMA_OFFICER_WA_PHONE / BIMA_OFFICER_TG_CHAT) | ticket=%s",
            ticket,
        )
        return False

    validation = _score_to_validation(score)
    docs_map = _documents_for_copilot(documents or [])
    brief = _render_brief(
        ticket=ticket,
        license_name=license_name,
        applicant_name=applicant_name,
        validation=validation,
    )

    # Prefer WhatsApp when configured; Telegram otherwise. (Both could be set
    # in a multi-officer future; for the demo it's one channel.)
    channel = CHANNEL_WHATSAPP if wa else CHANNEL_TELEGRAM
    channel_id = wa or tg

    sess = OfficerCaseSession(
        channel_id=channel_id,
        channel=channel,
        ticket=ticket,
        request_id=request_id,
        license_name=license_name,
        validation=validation,
        documents=docs_map,
    )
    await _put_session(sess)

    sent = await _send(channel, channel_id, brief)
    logger.info(
        "officer notify | ticket=%s | channel=%s | officer=%s | sent=%s | "
        "has_score=%s | docs=%d",
        ticket, channel, _mask(channel_id), sent,
        validation is not None, len(docs_map),
    )
    return sent


# ===========================================================================
# 2) Officer reply → copilot bridge (inbound FAST-PATH)
# ===========================================================================


def is_officer_channel_id(channel_id: str) -> bool:
    """True when this inbound sender is the configured demo officer AND the
    bridge is enabled. Cheap pre-check so the routers can short-circuit."""
    if not is_enabled():
        return False
    cid = (channel_id or "").strip()
    if not cid:
        return False
    return cid in {_demo_officer_wa(), _demo_officer_tg()} - {""}


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

        result = await get_copilot().chat(
            message=msg,
            ticket=sess.ticket,
            history=history,
            officer_id=None,           # masked-logging only; we hold no JWT here
            validation=sess.validation,
            mode="officer",
            documents=sess.documents,  # in-session bytes → real get_doc_summary
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

    logger.info(
        "officer copilot bridge turn | officer=%s | ticket=%s | "
        "tool_calls=%d | reply_len=%d | closed=%s",
        _mask(channel_id), sess.ticket,
        len(tool_calls), len(reply), closed,
    )

    if closed:
        # A confirmed forward/decision write succeeded → the case left this
        # officer's desk. Clear the session so a stale ticket can't hijack the
        # officer's next, unrelated message for up to the 12h TTL. The desk
        # returns to "nothing queued" until the next brief arrives.
        await clear_session(channel_id)
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
