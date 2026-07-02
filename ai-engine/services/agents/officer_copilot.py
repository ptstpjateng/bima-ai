"""
Officer Copilot agent — the officer-facing AI partner per [[BIMA Vision]]
req #6/#8/#9 and [[Decisions]] §18 + §22.

Lives in the bima-admin case page side panel. It is each officer's
"right-hand man", scoped to their assigned cases, and its HEADLINE JOB is
helping the officer VALIDATE the submission in front of them (the killer
feature — Decisions §22). When an officer opens a permit submission, this
agent can:
  * surface the BIMA validator's score + prioritized issue list
    (`get_validation_summary` — the lead tool),
  * pull the live case record from SIAP,
  * compare two extracted fields (e.g. NIK on KTP vs NIK on NIB),
  * cite the relevant OSS-RBA regulation chunk for a KBLI/scenario,
  * (Phase 3) summarise an individual document attached to the submission.

It is a Gemini function-calling agent. The model decides which tools to call;
this module wires the tool implementations to Gemini's `functionDeclarations`
contract and runs the call loop (max 5 rounds) until the model produces a
plain-text reply.

Stateful sessions, stateless agent:
  The agent itself holds NO per-officer memory. Conversation durability is
  owned by admin-api's `copilot_session` table (one row per officer+ticket).
  admin-api loads the prior history, passes it to `chat()`, and persists the
  returned history. ai-engine stays stateless at the HTTP layer — see
  routers/copilot.py and [[Decisions]] §22.

Validation context injection:
  The validator needs the submission's document bytes to run, which the
  copilot does not hold. admin-api DOES already run the validator for the
  case page (`POST /case/{ticket}/validate`), so it injects the computed
  validation result into the copilot call. `get_validation_summary` reads
  that injected context — no second Gemini Vision pass, no quota burn.

Design notes:
  * The Indonesian system prompt forbids hallucinating case details. The
    officer is the human in the loop — they must be able to trust every
    fact in the reply. If a tool returns nothing, the model is instructed
    to say so explicitly rather than fabricate.
  * Tools are pure Python. We do not let the model run arbitrary code; the
    only side effect surface is the declared tools.
  * Reuses `services.siap_client.get_siap_client()` so the SIAP auth and
    timeout story stays uniform across BIMA's agents.
  * Reuses `services.agents.validator._normalize_name` so name comparison
    in the Copilot matches the validator's behaviour byte-for-byte. If a
    field starts to need its own normaliser, lift it to a shared helpers
    module rather than duplicating.
  * Reuses `services.rag_service.query_regulations` so officer citations
    come from the same ChromaDB collection the citizen chatbot reads.

Write actions (Wave 2 — officer-in-the-loop):
  * `forward_case` and `record_decision` are the copilot's two WRITE tools.
    They call SIAP's `forward` / `decision` endpoints via
    `services.siap_write_client`. Forwarding a case and — especially —
    approving/rejecting a licence are accountable OFFICER actions, so the
    copilot can never fire them on its own. Each write tool takes a
    `confirmed: bool` argument and REFUSES to execute unless it is true.
    The system prompt makes `confirmed=true` legal only after the officer
    has typed an explicit "ya"/"setuju"/"lanjutkan" in the turn immediately
    before. The copilot DRAFTS the action, the officer confirms, then — and
    only then — the write fires. See `_REQUIRES_CONFIRMATION` below and the
    "OFFICER-IN-THE-LOOP" block in the system prompt.
  * `get_case_log_notes` surfaces the prior-stage notes another desk wrote
    into SIAP's `license_log` (Vision req #11 — "the next desk sees what
    the previous desk said"). Forward/decision notes land in that same log,
    so this tool closes the chained-context loop.

Signature-assistant mode (Wave 4 — Vision req #13):
  * `chat(mode="signature")` selects the SIGNATURE-ASSISTANT variant — a
    Head-of-DPMPTSP-facing copilot for the FINAL signing decision. It is
    the SAME function-calling agent with the SAME read tools; only the
    system prompt changes (to frame the whole approval chain for a signing
    decision) and one extra READ tool is exposed: `get_siap_signing_link`.
  * BIMA does NOT sign anything. Digital signing / BSRE is owned entirely
    by SIAP Jateng (its "Tanda Tangan Berkas" / TTE feature). The
    signature-assistant's job is decision SUPPORT + a clean HANDOFF: it
    synthesises every prior desk's notes, the validator findings, and the
    regulation basis, then `get_siap_signing_link` returns a deep-link that
    opens SIAP's signing page for that case. The Head reviews with BIMA,
    clicks through, and signs in SIAP. No cryptography lives in BIMA.
  * The signature-assistant deliberately does NOT expose `forward_case` or
    `record_decision` — the only accountable action at this stage is the
    signature itself, and that happens in SIAP, not BIMA.

Not yet covered (deferred to Phase 3):
  * `get_doc_summary` returns a canned summary because SIAP does not
    currently expose a file-download endpoint. When that lands, swap the
    body for a real SIAP fetch + Gemini Vision OCR — the tool's contract
    (file_id → str) does not change.
  * Streaming responses. Today the endpoint waits for the full final
    reply. With multi-tool rounds the worst case is ~30s — acceptable for
    an internal officer tool, not for citizen WhatsApp.
"""

from __future__ import annotations

import contextvars
import difflib
import json
import logging
import os
import re
from typing import Any, Optional
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

from services.agents.validator import _normalize_name
from services.rag_service import query_regulations
from services.siap_client import get_siap_client
from services.siap_tools import siap_get_status_timeline
from services.siap_write_client import get_siap_write_client

load_dotenv()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration — mirrors gemini_vision.py so they can be flipped together.
# ---------------------------------------------------------------------------

_GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
_GEMINI_TOOL_MODEL: str = os.getenv(
    "GEMINI_TOOL_MODEL",
    "models/gemini-2.5-flash",
)
_TOOL_TIMEOUT_SECONDS: float = float(os.getenv("GEMINI_TOOL_TIMEOUT_SECONDS", "30"))

# SIAP signing deep-link base (Wave 4 / Vision req #13). The signature-
# assistant hands the Head of DPMPTSP off to SIAP Jateng to perform the
# actual digital signature (TTE/BSRE). This is the public base of SIAP's
# Filament admin panel — the "Tanda Tangan Berkas" resource lives at
# `<base>/admin/tanda-tangan-berkas`. Defaults to Beta-SIAP so a missing
# env var still produces a working rehearsal link rather than a dead one.
#   Beta:       https://beta-siap.nolongin.com
#   Production: https://perizinan.jatengprov.go.id  (gated — do not auto-use)
_SIAP_SIGNING_BASE: str = os.getenv(
    "SIAP_SIGNING_URL_BASE", "https://beta-siap.nolongin.com"
).rstrip("/")

# Hard ceiling on tool-call rounds inside a single chat() invocation. Five is
# generous — typical answer is 1–2 tool calls. If we hit the cap we return
# whatever text the model has emitted so far plus a short note.
_MAX_TOOL_ROUNDS = 5


def is_configured() -> bool:
    return bool(_GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# System prompt — pinned in Indonesian per the bima-admin officer locale.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = (
    "Anda adalah BIMA, co-pilot validasi untuk petugas DPMPTSP Jateng. "
    "Anda BUKAN chatbot umum — tugas utama Anda adalah membantu petugas "
    "MEMVALIDASI berkas permohonan izin yang sedang dibuka.\n\n"
    "PRINSIP KERJA:\n"
    "1. Validasi adalah prioritas. Di awal sesi, atau ketika petugas "
    "belum jelas mau apa, panggil `get_validation_summary` lalu sampaikan "
    "skor validasi BIMA dan pandu petugas menelusuri masalah dari yang "
    "paling parah lebih dahulu (critical → high → medium → low).\n"
    "2. Selalu gunakan tool untuk mendapatkan data faktual sebelum "
    "menjawab. Jangan pernah mengarang detail kasus, skor, nama pemohon, "
    "atau referensi peraturan — selalu kutip dari hasil tool. Jika tool "
    "tidak mengembalikan data, katakan terus terang.\n"
    "3. Setelah menjelaskan sebuah masalah, tawarkan langkah konkret: "
    "menelusuri dokumen tertentu, membandingkan field (mis. NIK di KTP vs "
    "NIB lewat `compare_field`), atau mencari dasar hukum lewat "
    "`cite_regulation`.\n"
    "4. Saat petugas membuka kasus, panggil `get_case_log_notes` untuk "
    "membaca catatan dari meja/tahap sebelumnya. Sampaikan catatan itu "
    "supaya petugas saat ini tahu apa yang dikatakan petugas sebelumnya.\n"
    "5. Jawab ringkas, dalam Bahasa Indonesia formal, dan langsung ke "
    "inti — petugas sedang bekerja cepat di antrean berkas.\n\n"
    "=== ATURAN MUTLAK — TINDAKAN YANG MENGUBAH DATA (forward & decision) ===\n"
    "Anda memiliki dua tool yang MENGUBAH data di SIAP: `forward_case` "
    "(meneruskan berkas ke meja berikutnya) dan `record_decision` "
    "(mencatat keputusan TERIMA/TOLAK izin). Tool-tool ini adalah "
    "tindakan resmi yang menjadi TANGGUNG JAWAB PETUGAS, bukan tanggung "
    "jawab Anda. Anda HANYA menyusun draf — petugaslah yang memutuskan.\n\n"
    "Alur wajib dua langkah:\n"
    "  LANGKAH 1 (USUL): Ketika petugas meminta meneruskan berkas atau "
    "menerima/menolak izin, JANGAN langsung memanggil tool. Pertama, "
    "nyatakan ulang dengan TEPAT apa yang akan dilakukan: tiket mana, "
    "tindakan apa (teruskan / TERIMA / TOLAK), dan isi catatannya. Lalu "
    "minta petugas mengonfirmasi secara eksplisit, mis. 'Ketik \"ya\" "
    "untuk menjalankan.'\n"
    "  LANGKAH 2 (EKSEKUSI): Panggil tool dengan `confirmed=true` HANYA "
    "jika pesan petugas TEPAT SEBELUM giliran ini berisi konfirmasi "
    "eksplisit ('ya', 'setuju', 'lanjutkan', 'benar', 'ok jalankan') "
    "atas usulan yang persis sama.\n\n"
    "DILARANG KERAS:\n"
    "  - Memanggil `forward_case`/`record_decision` dengan `confirmed=true` "
    "tanpa konfirmasi eksplisit petugas di giliran sebelumnya.\n"
    "  - Menganggap permintaan awal petugas ('tolong teruskan berkas ini') "
    "sebagai konfirmasi. Itu adalah PERMINTAAN, bukan konfirmasi — Anda "
    "tetap wajib menyusun draf dan menunggu jawaban 'ya'.\n"
    "  - Mengarang isi catatan keputusan. Catatan harus berasal dari "
    "petugas atau dari temuan validasi yang nyata.\n"
    "Jika ragu apakah petugas sudah mengonfirmasi, ANGGAP BELUM: panggil "
    "tool dengan `confirmed=false` (itu hanya akan mengembalikan draf) "
    "atau minta konfirmasi ulang. Meneruskan/memutuskan berkas tanpa izin "
    "petugas adalah kesalahan serius.\n\n"
    "Konteks sesi:\n"
    "- Tiket yang sedang dianalisis: {ticket}\n"
    "- Mulailah dengan `get_validation_summary` untuk melihat temuan "
    "validator. Gunakan `get_case_full` bila perlu konteks SIAP, "
    "`get_case_log_notes` untuk catatan tahap sebelumnya, lalu tool lain "
    "sesuai pertanyaan petugas."
)


# ---------------------------------------------------------------------------
# Signature-assistant system prompt — Wave 4, Vision req #13.
#
# The Head of DPMPTSP signs the final licence. This variant frames the SAME
# read tools for the SIGNING decision: synthesise the whole approval chain,
# surface anything a prior desk flagged, then hand off to SIAP to sign. BIMA
# never performs the signature — SIAP owns TTE/BSRE.
# ---------------------------------------------------------------------------

_SIGNATURE_SYSTEM_PROMPT_TEMPLATE = (
    "Anda adalah BIMA, asisten tanda tangan untuk Kepala DPMPTSP Jawa "
    "Tengah. Pejabat yang sedang Anda dampingi akan MENANDATANGANI "
    "(mengesahkan) izin final. Tugas Anda adalah memberi beliau gambaran "
    "LENGKAP rantai persetujuan supaya keputusan tanda tangan diambil "
    "dengan keyakinan penuh.\n\n"
    "PRINSIP KERJA:\n"
    "1. Di awal sesi, susun ringkasan keputusan: panggil "
    "`get_case_log_notes` untuk membaca SELURUH catatan dari setiap meja "
    "sebelumnya (rantai license_log), `get_validation_summary` untuk skor "
    "validator BIMA beserta masalah yang ditandai, dan `get_case_full` "
    "untuk konteks faktual permohonan. Sajikan semuanya sebagai satu "
    "sintesis yang runut — dari pengajuan hingga meja terakhir.\n"
    "2. SOROT secara eksplisit apa pun yang ditandai bermasalah oleh meja "
    "sebelumnya atau oleh validator (critical/high lebih dahulu). Jika ada "
    "temuan yang belum tuntas, sampaikan terus terang sebagai bahan "
    "pertimbangan sebelum tanda tangan — jangan menutupinya.\n"
    "3. Gunakan `cite_regulation` bila Kepala bertanya soal dasar hukum, "
    "persyaratan, atau kewajiban yang melekat pada izin.\n"
    "4. Selalu gunakan tool untuk fakta. Jangan pernah mengarang skor, "
    "nama pemohon, isi catatan meja, atau referensi peraturan. Jika tool "
    "tidak mengembalikan data, katakan terus terang.\n"
    "5. Jawab ringkas, dalam Bahasa Indonesia formal dan santun sesuai "
    "kedudukan pejabat.\n\n"
    "=== TANDA TANGAN DILAKUKAN DI SIAP JATENG, BUKAN DI BIMA ===\n"
    "BIMA TIDAK menandatangani dokumen apa pun. Penandatanganan digital "
    "(TTE/BSRE) sepenuhnya milik aplikasi SIAP Jateng. Peran Anda adalah "
    "PENDUKUNG KEPUTUSAN dan PENGHUBUNG.\n"
    "Ketika Kepala sudah selesai meninjau dan siap menandatangani — atau "
    "menanyakan cara/tempat tanda tangan — panggil `get_siap_signing_link` "
    "untuk mendapatkan tautan halaman tanda tangan SIAP bagi berkas ini, "
    "lalu sampaikan tautan itu dengan ajakan jelas: "
    "'Tanda tangani di SIAP Jateng'. Jelaskan singkat bahwa proses TTE "
    "dilakukan di SIAP menggunakan passphrase BSrE milik beliau.\n"
    "DILARANG mengaku telah menandatangani berkas, atau menjanjikan BIMA "
    "akan menandatanganinya. BIMA hanya menyiapkan konteks dan tautan.\n\n"
    "Konteks sesi:\n"
    "- Tiket yang akan ditandatangani: {ticket}\n"
    "- Mulailah dengan sintesis rantai persetujuan (`get_case_log_notes` + "
    "`get_validation_summary` + `get_case_full`), lalu jawab pertanyaan "
    "Kepala dan tawarkan tautan tanda tangan SIAP saat beliau siap."
)


# Copilot operating modes. "officer" is the validation-first desk copilot
# (Wave 1-2); "signature" is the Head-of-DPMPTSP signing assistant (Wave 4).
_MODE_OFFICER = "officer"
_MODE_SIGNATURE = "signature"
_VALID_MODES = frozenset({_MODE_OFFICER, _MODE_SIGNATURE})


# ---------------------------------------------------------------------------
# Per-call validation context.
#
# `get_validation_summary` needs the case's validation result, but the
# validator requires the submission's document bytes — which the copilot does
# not hold. admin-api already computes the validation result for the case page
# and injects it into each `/v1/copilot/chat` call. We carry that injected
# payload to the tool via a ContextVar so the tool stays a plain module-level
# function (consistent with the other tools) while remaining async-task-safe:
# `chat()` sets the var per invocation, the tool reads it, and a token reset
# restores the previous value when the turn ends.
# ---------------------------------------------------------------------------

_validation_context: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "officer_copilot_validation_context", default=None
)

# ---------------------------------------------------------------------------
# In-session document context — the chat-bridge demo path (June-4 slice).
#
# SIAP exposes no file-download endpoint, so `get_doc_summary` normally
# returns a canned string. BUT in the WhatsApp/Telegram demo flow the citizen
# sent their documents straight into the chat session, so BIMA holds the raw
# bytes. The officer bridge injects those bytes here (keyed by file_id) for
# the duration of one `chat()` turn, and `get_doc_summary` runs Gemini Vision
# over them — a real answer to "apa isi proposalnya?".
#
# Shape: {file_id: {"filename": str, "mime_type": str, "content": bytes,
#                    "claimed_type": str}}
# Empty / None → no in-session docs; `get_doc_summary` falls back to the
# canned string (admin-api dashboard path, where SIAP file fetch isn't wired).
# Bytes are never logged.
# ---------------------------------------------------------------------------
_doc_context: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "officer_copilot_doc_context", default=None
)

# ---------------------------------------------------------------------------
# Out-of-band document-send channel (Task G — officer requests the real file).
#
# A tool can only return a STRING to the model, but delivering a document is an
# out-of-band side effect (host the bytes at /dl/{token} + push a WhatsApp
# document message). So `send_document` does NOT send the file itself — it
# RESOLVES the requested doc to a file_id and records that id here. `chat()`
# drains this list after the turn and surfaces it to the caller as
# `result["documents_to_send"]`; the officer bridge then performs the actual
# send on the officer's channel. Same ContextVar pattern as `_doc_context`, so
# it stays a plain module-level tool and remains async-task-safe (each turn
# gets its own list; a token reset restores the previous value).
# ---------------------------------------------------------------------------
_docs_to_send_context: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "officer_copilot_docs_to_send", default=None
)

# Severity ladder — kept in sync with admin/src/lib/case-types.ts SEVERITY_ORDER
# and ai-engine/services/agents/validator.py IssueLevel. Lower = worse.
_SEVERITY_RANK: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


# ---------------------------------------------------------------------------
# Tool implementations.
# ---------------------------------------------------------------------------


def _documents_digest_from_ctx() -> list[dict[str, Any]]:
    """Return the compact per-document read digest carried on the validation
    context, if any (Task F).

    The bridge derives a `documents_digest` from the rich SuitabilityResult at
    officer-case creation — {filename, detected_type, has_meterai, confidence,
    matches, claimed_type} per doc — and threads it through the same
    `validation` dict the copilot receives, so "what BIMA read per doc" (type +
    meterai) survives a Redis round-trip / process restart even though the raw
    SuitabilityResult is stripped before Redis. We pass it through unchanged;
    the bridge already masked any PII in the fields when it built the digest.
    Returns [] when no digest is bound.
    """
    ctx = _validation_context.get()
    if not ctx or not isinstance(ctx, dict):
        return []
    digest = ctx.get("documents_digest")
    if not isinstance(digest, list):
        return []
    out: list[dict[str, Any]] = []
    for d in digest:
        if not isinstance(d, dict):
            continue
        out.append({
            "filename": d.get("filename", "") or "",
            "detected_type": d.get("detected_type", "") or "",
            "claimed_type": d.get("claimed_type", "") or "",
            "has_meterai": d.get("has_meterai"),
            "confidence": d.get("confidence"),
            "matches": d.get("matches"),
        })
    return out


def get_validation_summary() -> dict:
    """
    Return the BIMA validator's findings for the case currently in session:
    the completion score and the issue list sorted worst-first.

    This is the copilot's LEAD tool — Decisions §22 makes validation help the
    headline job. The validation result is supplied by admin-api (which runs
    the validator for the case page) and injected via `_validation_context`;
    we never re-run Gemini Vision here.

    Returns a structured dict the model can narrate verbatim:
      {
        "available": bool,
        "score_percent": int,        # 0-100
        "status": str,               # ready | minor_issues | major_issues | unverified
        "summary": str,              # one-paragraph Indonesian
        "issue_count": int,
        "issues": [ {severity, field, message, related_docs}, ... ]  # worst-first
        "documents_read": [ {filename, detected_type, claimed_type,
                             has_meterai, confidence, matches}, ... ]
      }
    `documents_read` is the per-doc digest of what BIMA actually read (type +
    meterai per document) — present even after a restart, so the officer can
    always see it. On miss returns `{"available": False, "note": "..."}` so the
    model is told to ask the officer to run the validator rather than
    fabricating a score.
    """
    ctx = _validation_context.get()
    if not ctx or not isinstance(ctx, dict):
        return {
            "available": False,
            "note": (
                "Hasil validasi belum tersedia untuk tiket ini. Minta petugas "
                "menjalankan validator pada halaman kasus terlebih dahulu."
            ),
        }

    raw_issues = ctx.get("issues") or []
    issues: list[dict[str, Any]] = []
    for it in raw_issues:
        if not isinstance(it, dict):
            continue
        issues.append({
            "severity": str(it.get("severity", "") or "").lower(),
            "field": it.get("field", "") or "",
            "message": it.get("message", "") or "",
            "related_docs": it.get("related_docs", []) or [],
        })
    # Worst-first so the model walks the officer through critical items first.
    issues.sort(key=lambda i: _SEVERITY_RANK.get(i["severity"], 99))

    score_percent = ctx.get("score_percent")
    if score_percent is None and ctx.get("score") is not None:
        try:
            score_percent = int(round(float(ctx["score"]) * 100))
        except (TypeError, ValueError):
            score_percent = None

    return {
        "available": True,
        "score_percent": score_percent if score_percent is not None else 0,
        "status": ctx.get("status", "unverified") or "unverified",
        "summary": ctx.get("summary", "") or "",
        "issue_count": len(issues),
        "issues": issues,
        "documents_read": _documents_digest_from_ctx(),
    }


async def get_case_full(ticket: str) -> dict:
    """
    Fetch the SIAP monitoring-berkas record for a ticket. Reuses the same
    client the WhatsApp fast-path uses, so the auth + timeout story is
    identical.

    Also attaches `documents_read` — the compact per-doc digest of what BIMA
    read at submission (type + meterai per document, Task F) — from the
    validation context, so the officer sees it alongside the case record even
    after a restart. Absent (empty list) when no digest is bound.

    Returns the record dict on hit, or `{"found": False, "ticket": ticket}`
    on miss / SIAP unavailable. We do not raise — the model must always
    get a structured tool result so it can phrase the answer correctly.
    """
    digest = _documents_digest_from_ctx()
    client = get_siap_client()
    record = await client.get_status_by_ticket(ticket)
    if not record:
        return {"found": False, "ticket": ticket, "documents_read": digest}
    return {"found": True, **record, "documents_read": digest}


# Gemini Vision schema for a free-text document summary. JSON-mode (the
# vision client only returns JSON), so we wrap the prose in a `summary` field.
_DOC_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "A concise Indonesian summary (3-5 sentences) of what this "
                "document is and the key information it contains."
            ),
        },
    },
    "required": ["summary"],
}

_DOC_SUMMARY_PROMPT = (
    "Anda asisten petugas perizinan. Ringkas dokumen terlampir dalam Bahasa "
    "Indonesia: jenis dokumen, pihak/nama yang tercantum, poin penting "
    "(nomor, tanggal, materai bila ada), dan apakah dokumen tampak lengkap. "
    "Jangan mengarang isi yang tidak terlihat. 3-5 kalimat."
)


def _digest_summary_for_ref(doc_ref: str) -> Optional[str]:
    """Best-effort summary of a document from the RETAINED read-digest text
    when its bytes are gone (post-restart / Redis rehydrate stripped them).

    The digest (Task F) survives a Redis round-trip while the raw bytes may not,
    so "what BIMA read" (detected type, meterai, confidence) is still available
    even when a Vision re-pass is impossible. We match `doc_ref` against the
    digest's detected/claimed type + filename (same tolerance as
    `_resolve_doc_ref`) and phrase a short factual line from it — never a
    dead-end. Returns None when no digest entry matches.
    """
    digest = _documents_digest_from_ctx()
    if not digest:
        return None

    ref = _norm_ref(doc_ref)
    match: Optional[dict[str, Any]] = None
    if ref:
        for entry in digest:
            labels = (
                _norm_ref(entry.get("detected_type")),
                _norm_ref(entry.get("claimed_type")),
                _norm_ref(entry.get("filename")),
            )
            if any(lbl and (ref == lbl or ref in lbl or lbl in ref) for lbl in labels):
                match = entry
                break
    # A single-document submission: honour a summary request even without a
    # crisp label match, so the officer still gets what BIMA read.
    if match is None and len(digest) == 1:
        match = digest[0]
    if match is None:
        return None

    detected = str(match.get("detected_type") or "").strip()
    filename = str(match.get("filename") or "").strip()
    label = filename or detected or doc_ref
    bits: list[str] = []
    if detected:
        bits.append(f"BIMA membacanya sebagai *{detected}*")
    conf = match.get("confidence")
    if isinstance(conf, (int, float)):
        bits.append(f"keyakinan {int(round(float(conf) * 100))}%")
    if match.get("has_meterai") is True:
        bits.append("materai terdeteksi")
    elif match.get("has_meterai") is False:
        bits.append("materai tidak terdeteksi")
    if match.get("matches") is True:
        bits.append("jenis sesuai dengan yang dinyatakan pemohon")
    elif match.get("matches") is False:
        bits.append("jenis TIDAK cocok dengan label pemohon")
    detail = "; ".join(bits) if bits else "hanya metadata dasar yang tersimpan"
    return (
        f"📄 {label}: berkas aslinya tidak lagi tersedia di sesi ini, jadi "
        f"saya belum bisa membaca ulang isinya. Dari catatan pemeriksaan: "
        f"{detail}."
    )


async def get_doc_summary(doc_ref: str = "", file_id: str = "") -> str:
    """
    Return a short Indonesian summary of one supporting document. `doc_ref`
    may be a document TYPE/NAME ("KTP", "surat permohonan", "proposal") OR a
    literal file_id — the officer speaks in plain names, not ids. `file_id` is
    accepted as an alias for backward-compat (the tool's earlier signature) so
    a model that still emits `file_id` keeps working.

    DEMO PATH (chat bridge): when the citizen sent the document into the
    WhatsApp/Telegram session, BIMA holds the raw bytes and the officer bridge
    injects them via `_doc_context`. We resolve the ref to a file_id, run
    Gemini Vision over the in-session bytes, and return a real summary — a
    genuine answer to "apa isi proposalnya?".

    GRACEFUL DEGRADE: if the bytes are genuinely gone (post-restart rehydrate
    dropped them) but BIMA still holds the compact read-digest, we summarise
    from that digest text and SAY the original can't be re-read — never a
    dead-end on "tidak memiliki ID file".

    FALLBACK (admin dashboard): when neither bytes nor digest are bound (SIAP
    has no file-download endpoint), we return the canned placeholder.
    """
    doc_ref = (doc_ref or file_id or "").strip()
    ctx = _doc_context.get()
    if ctx and isinstance(ctx, dict):
        file_id = _resolve_doc_ref(ctx, doc_ref)
        doc = ctx.get(file_id) if file_id else None
        content = doc.get("content") if doc else None
        if doc is not None and content:
            from services.gemini_vision import extract_structured, is_configured

            if not is_configured():
                return (
                    f"Ringkasan dokumen {doc.get('filename', file_id)} belum "
                    "dapat dibuat — Gemini Vision belum dikonfigurasi di server."
                )
            parsed = await extract_structured(
                image_bytes=content,
                mime_type=doc.get("mime_type", "application/octet-stream"),
                prompt=_DOC_SUMMARY_PROMPT,
                response_schema=_DOC_SUMMARY_SCHEMA,
            )
            if parsed and isinstance(parsed, dict) and parsed.get("summary"):
                label = doc.get("filename", file_id)
                return f"📄 {label}: {str(parsed['summary']).strip()}"
            # Vision failed on real bytes — try the digest before giving up.
            degraded = _digest_summary_for_ref(doc_ref)
            if degraded:
                return degraded
            return (
                f"Tidak dapat membaca dokumen {doc.get('filename', file_id)} — "
                "pastikan berkas jelas (foto/PDF tidak buram)."
            )

        # Resolved to a doc whose bytes are gone (or didn't resolve at all):
        # degrade to the retained digest rather than dead-ending on "no id".
        degraded = _digest_summary_for_ref(doc_ref)
        if degraded:
            return degraded

        # Nothing to summarise from — tell the model what IS available so it can
        # re-ask, listing human labels (not opaque ids) where we have them.
        labels = sorted(
            (
                d.get("filename")
                or d.get("detected_type")
                or d.get("claimed_type")
                or fid
            )
            for fid, d in ctx.items()
        )
        available = ", ".join(labels) or "(tidak ada)"
        return (
            f"Dokumen '{doc_ref}' tidak ditemukan di sesi ini. "
            f"Dokumen yang tersedia: {available}."
        )

    # No in-session bytes bound — try the retained digest (survives a restart
    # even when the doc context does not), then the canned placeholder.
    degraded = _digest_summary_for_ref(doc_ref)
    if degraded:
        return degraded
    return (
        f"[Ringkasan dokumen '{doc_ref}' belum tersedia di jalur ini]. "
        "Endpoint pengunduhan dokumen SIAP belum diaktifkan. Pada alur chat "
        "(WhatsApp/Telegram) di mana dokumen dikirim langsung ke BIMA, "
        "ringkasan dibuat otomatis dengan Gemini Vision."
    )


def _norm_ref(s: Any) -> str:
    """Space/underscore-agnostic, case-insensitive label normaliser. Shared by
    the doc-ref resolver and the digest fallback so "surat pesanan",
    "Surat_Pesanan", and "SURAT  PESANAN" all collapse to one form."""
    return re.sub(r"[\s_]+", " ", str(s or "")).strip().lower()


def _resolve_doc_ref(ctx: dict, doc_ref: str) -> Optional[str]:
    """Resolve a human `doc_ref` ("KTP", "Surat Pesanan", a filename fragment)
    OR a literal file_id to a file_id present in the in-session doc context.

    Matching is case-insensitive and tolerant of the label formatting the
    validator uses (underscores in detected/claimed types, e.g. "Surat_Pesanan"
    vs the officer's "surat pesanan"). Precedence (most specific → least):
      1. exact file_id key,
      2. exact type-label equality — detected_type (what BIMA READ the doc to
         be) OR claimed_type (what the citizen DECLARED) equals the ref. Detected
         is tried first: "KTP" must resolve even when the citizen mislabelled the
         upload, which is exactly a case the officer is reviewing.
      3. filename substring, then type-label substring (detected then claimed).
    Returns the file_id on a hit, or None when nothing matches.

    `detected_type` is present when the bridge cross-referenced the read digest
    onto the doc context; it may be absent on the admin-dashboard path — the
    resolver simply skips empty labels, so it degrades to claimed_type+filename.
    """
    if not ctx or not doc_ref:
        return None

    ref = _norm_ref(doc_ref)

    # 1) A literal file_id the officer (or model) passed straight through.
    if doc_ref in ctx:
        return doc_ref
    if not ref:
        return None

    # 2) Exact type-label equality — detected first (BIMA's read), then claimed.
    for label_key in ("detected_type", "claimed_type"):
        for fid, doc in ctx.items():
            if _norm_ref(doc.get(label_key)) == ref:
                return fid

    # 3) Substring match — filename first (most specific), then type labels.
    for fid, doc in ctx.items():
        if ref in _norm_ref(doc.get("filename")):
            return fid
    for label_key in ("detected_type", "claimed_type"):
        for fid, doc in ctx.items():
            label = _norm_ref(doc.get(label_key))
            if label and (ref in label or label in ref):
                return fid

    return None


def send_document(doc_ref: str) -> str:
    """Officer asks to RECEIVE/VIEW a specific uploaded document ("kirim
    KTP-nya", "boleh saya lihat surat pesanannya").

    This tool does NOT send the file — a tool can only return text, and the
    delivery is an out-of-band side effect. It resolves `doc_ref` (a document
    label like "KTP"/"Surat Pesanan", OR a file_id) against the in-session
    documents, records the resolved file_id in `_docs_to_send_context` so the
    officer bridge can push the real file on the officer's channel, and returns
    a short confirmation for the model to relay. On no match it records nothing
    and tells the officer the document isn't on this submission.
    """
    ctx = _doc_context.get()
    if not ctx or not isinstance(ctx, dict):
        # No in-session bytes bound (admin dashboard path) — nothing to send.
        return (
            f"Dokumen '{doc_ref}' tidak dapat dikirim: berkas tidak tersedia "
            "pada sesi ini."
        )

    file_id = _resolve_doc_ref(ctx, doc_ref)
    if file_id is None:
        return f"Dokumen '{doc_ref}' tidak ditemukan pada pengajuan ini."

    doc = ctx.get(file_id) or {}
    label = doc.get("filename") or doc.get("claimed_type") or file_id

    queue = _docs_to_send_context.get()
    if isinstance(queue, list):
        if file_id not in queue:
            queue.append(file_id)
    else:
        # No queue bound (e.g. tool exercised in isolation) — the resolution
        # still succeeds; only the out-of-band send is skipped.
        logger.debug("send_document resolved but no send queue bound | file_id=%s", file_id)

    return f"Baik, saya kirimkan dokumen {label} ke chat ini."


def compare_field(doc_a: dict, doc_b: dict, field: str) -> dict:
    """
    Pure compare of one field across two extracted-document dicts.

    Uses validator-aligned name normalisation when the field is name-like
    (heuristic: field contains 'nama' or 'name') so two KTPs of the same
    person with different honorifics still compare equal.
    """
    raw_a = doc_a.get(field, "") if isinstance(doc_a, dict) else ""
    raw_b = doc_b.get(field, "") if isinstance(doc_b, dict) else ""

    is_name_like = bool(re.search(r"nama|name", field, re.IGNORECASE))

    if is_name_like:
        norm_a, norm_b = _normalize_name(raw_a), _normalize_name(raw_b)
    else:
        norm_a = re.sub(r"\s+", " ", str(raw_a or "")).strip().upper()
        norm_b = re.sub(r"\s+", " ", str(raw_b or "")).strip().upper()

    if not norm_a or not norm_b:
        return {
            "equal": False,
            "similarity": 0.0,
            "note": (
                "Salah satu nilai kosong — tidak bisa dibandingkan. "
                f"doc_a.{field}={raw_a!r}, doc_b.{field}={raw_b!r}."
            ),
        }

    similarity = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()
    equal = norm_a == norm_b

    if equal:
        note = f"Nilai identik setelah normalisasi: {norm_a!r}."
    elif similarity >= 0.85:
        note = (
            f"Mirip ({int(similarity * 100)}%) tapi tidak identik. "
            f"doc_a={raw_a!r}, doc_b={raw_b!r}."
        )
    else:
        note = (
            f"Berbeda ({int(similarity * 100)}% kemiripan). "
            f"doc_a={raw_a!r}, doc_b={raw_b!r}."
        )

    return {
        "equal": equal,
        "similarity": round(similarity, 4),
        "note": note,
    }


def cite_regulation(query: str) -> list[dict]:
    """
    Search ChromaDB for the top 3 most relevant OSS-RBA chunks. Returns a
    list of `{title, snippet, citation}` dicts that the model can quote
    verbatim. Empty list when ChromaDB is empty or unreachable — the model
    is told to handle that by saying it cannot find a citation.
    """
    chunks = query_regulations(query, n_results=3)
    out: list[dict] = []
    for chunk in chunks:
        kbli = chunk.get("kbli_code", "") or ""
        section = chunk.get("section", "") or "umum"
        skala = chunk.get("skala", "") or ""
        source_url = chunk.get("source_url", "") or ""

        title_bits = [f"KBLI {kbli}" if kbli else "OSS-RBA"]
        if section:
            title_bits.append(section)
        if skala:
            title_bits.append(skala)
        title = " — ".join(title_bits)

        content = chunk.get("content", "") or ""
        snippet = content[:200] + ("…" if len(content) > 200 else "")

        citation = source_url or (f"KBLI {kbli}" if kbli else "OSS-RBA")

        out.append({
            "title": title,
            "snippet": snippet,
            "citation": citation,
        })
    return out


# ---------------------------------------------------------------------------
# Chained-context read tool — Vision req #11.
#
# When an officer opens a case, the next desk must see what the previous desk
# said. SIAP's forward/decision endpoints append their notes to
# `ptsp.license_log`; `siap_get_status_timeline` already reads that log
# (per-step `status` + `description` + owning desk). This tool reuses it and
# reshapes the result into a clean "prior-stage notes" view the model can
# narrate. We do NOT re-implement the SQL — siap_tools owns that.
# ---------------------------------------------------------------------------

# license_log statuses that carry an officer-written note worth surfacing.
_NOTE_BEARING_STATUSES = {"APPROVED", "REJECTED", "SUBMITTED", "VERIFIKASI", "DITOLAK"}


async def get_case_log_notes(ticket: str) -> dict:
    """
    Return the chain of notes prior desks wrote into SIAP's `license_log`
    for this case, so the officer now holding the file sees the handover
    context (Vision req #11).

    Built on `siap_get_status_timeline` — every forward/decision note lands
    in `license_log`, which that tool already reads. We reshape its step
    list into `{step, status, owner_desk, note, entered_on}` entries,
    oldest-first, dropping steps with no note text.

    Returns:
      {
        "found": bool,
        "ticket": str,
        "request_id": int | None,   # also the id the write tools need
        "note_count": int,
        "notes": [ {step, status, owner_desk, note, entered_on}, ... ],
      }
    On miss / SIAP unavailable: found=False with a `note` field. Never raises.
    """
    timeline = await siap_get_status_timeline(ticket)
    if not timeline.get("found"):
        return {
            "found": False,
            "ticket": ticket,
            "note": timeline.get(
                "note", "Riwayat catatan untuk tiket ini tidak tersedia."
            ),
        }

    notes: list[dict[str, Any]] = []
    for step in timeline.get("steps") or []:
        if not isinstance(step, dict):
            continue
        desc = str(step.get("step", "") or "").strip()
        status = str(step.get("status", "") or "").strip().upper()
        # The step `description` from license_log is the officer's note.
        # Skip purely structural rows with no human text.
        if not desc or desc.lower() in {"tahapan", "-"}:
            continue
        notes.append({
            "step": desc,
            "status": status,
            "owner_desk": step.get("owner_desk"),
            "note": desc,
            "entered_on": step.get("entered_on"),
        })

    return {
        "found": True,
        "ticket": timeline.get("no_tiket", ticket),
        "request_id": timeline.get("request_id"),
        "note_count": len(notes),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Signature handoff tool — Wave 4, Vision req #13.
#
# BIMA does NOT sign. SIAP Jateng owns the digital signature (TTE/BSRE) via
# its "Tanda Tangan Berkas" Filament resource. This tool builds the deep-link
# that opens SIAP's signing surface for a specific case so the Head of
# DPMPTSP can review with BIMA, then click through and sign in SIAP.
#
# SIAP route (verified against the SIAP repo, 2026-05-21):
#   * Filament panel:   AdminPanelProvider → ->id('admin') ->path('admin')
#   * Resource:         App\Filament\Resources\TandaTanganBerkasResource
#                       protected static ?string $slug = 'tanda-tangan-berkas';
#                       single page ManageTandaTanganBerkas at route '/'.
#   * The TTE action is a Filament table EditAction named 'tte' on that page;
#     the table column `properties.ticket` is `searchable`.
# So the signing page is `<base>/admin/tanda-tangan-berkas`, and we pre-filter
# it to the one case by appending Filament's table-search query param
# (`?tableSearch=<ticket>`) — the ticket is the stable, public-knowledge
# handle (BIMA does not own SIAP's internal `request_id` record key).
# ---------------------------------------------------------------------------

# Filament's table-search query parameter. Filament v3 hydrates the table
# search box from `?tableSearch=`, so the Head lands on the signing list
# already filtered to this one ticket.
_SIAP_SIGNING_PATH = "/admin/tanda-tangan-berkas"


def get_siap_signing_link(ticket: str) -> dict:
    """
    Build the deep-link that opens SIAP Jateng's signing page for a case.

    BIMA never signs — SIAP owns TTE/BSRE. This tool is the HANDOFF: it
    returns the URL of SIAP's "Tanda Tangan Berkas" Filament page,
    pre-filtered to `ticket` via Filament's `tableSearch` query param, so
    the Head of DPMPTSP clicks through and signs in SIAP itself.

    Args:
      ticket: the SIAP ticket of the case to sign.

    Returns:
      {
        "available": bool,
        "ticket": str,
        "url": str,            # the SIAP signing deep-link
        "label": str,          # the call-to-action text, Indonesian
        "note": str,           # one-line guidance for the model to narrate
      }
    On a missing ticket returns `{"available": False, "note": "..."}`.
    """
    ticket = (ticket or "").strip()
    if not ticket:
        return {
            "available": False,
            "note": (
                "Nomor tiket tidak tersedia — tautan tanda tangan SIAP "
                "tidak dapat dibuat."
            ),
        }

    # Zero-padded 9-digit ticket matches what SIAP stores in
    # `license_request.properties->>'ticket'`, so the table search hits.
    padded = ticket.zfill(9) if ticket.isdigit() else ticket
    url = f"{_SIAP_SIGNING_BASE}{_SIAP_SIGNING_PATH}?tableSearch={quote(padded)}"
    return {
        "available": True,
        "ticket": padded,
        "url": url,
        "label": "Tanda tangani di SIAP Jateng",
        "note": (
            "Tautan ini membuka halaman Tanda Tangan Berkas di SIAP Jateng "
            f"yang sudah tersaring ke tiket {padded}. Penandatanganan "
            "(TTE/BSRE) dilakukan di SIAP menggunakan passphrase BSrE "
            "milik penandatangan — BIMA tidak menandatangani dokumen."
        ),
    }


# ---------------------------------------------------------------------------
# Write tools — Wave 2. OFFICER-IN-THE-LOOP.
#
# `forward_case` and `record_decision` are the only side-effecting tools in
# the copilot. They reach SIAP through `services.siap_write_client`. The hard
# guard against an unconfirmed write is a `confirmed: bool` argument that is
# checked HERE, in Python — it does not rely on the model behaving. When
# `confirmed` is false (or absent) the tool returns a DRAFT and performs no
# write at all. The model is told (system prompt) that `confirmed=true` is
# only legal right after an explicit officer "ya"/"setuju". So a write needs
# BOTH: the model must pass confirmed=true AND, even if it wrongly does so,
# the draft path is the default — there is no code path that writes without
# `confirmed is True`.
#
# The write endpoints address a request by numeric `request_id`, not by
# ticket. We resolve it from the ticket via `get_case_log_notes` (which wraps
# siap_get_status_timeline) so a caller only ever needs the ticket.
# ---------------------------------------------------------------------------

# Tool names that MUST receive an explicit confirmation. Referenced by the
# chat loop for defence-in-depth logging.
_REQUIRES_CONFIRMATION = {"forward_case", "record_decision"}


async def _resolve_request_id(ticket: str) -> tuple[Optional[int], Optional[str]]:
    """Resolve a SIAP `request_id` from a ticket. Returns (request_id, error).

    On success error is None; on failure request_id is None and error holds
    an Indonesian explanation the write tool can return to the model.
    """
    log = await get_case_log_notes(ticket)
    if not log.get("found"):
        return None, log.get(
            "note", f"Tidak dapat menemukan permohonan untuk tiket {ticket}."
        )
    request_id = log.get("request_id")
    if request_id is None:
        return None, (
            f"Permohonan untuk tiket {ticket} ditemukan tetapi request_id "
            "tidak tersedia — tindakan tulis tidak bisa dijalankan."
        )
    return int(request_id), None


async def forward_case(
    ticket: str,
    note: str = "",
    confirmed: bool = False,
) -> dict:
    """
    Forward a case to the NEXT approval desk in SIAP.

    OFFICER-IN-THE-LOOP: this tool only performs the write when
    `confirmed is True`. With `confirmed` false/absent it returns a DRAFT
    (`{"executed": False, "draft": {...}}`) describing exactly what would
    happen — the copilot must show that draft and get an explicit officer
    "ya" before calling again with `confirmed=true`.

    Args:
      ticket: the 9-digit SIAP ticket of the case to forward.
      note: optional officer note (max 1000 chars) recorded into
        `license_log` so the next desk sees why the file moved.
      confirmed: MUST be true to actually forward. The system prompt makes
        this legal only directly after an explicit officer confirmation.

    Returns a structured dict — never raises:
      draft  → {"executed": False, "draft": {...}, "needs_confirmation": True}
      done   → {"executed": True, "ok": True, "result": {...}}
      failed → {"executed": True, "ok": False, "note": "..."}
    """
    ticket = (ticket or "").strip()
    note = (note or "").strip()

    if not confirmed:
        # DRAFT path — no write. This is the default and the safe path.
        return {
            "executed": False,
            "needs_confirmation": True,
            "draft": {
                "action": "forward_case",
                "ticket": ticket,
                "note": note,
                "description": (
                    f"Akan meneruskan berkas tiket {ticket} ke meja "
                    f"persetujuan berikutnya di SIAP"
                    + (f", dengan catatan: \"{note}\"." if note else ".")
                ),
            },
            "instruction_to_model": (
                "Tampilkan draf ini kepada petugas dan minta konfirmasi "
                "eksplisit ('ya'). JANGAN memanggil forward_case dengan "
                "confirmed=true sebelum petugas menjawab ya."
            ),
        }

    write_client = get_siap_write_client()
    if not write_client.is_configured():
        return {
            "executed": False,
            "ok": False,
            "note": (
                "Integrasi tulis SIAP belum dikonfigurasi (SIAP_WRITE_API_TOKEN "
                "kosong). Berkas tidak diteruskan."
            ),
        }

    request_id, error = await _resolve_request_id(ticket)
    if error is not None:
        return {"executed": True, "ok": False, "note": error}

    logger.info(
        "Copilot WRITE forward_case | ticket=%s | request_id=%s | confirmed=True",
        ticket, request_id,
    )
    result = await write_client.forward_request(request_id, note=note or None)
    return {
        "executed": True,
        "ok": bool(result.get("ok")),
        "result": result.get("data") if result.get("ok") else None,
        "note": result.get("note", ""),
    }


async def record_decision(
    ticket: str,
    decision: str,
    notes: str,
    confirmed: bool = False,
) -> dict:
    """
    Record an officer's APPROVE or REJECT decision on a case in SIAP.

    OFFICER-IN-THE-LOOP: this is the most accountable action the copilot
    can take, so the guard is the same as `forward_case` — the write only
    happens when `confirmed is True`. With `confirmed` false/absent it
    returns a DRAFT and writes NOTHING. A `rejected` decision routes the
    case back to the previous desk on the SIAP side.

    Args:
      ticket: the 9-digit SIAP ticket of the case.
      decision: "approved" (terima) or "rejected" (tolak).
      notes: the officer's reasoning — REQUIRED, max 1000 chars. This is
        written into `license_log` so the next/previous desk sees it.
      confirmed: MUST be true to actually record the decision. Legal only
        directly after an explicit officer confirmation (system prompt).

    Returns a structured dict — never raises:
      draft  → {"executed": False, "draft": {...}, "needs_confirmation": True}
      done   → {"executed": True, "ok": True, "result": {...}}
      failed → {"executed": True, "ok": False, "note": "..."}
    """
    ticket = (ticket or "").strip()
    decision_norm = (decision or "").strip().lower()
    notes = (notes or "").strip()

    decision_label = {"approved": "MENERIMA", "rejected": "MENOLAK"}.get(
        decision_norm, decision_norm.upper() or "(tidak ditentukan)"
    )

    if not confirmed:
        # DRAFT path — no write. Safe default.
        return {
            "executed": False,
            "needs_confirmation": True,
            "draft": {
                "action": "record_decision",
                "ticket": ticket,
                "decision": decision_norm,
                "notes": notes,
                "description": (
                    f"Akan mencatat keputusan {decision_label} izin untuk "
                    f"tiket {ticket} di SIAP"
                    + (f", dengan alasan: \"{notes}\"." if notes else
                       " (PERINGATAN: alasan keputusan masih kosong).")
                ),
            },
            "instruction_to_model": (
                "Tampilkan draf keputusan ini kepada petugas dan minta "
                "konfirmasi eksplisit ('ya'). Keputusan izin bersifat "
                "final dan menjadi tanggung jawab petugas — JANGAN "
                "memanggil record_decision dengan confirmed=true sebelum "
                "petugas menjawab ya."
            ),
        }

    write_client = get_siap_write_client()
    if not write_client.is_configured():
        return {
            "executed": False,
            "ok": False,
            "note": (
                "Integrasi tulis SIAP belum dikonfigurasi (SIAP_WRITE_API_TOKEN "
                "kosong). Keputusan tidak dicatat."
            ),
        }

    request_id, error = await _resolve_request_id(ticket)
    if error is not None:
        return {"executed": True, "ok": False, "note": error}

    logger.info(
        "Copilot WRITE record_decision | ticket=%s | request_id=%s | "
        "decision=%s | confirmed=True",
        ticket, request_id, decision_norm,
    )
    result = await write_client.record_decision(
        request_id, decision=decision_norm, notes=notes
    )
    return {
        "executed": True,
        "ok": bool(result.get("ok")),
        "result": result.get("data") if result.get("ok") else None,
        "note": result.get("note", ""),
    }


# ---------------------------------------------------------------------------
# Function declarations for Gemini.
#
# Gemini's `functionDeclarations` accepts the OpenAPI 3.0 subset (same family
# as gemini_vision.py's responseSchema). Only the types string / integer /
# number / boolean / array / object are allowed; no `$ref`, no `oneOf`,
# no `additionalProperties`. Spec: https://ai.google.dev/api/generate-content
# (Tool & FunctionDeclaration sections).
# ---------------------------------------------------------------------------

_FUNCTION_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "get_validation_summary",
        "description": (
            "Ambil hasil validasi BIMA untuk tiket yang sedang dianalisis: "
            "skor kelengkapan, status, dan daftar masalah yang sudah "
            "diurutkan dari yang paling parah. WAJIB dipanggil di awal sesi "
            "atau saat petugas bertanya tentang kondisi/kelayakan berkas. "
            "Tidak butuh argumen — konteksnya sudah terikat ke sesi."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_case_full",
        "description": (
            "Ambil seluruh data permohonan izin dari SIAP berdasarkan nomor "
            "tiket (9 digit, zero-padded). Gunakan ini di awal sesi untuk "
            "mendapatkan konteks faktual kasus."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "string",
                    "description": "Nomor tiket SIAP, 9 digit (mis. '000077591').",
                },
            },
            "required": ["ticket"],
        },
    },
    {
        "name": "get_doc_summary",
        "description": (
            "Ringkas isi satu dokumen pendukung yang diunggah (KTP, surat "
            "permohonan, proposal, pakta integritas, spesifikasi alat "
            "tangkap, dll). Gunakan saat petugas minta ringkasan/isi dokumen, "
            "mis. 'apa isi proposalnya', 'ringkas KTP-nya'. Argumen `doc_ref` "
            "adalah JENIS/NAMA dokumen (mis. 'KTP', 'surat permohonan', "
            "'proposal') ATAU file_id — sebutkan sesuai kata petugas; tidak "
            "perlu tahu id file. Pada alur chat, ringkasan dibuat dengan "
            "Gemini Vision atas berkas asli; bila berkas sudah tidak tersimpan, "
            "tool tetap memberi ringkasan dari catatan pemeriksaan."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_ref": {
                    "type": "string",
                    "description": (
                        "Jenis/nama dokumen yang diminta (mis. 'KTP', 'surat "
                        "permohonan', 'proposal') atau file_id."
                    ),
                },
            },
            "required": ["doc_ref"],
        },
    },
    {
        "name": "send_document",
        "description": (
            "Kirimkan berkas dokumen ASLI yang diunggah ke chat petugas, "
            "supaya petugas dapat melihat/menerima dokumennya sendiri. "
            "Gunakan saat petugas meminta melihat atau menerima dokumen "
            "tertentu, mis. 'kirim KTP-nya', 'boleh saya lihat surat "
            "pesanannya', atau 'tolong kirimkan proposalnya'. Argumen "
            "`doc_ref` adalah label/jenis dokumen (mis. 'KTP', 'Surat "
            "Pesanan', 'proposal') atau file_id. Berbeda dari get_doc_summary "
            "yang hanya MERINGKAS — tool ini benar-benar MENGIRIM berkasnya."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_ref": {
                    "type": "string",
                    "description": (
                        "Jenis/label dokumen yang diminta (mis. 'KTP', "
                        "'Surat Pesanan', 'NIB') atau file_id dokumen."
                    ),
                },
            },
            "required": ["doc_ref"],
        },
    },
    {
        "name": "compare_field",
        "description": (
            "Bandingkan satu field di antara dua dokumen yang sudah "
            "diekstrak (mis. NIK di KTP vs NIK di NIB). Mengembalikan "
            "kesamaan persis, skor kemiripan 0-1, dan catatan singkat."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_a": {
                    "type": "object",
                    "description": "Dokumen pertama (dict field → nilai).",
                },
                "doc_b": {
                    "type": "object",
                    "description": "Dokumen kedua (dict field → nilai).",
                },
                "field": {
                    "type": "string",
                    "description": (
                        "Nama field yang akan dibandingkan (mis. 'nama', "
                        "'nik', 'alamat')."
                    ),
                },
            },
            "required": ["doc_a", "doc_b", "field"],
        },
    },
    {
        "name": "cite_regulation",
        "description": (
            "Cari kutipan peraturan OSS-RBA dari ChromaDB. Mengembalikan "
            "tiga chunk paling relevan dengan judul, cuplikan, dan sumber. "
            "Wajib digunakan ketika petugas bertanya tentang persyaratan, "
            "kewajiban, atau dasar hukum."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Kata kunci pencarian dalam Bahasa Indonesia, mis. "
                        "'persyaratan KBLI 56101 risiko rendah'."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_case_log_notes",
        "description": (
            "Ambil rangkaian catatan dari meja/tahap SEBELUMNYA pada riwayat "
            "berkas (license_log SIAP). Gunakan saat petugas baru membuka "
            "kasus agar tahu apa yang dikatakan petugas sebelumnya — konteks "
            "serah-terima antar meja."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "string",
                    "description": "Nomor tiket SIAP, 9 digit (mis. '000077591').",
                },
            },
            "required": ["ticket"],
        },
    },
    {
        "name": "get_siap_signing_link",
        "description": (
            "HANYA untuk mode asisten tanda tangan. Ambil tautan halaman "
            "tanda tangan SIAP Jateng untuk berkas ini, supaya Kepala "
            "DPMPTSP dapat klik dan menandatangani izin di SIAP. BIMA TIDAK "
            "menandatangani — penandatanganan (TTE/BSRE) dilakukan di SIAP. "
            "Gunakan tool ini saat Kepala siap menandatangani atau bertanya "
            "cara/tempat tanda tangan."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "string",
                    "description": "Nomor tiket SIAP, 9 digit (mis. '000077591').",
                },
            },
            "required": ["ticket"],
        },
    },
    {
        "name": "forward_case",
        "description": (
            "TINDAKAN MENGUBAH DATA. Teruskan berkas ke meja persetujuan "
            "berikutnya di SIAP. WAJIB pola dua langkah: panggil pertama "
            "dengan confirmed=false untuk mendapatkan DRAF, tampilkan draf "
            "itu ke petugas, lalu panggil lagi dengan confirmed=true HANYA "
            "setelah petugas menjawab 'ya'/'setuju' secara eksplisit di "
            "giliran tepat sebelumnya. Permintaan awal petugas BUKAN "
            "konfirmasi."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "string",
                    "description": "Nomor tiket SIAP, 9 digit, berkas yang diteruskan.",
                },
                "note": {
                    "type": "string",
                    "description": (
                        "Catatan petugas (opsional, maks 1000 karakter) yang "
                        "dicatat ke license_log agar dibaca meja berikutnya."
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "Set true HANYA setelah petugas mengonfirmasi "
                        "eksplisit. Jika false/diabaikan, tool hanya "
                        "mengembalikan draf dan tidak mengubah data apa pun."
                    ),
                },
            },
            "required": ["ticket"],
        },
    },
    {
        "name": "record_decision",
        "description": (
            "TINDAKAN MENGUBAH DATA — paling penting. Catat keputusan "
            "petugas TERIMA (approved) atau TOLAK (rejected) atas izin di "
            "SIAP. WAJIB pola dua langkah: panggil pertama dengan "
            "confirmed=false untuk DRAF, tampilkan ke petugas, lalu panggil "
            "lagi dengan confirmed=true HANYA setelah petugas menjawab 'ya' "
            "secara eksplisit di giliran tepat sebelumnya. Keputusan izin "
            "adalah tanggung jawab petugas, bukan BIMA."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "string",
                    "description": "Nomor tiket SIAP, 9 digit.",
                },
                "decision": {
                    "type": "string",
                    "description": (
                        "Keputusan: 'approved' (terima) atau 'rejected' (tolak)."
                    ),
                },
                "notes": {
                    "type": "string",
                    "description": (
                        "Alasan/keterangan keputusan dari petugas — WAJIB "
                        "diisi, maks 1000 karakter. Jangan dikarang; ambil "
                        "dari petugas atau dari temuan validasi nyata."
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "Set true HANYA setelah petugas mengonfirmasi "
                        "eksplisit. Jika false/diabaikan, tool hanya "
                        "mengembalikan draf dan tidak mengubah data apa pun."
                    ),
                },
            },
            "required": ["ticket", "decision", "notes"],
        },
    },
]


# Map tool name → callable. Each callable is invoked with the dict of
# arguments the model produced. The chat loop handles sync vs async.
_TOOL_DISPATCH: dict[str, Any] = {
    "get_validation_summary": get_validation_summary,
    "get_case_full": get_case_full,
    "get_doc_summary": get_doc_summary,
    "send_document": send_document,
    "compare_field": compare_field,
    "cite_regulation": cite_regulation,
    "get_case_log_notes": get_case_log_notes,
    "get_siap_signing_link": get_siap_signing_link,
    "forward_case": forward_case,
    "record_decision": record_decision,
}


# ---------------------------------------------------------------------------
# Per-mode tool exposure.
#
# The function-calling agent is shared; what differs per mode is WHICH tools
# the model may call. The signature-assistant (Wave 4) is read-only over the
# case and gets the SIAP signing handoff tool — it deliberately does NOT see
# `forward_case`/`record_decision`, because the only accountable action at the
# signing stage is the signature itself, which happens in SIAP, not BIMA.
# ---------------------------------------------------------------------------

# Tools the officer copilot may call — the full set (Wave 1-2 behaviour).
_OFFICER_TOOL_NAMES = frozenset({
    "get_validation_summary",
    "get_case_full",
    "get_doc_summary",
    "send_document",
    "compare_field",
    "cite_regulation",
    "get_case_log_notes",
    "forward_case",
    "record_decision",
})

# Tools the signature-assistant may call — read-only chain synthesis plus the
# SIAP signing deep-link handoff. No write tools.
_SIGNATURE_TOOL_NAMES = frozenset({
    "get_validation_summary",
    "get_case_full",
    "cite_regulation",
    "get_case_log_notes",
    "get_siap_signing_link",
})


def _declarations_for_mode(mode: str) -> list[dict[str, Any]]:
    """Return the Gemini functionDeclarations subset allowed in `mode`."""
    allowed = (
        _SIGNATURE_TOOL_NAMES if mode == _MODE_SIGNATURE else _OFFICER_TOOL_NAMES
    )
    return [d for d in _FUNCTION_DECLARATIONS if d["name"] in allowed]


def _result_preview(value: Any, limit: int = 240) -> str:
    """Compact one-line preview of a tool result for transcripts/logs."""
    try:
        s = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(value)
    s = s.replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


async def _invoke_tool(name: str, args: dict[str, Any]) -> Any:
    """Run one tool, awaiting if async. Catches exceptions so the model
    always gets a structured result back (never an unhandled raise)."""
    fn = _TOOL_DISPATCH.get(name)
    if fn is None:
        return {"error": f"Tool {name!r} tidak dikenal."}
    try:
        result = fn(**args) if not _is_coroutine_function(fn) else await fn(**args)
        return result
    except TypeError as e:
        logger.warning("Tool %s called with bad args=%s | err=%s", name, args, e)
        return {"error": f"Argumen tidak valid untuk {name}: {e}"}
    except Exception as e:  # pragma: no cover — defensive
        logger.exception("Tool %s raised", name)
        return {"error": f"Tool {name} gagal: {e}"}


def _is_coroutine_function(fn: Any) -> bool:
    import inspect
    return inspect.iscoroutinefunction(fn)


# ---------------------------------------------------------------------------
# OfficerCopilot — the main class.
# ---------------------------------------------------------------------------


class OfficerCopilot:
    """
    Officer-facing AI partner. One instance per process is fine; the class
    holds no per-request state. The `chat` method is fully self-contained.
    """

    def __init__(
        self,
        api_key: str = _GEMINI_API_KEY,
        model: str = _GEMINI_TOOL_MODEL,
        timeout: float = _TOOL_TIMEOUT_SECONDS,
        max_rounds: int = _MAX_TOOL_ROUNDS,
    ) -> None:
        self.api_key = api_key
        self.model = model.removeprefix("models/")
        self.timeout = timeout
        self.max_rounds = max_rounds

    def _endpoint(self) -> str:
        return (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )

    def _build_initial_contents(
        self,
        message: str,
        history: list[dict],
    ) -> list[dict]:
        """
        Convert the caller's history (list of {role, text}) plus the new
        user message into Gemini's `contents` array. Gemini accepts roles
        `user` and `model`; any unknown role is coerced to `user`.
        """
        contents: list[dict] = []
        for turn in history or []:
            role = turn.get("role", "user")
            text = turn.get("text", "")
            if not text:
                continue
            if role not in ("user", "model"):
                role = "user"
            contents.append({"role": role, "parts": [{"text": text}]})
        contents.append({"role": "user", "parts": [{"text": message}]})
        return contents

    async def chat(
        self,
        message: str,
        ticket: str,
        history: list[dict],
        officer_id: int | None = None,
        validation: dict | None = None,
        mode: str = _MODE_OFFICER,
        documents: dict | None = None,
    ) -> dict:
        """
        Run one user-message turn. Returns:
          {
            "reply":       str (final assistant text),
            "tool_calls":  list of {name, args, result_preview},
            "history":     updated history including the new user turn
                           and the final model reply, ready to be passed
                           back on the next call.
          }

        Args:
          officer_id: the verified-JWT officer this session belongs to. Used
            only for masked logging — the agent holds no per-officer state;
            durability is admin-api's `copilot_session` table. NEVER trust an
            officer_id from a request body; admin-api derives it from the JWT.
          validation: the BIMA validator result for `ticket` (score, status,
            summary, issues), computed by admin-api and injected here so the
            `get_validation_summary` tool can surface it without re-running
            Gemini Vision. None when no validation is available yet.
          mode: "officer" (default) for the validation-first desk copilot, or
            "signature" for the Head-of-DPMPTSP signing assistant (Wave 4 /
            Vision req #13). The mode selects the system prompt and the
            tool subset; the agent machinery is otherwise identical. An
            unknown value falls back to "officer".
          documents: optional in-session document bytes keyed by file_id —
            {file_id: {"filename", "mime_type", "content" (bytes),
            "claimed_type"}}. Supplied by the chat bridge (officer_bridge.py)
            in the WhatsApp/Telegram demo where the citizen sent their docs
            straight into BIMA. Lets `get_doc_summary` answer with real Gemini
            Vision instead of the canned placeholder. None on the admin
            dashboard path (SIAP file-download not wired). Bytes never logged.
        """
        mode = mode if mode in _VALID_MODES else _MODE_OFFICER

        if not is_configured():
            unavailable = (
                "Maaf, Asisten Tanda Tangan belum dapat digunakan: "
                if mode == _MODE_SIGNATURE
                else "Maaf, Officer Copilot belum dapat digunakan: "
            )
            return {
                "reply": (
                    unavailable
                    + "GEMINI_API_KEY belum dikonfigurasi di server ai-engine."
                ),
                "tool_calls": [],
                "history": list(history or []),
            }

        contents = self._build_initial_contents(message, history)
        tool_calls_log: list[dict[str, Any]] = []

        prompt_template = (
            _SIGNATURE_SYSTEM_PROMPT_TEMPLATE
            if mode == _MODE_SIGNATURE
            else _SYSTEM_PROMPT_TEMPLATE
        )
        system_instruction = {
            "role": "system",
            "parts": [{"text": prompt_template.format(ticket=ticket)}],
        }

        # Bind the validation result + any in-session documents for this turn
        # so `get_validation_summary` / `get_doc_summary` can read them. The
        # token resets in `finally` keep concurrent requests isolated — each
        # asyncio task gets its own ContextVar copy. `_docs_to_send_context`
        # is a fresh per-turn list `send_document` appends resolved file_ids
        # to; we drain it into the result so the bridge can push the files.
        ctx_token = _validation_context.set(validation)
        doc_token = _doc_context.set(documents)
        send_token = _docs_to_send_context.set([])
        logger.info(
            "Copilot turn start | mode=%s | officer_id=%s | ticket=%s | "
            "has_validation=%s | in_session_docs=%d | history_turns=%d",
            mode,
            officer_id if officer_id is not None else "<none>",
            ticket,
            validation is not None,
            len(documents or {}),
            len(history or []),
        )
        try:
            result = await self._run_chat_loop(
                client_message=message,
                ticket=ticket,
                history=history,
                contents=contents,
                tool_calls_log=tool_calls_log,
                system_instruction=system_instruction,
                mode=mode,
            )
            # Surface any documents `send_document` resolved this turn so the
            # bridge can deliver them out-of-band. Always a list (possibly
            # empty). Only meaningful in officer mode (send_document is not in
            # the signature tool set), but harmless elsewhere.
            queued = _docs_to_send_context.get() or []
            result["documents_to_send"] = list(queued)
            return result
        finally:
            _validation_context.reset(ctx_token)
            _doc_context.reset(doc_token)
            _docs_to_send_context.reset(send_token)

    async def _run_chat_loop(
        self,
        *,
        client_message: str,
        ticket: str,
        history: list[dict],
        contents: list[dict],
        tool_calls_log: list[dict[str, Any]],
        system_instruction: dict,
        mode: str = _MODE_OFFICER,
    ) -> dict:
        """The Gemini function-calling round loop. Split out of `chat()` so the
        ContextVar set/reset stays a tight wrapper around the whole turn."""
        message = client_message
        # Only the tools allowed in `mode` are declared to Gemini — the
        # signature-assistant never sees the write tools.
        declarations = _declarations_for_mode(mode)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for round_idx in range(self.max_rounds):
                payload = {
                    "systemInstruction": system_instruction,
                    "contents": contents,
                    "tools": [{"functionDeclarations": declarations}],
                    "generationConfig": {
                        "temperature": 0.2,
                        "maxOutputTokens": 1024,
                    },
                }

                try:
                    resp = await client.post(self._endpoint(), json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                except httpx.TimeoutException:
                    logger.warning(
                        "Copilot Gemini timeout | round=%d | model=%s",
                        round_idx, self.model,
                    )
                    return self._error_reply(
                        "Permintaan ke Gemini melewati batas waktu.",
                        tool_calls_log, history, message,
                    )
                except httpx.HTTPStatusError as e:
                    logger.warning(
                        "Copilot Gemini HTTP %d | round=%d | body=%s",
                        e.response.status_code, round_idx,
                        e.response.text[:300],
                    )
                    return self._error_reply(
                        f"Gemini merespon HTTP {e.response.status_code}.",
                        tool_calls_log, history, message,
                    )
                except httpx.RequestError as e:
                    logger.warning("Copilot Gemini network error | err=%s", e)
                    return self._error_reply(
                        "Gangguan jaringan saat menghubungi Gemini.",
                        tool_calls_log, history, message,
                    )

                candidate = (data.get("candidates") or [{}])[0]
                content = candidate.get("content") or {}
                parts = content.get("parts") or []

                function_calls = [
                    p["functionCall"] for p in parts if "functionCall" in p
                ]
                text_chunks = [p.get("text", "") for p in parts if "text" in p]

                # Final text reply (no tool call) → done.
                if not function_calls:
                    reply_text = "".join(text_chunks).strip()
                    if not reply_text:
                        reply_text = (
                            "Maaf, saya tidak dapat menyusun jawaban untuk "
                            "pertanyaan ini."
                        )
                    new_history = list(history or [])
                    new_history.append({"role": "user", "text": message})
                    new_history.append({"role": "model", "text": reply_text})
                    return {
                        "reply": reply_text,
                        "tool_calls": tool_calls_log,
                        "history": new_history,
                    }

                # Otherwise: execute every requested tool call, append both
                # the model's functionCall and our functionResponse to
                # contents, then loop.
                contents.append({"role": "model", "parts": parts})

                response_parts: list[dict[str, Any]] = []
                allowed_tools = (
                    _SIGNATURE_TOOL_NAMES
                    if mode == _MODE_SIGNATURE
                    else _OFFICER_TOOL_NAMES
                )
                for fc in function_calls:
                    name = fc.get("name", "")
                    args = fc.get("args") or {}
                    logger.info(
                        "Copilot tool call | mode=%s | round=%d | name=%s | args=%s",
                        mode, round_idx, name, _result_preview(args, 160),
                    )
                    # Defence-in-depth: even though only mode-scoped tools are
                    # declared to Gemini, refuse to EXECUTE a tool outside the
                    # mode's allowed set. A signature-assistant turn can never
                    # run a write tool, period.
                    if name not in allowed_tools:
                        logger.warning(
                            "Copilot BLOCKED out-of-mode tool | mode=%s | name=%s",
                            mode, name,
                        )
                        blocked = {
                            "error": (
                                f"Tool {name!r} tidak tersedia dalam mode "
                                f"{mode!r}."
                            )
                        }
                        tool_calls_log.append({
                            "name": name,
                            "args": args,
                            "result_preview": _result_preview(blocked),
                        })
                        response_parts.append({
                            "functionResponse": {
                                "name": name,
                                "response": {"result": blocked},
                            },
                        })
                        continue
                    # Defence-in-depth audit line: a confirmed write is an
                    # accountable action — log it loudly, separately from the
                    # generic tool-call line, with PII-light args only.
                    if name in _REQUIRES_CONFIRMATION and args.get("confirmed") is True:
                        logger.warning(
                            "Copilot CONFIRMED WRITE | name=%s | ticket=%s | "
                            "decision=%s | note_len=%d",
                            name,
                            args.get("ticket", "<none>"),
                            args.get("decision", "-"),
                            len(str(args.get("notes") or args.get("note") or "")),
                        )
                    result = await _invoke_tool(name, args)
                    preview = _result_preview(result)
                    logger.info(
                        "Copilot tool result | name=%s | result=%s",
                        name, preview,
                    )
                    tool_calls_log.append({
                        "name": name,
                        "args": args,
                        "result_preview": preview,
                    })
                    response_parts.append({
                        "functionResponse": {
                            "name": name,
                            "response": {"result": result},
                        },
                    })

                contents.append({"role": "user", "parts": response_parts})

        # Loop exhausted without a text reply. Surface what we have so the
        # officer is not left hanging.
        logger.warning(
            "Copilot exceeded max_rounds=%d without final reply | ticket=%s",
            self.max_rounds, ticket,
        )
        return self._error_reply(
            (
                f"Saya sudah memanggil tool {self.max_rounds} kali tanpa bisa "
                "menyusun jawaban akhir. Mohon persempit pertanyaan Anda."
            ),
            tool_calls_log, history, message,
        )

    def _error_reply(
        self,
        reason: str,
        tool_calls_log: list[dict],
        history: list[dict],
        message: str,
    ) -> dict:
        new_history = list(history or [])
        new_history.append({"role": "user", "text": message})
        new_history.append({"role": "model", "text": reason})
        return {
            "reply": reason,
            "tool_calls": tool_calls_log,
            "history": new_history,
        }


# Module-level singleton for callers that want a default instance.
_default: Optional[OfficerCopilot] = None


def get_copilot() -> OfficerCopilot:
    global _default
    if _default is None:
        _default = OfficerCopilot()
    return _default
