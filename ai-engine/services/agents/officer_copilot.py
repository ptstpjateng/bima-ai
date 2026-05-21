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
      }
    On miss returns `{"available": False, "note": "..."}` so the model is told
    to ask the officer to run the validator rather than fabricating a score.
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
    }


async def get_case_full(ticket: str) -> dict:
    """
    Fetch the SIAP monitoring-berkas record for a ticket. Reuses the same
    client the WhatsApp fast-path uses, so the auth + timeout story is
    identical.

    Returns the record dict on hit, or `{"found": False, "ticket": ticket}`
    on miss / SIAP unavailable. We do not raise — the model must always
    get a structured tool result so it can phrase the answer correctly.
    """
    client = get_siap_client()
    record = await client.get_status_by_ticket(ticket)
    if not record:
        return {"found": False, "ticket": ticket}
    return {"found": True, **record}


async def get_doc_summary(file_id: str) -> str:
    """
    Return a short Indonesian summary of one supporting document.

    # TODO Phase 3: real SIAP file fetch + Gemini Vision OCR.
    Today there is no SIAP file-download endpoint, so we return a canned
    placeholder. The contract (file_id → str) stays so the front-end and
    the agent's tool schema do not need to change when the real fetch
    lands.
    """
    return (
        f"[Ringkasan dokumen file_id={file_id} belum tersedia di Phase 2]. "
        "Endpoint pengunduhan dokumen SIAP belum diaktifkan. Akan tersedia "
        "di Phase 3 ketika integrasi pengunduhan dokumen sudah siap, "
        "menggunakan Gemini Vision OCR."
    )


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
            "Ringkasan satu dokumen pendukung (KTP, NIB, NPWP, dll). "
            "Phase 2 mengembalikan placeholder; Phase 3 akan menggunakan "
            "Gemini Vision OCR atas berkas yang diambil dari SIAP."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "ID file dokumen pendukung di SIAP.",
                },
            },
            "required": ["file_id"],
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
    "compare_field": compare_field,
    "cite_regulation": cite_regulation,
    "get_case_log_notes": get_case_log_notes,
    "forward_case": forward_case,
    "record_decision": record_decision,
}


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
        """
        if not is_configured():
            return {
                "reply": (
                    "Maaf, Officer Copilot belum dapat digunakan: GEMINI_API_KEY "
                    "belum dikonfigurasi di server ai-engine."
                ),
                "tool_calls": [],
                "history": list(history or []),
            }

        contents = self._build_initial_contents(message, history)
        tool_calls_log: list[dict[str, Any]] = []

        system_instruction = {
            "role": "system",
            "parts": [{"text": _SYSTEM_PROMPT_TEMPLATE.format(ticket=ticket)}],
        }

        # Bind the validation result for this turn so `get_validation_summary`
        # can read it. The token reset in `finally` keeps concurrent requests
        # isolated — each asyncio task gets its own ContextVar copy.
        ctx_token = _validation_context.set(validation)
        logger.info(
            "Copilot turn start | officer_id=%s | ticket=%s | "
            "has_validation=%s | history_turns=%d",
            officer_id if officer_id is not None else "<none>",
            ticket,
            validation is not None,
            len(history or []),
        )
        try:
            return await self._run_chat_loop(
                client_message=message,
                ticket=ticket,
                history=history,
                contents=contents,
                tool_calls_log=tool_calls_log,
                system_instruction=system_instruction,
            )
        finally:
            _validation_context.reset(ctx_token)

    async def _run_chat_loop(
        self,
        *,
        client_message: str,
        ticket: str,
        history: list[dict],
        contents: list[dict],
        tool_calls_log: list[dict[str, Any]],
        system_instruction: dict,
    ) -> dict:
        """The Gemini function-calling round loop. Split out of `chat()` so the
        ContextVar set/reset stays a tight wrapper around the whole turn."""
        message = client_message
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for round_idx in range(self.max_rounds):
                payload = {
                    "systemInstruction": system_instruction,
                    "contents": contents,
                    "tools": [{"functionDeclarations": _FUNCTION_DECLARATIONS}],
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
                for fc in function_calls:
                    name = fc.get("name", "")
                    args = fc.get("args") or {}
                    logger.info(
                        "Copilot tool call | round=%d | name=%s | args=%s",
                        round_idx, name, _result_preview(args, 160),
                    )
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
