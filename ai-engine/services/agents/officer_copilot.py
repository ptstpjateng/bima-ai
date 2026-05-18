"""
Officer Copilot agent — the officer-facing AI partner per [[BIMA Vision]] req #8
and [[Decisions]] §18.

Lives in the bima-admin case page side panel. When an officer opens a permit
submission, this agent can:
  * pull the live case record from SIAP,
  * compare two extracted fields (e.g. NIK on KTP vs NIK on NIB),
  * cite the relevant OSS-RBA regulation chunk for a KBLI/scenario,
  * (Phase 3) summarise an individual document attached to the submission.

It is a Gemini function-calling agent. The model decides which tools to call;
this module wires the tool implementations to Gemini's `functionDeclarations`
contract and runs the call loop (max 5 rounds) until the model produces a
plain-text reply.

Design notes:
  * The Indonesian system prompt forbids hallucinating case details. The
    officer is the human in the loop — they must be able to trust every
    fact in the reply. If a tool returns nothing, the model is instructed
    to say so explicitly rather than fabricate.
  * Tools are pure Python. We do not let the model run arbitrary code; the
    only side effect surface is the four declared tools.
  * Reuses `services.siap_client.get_siap_client()` so the SIAP auth and
    timeout story stays uniform across BIMA's agents.
  * Reuses `services.agents.validator._normalize_name` so name comparison
    in the Copilot matches the validator's behaviour byte-for-byte. If a
    field starts to need its own normaliser, lift it to a shared helpers
    module rather than duplicating.
  * Reuses `services.rag_service.query_regulations` so officer citations
    come from the same ChromaDB collection the citizen chatbot reads.

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
    "Anda adalah BIMA, asisten AI yang membantu petugas DPMPTSP Jateng "
    "menganalisis berkas permohonan izin. Selalu gunakan tool yang "
    "tersedia untuk mendapatkan data faktual sebelum menjawab. Jangan "
    "pernah mengarang detail kasus, nama pemohon, atau referensi "
    "peraturan — selalu kutip dari tool result. Jawab dalam Bahasa "
    "Indonesia formal.\n\n"
    "Konteks sesi:\n"
    "- Tiket yang sedang dianalisis: {ticket}\n"
    "- Gunakan `get_case_full` di awal jika Anda belum memiliki data "
    "kasusnya, lalu gunakan tool lain sesuai pertanyaan petugas."
)


# ---------------------------------------------------------------------------
# Tool implementations.
# ---------------------------------------------------------------------------

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
]


# Map tool name → callable. Each callable is invoked with the dict of
# arguments the model produced. The chat loop handles sync vs async.
_TOOL_DISPATCH: dict[str, Any] = {
    "get_case_full": get_case_full,
    "get_doc_summary": get_doc_summary,
    "compare_field": compare_field,
    "cite_regulation": cite_regulation,
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
