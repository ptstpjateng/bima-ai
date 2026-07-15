"""
SIAP agent — the deep, tool-rich path for SIAP Jateng questions.

Per [[Decisions]] §22, SIAP Jateng (DPMPTSP Central Java's own licensing
system) is the domain BIMA goes DEEP on: a registry of tools the LLM picks
from, "like an MCP server". This agent is the consumer side of that registry.

When `ai_handler.analyze_user_intent` classifies a citizen message as
`scope == "siap"`, `generate_ai_response` routes the message HERE instead of
the OSS-RBA RAG+Gemma path. The agent:
  * exposes the five Tier-1 `services.siap_tools` functions to Gemini as
    `functionDeclarations`,
  * lets the model decide which tools to call (lookup → requirements is the
    typical chain for "apa syarat ..."),
  * runs the function-calling loop (max 5 rounds) until the model emits a
    plain-text Indonesian reply.

It is modelled byte-for-byte on `services.agents.officer_copilot` — same
Gemini function-calling contract, same `_MAX_TOOL_ROUNDS` guard, same
`GEMINI_TOOL_MODEL` env. The two differ only in: (a) the tool set, (b) the
system prompt, (c) this agent is citizen-facing (WhatsApp/web) so its prompt
is plain conversational Indonesian, not the officer-analysis register.

Why function-calling and not RAG?
  SIAP knowledge (requirements, legal basis, fee, SLA, ticket status, step
  history) is structured DB data, not a prose corpus. The model should ASK
  the database via tools, not embed-search it — that is exactly the failure
  mode §22 was written to kill (RAG a SIAP question against the OSS corpus,
  get a wrong-but-confident answer).

Failure model:
  Tools never raise (they return structured dicts). Gemini errors degrade to
  a friendly Indonesian apology. The agent always returns a string reply.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

from services.siap_tools import (
    siap_get_regulations,
    siap_get_requirements,
    siap_get_status,
    siap_get_status_timeline,
    siap_list_licenses_by_sektor,
    siap_lookup_license,
)

load_dotenv()

logger = logging.getLogger("bima_ai.siap_agent")


# ---------------------------------------------------------------------------
# Configuration — mirrors officer_copilot.py so they flip together.
# ---------------------------------------------------------------------------

_GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
_GEMINI_TOOL_MODEL: str = os.getenv("GEMINI_TOOL_MODEL", "models/gemini-2.5-flash")
_TOOL_TIMEOUT_SECONDS: float = float(os.getenv("GEMINI_TOOL_TIMEOUT_SECONDS", "30"))

# Hard ceiling on tool-call rounds inside one chat() call. Five is generous —
# the typical SIAP chain is lookup_license → get_requirements (2 rounds).
_MAX_TOOL_ROUNDS = 5


def is_configured() -> bool:
    """The agent needs a Gemini key to do function calling. The SIAP DB being
    unconfigured is handled gracefully by the tools themselves (they return a
    structured 'not configured' result the model can phrase)."""
    return bool(_GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# System prompt — citizen-facing, Indonesian. Unlike the officer copilot this
# is conversational (WhatsApp/web), not an analyst register.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "Anda adalah BIMA, asisten AI untuk DPMPTSP Provinsi Jawa Tengah. "
    "Anda sedang membantu warga dengan pertanyaan tentang SIAP Jateng — "
    "sistem perizinan milik DPMPTSP Jateng (izin sektoral, status berkas, "
    "alur persetujuan).\n\n"
    "ATURAN WAJIB:\n"
    "1. Selalu gunakan tool yang tersedia untuk mengambil data faktual dari "
    "SIAP sebelum menjawab. JANGAN PERNAH mengarang persyaratan, dasar "
    "hukum, biaya, jangka waktu, status tiket, atau nama izin.\n"
    "2. Untuk pertanyaan 'apa syarat izin X': panggil `siap_lookup_license` "
    "dulu untuk menemukan license_id yang tepat, lalu `siap_get_requirements` "
    "dengan license_id itu. Jika hasil lookup ambigu (beberapa kandidat), "
    "sebutkan kandidatnya dan minta warga memperjelas.\n"
    "3. Untuk pertanyaan status tiket, gunakan `siap_get_status`; untuk "
    "'kenapa lama / mandek di mana', gunakan `siap_get_status_timeline`.\n"
    "4. Untuk pertanyaan 'apa dasar hukum / peraturan yang mengatur X' atau "
    "permintaan rujukan hukum sebuah izin atau proses: panggil "
    "`siap_get_regulations` dengan kata kunci topik, lalu sebutkan nomor dan "
    "judul regulasi yang dikembalikan APA ADANYA. Jangan mengarang nomor atau "
    "judul peraturan; bila found=false, katakan dengan jujur.\n"
    "5. Jika tool mengembalikan found=false, sampaikan dengan jujur bahwa "
    "data tidak ditemukan — jangan menebak. Sarankan langkah lanjutan "
    "(periksa nomor tiket, hubungi DPMPTSP).\n"
    "6. Jawab dalam Bahasa Indonesia yang ramah, jelas, dan ringkas. "
    "Sajikan persyaratan sebagai daftar bernomor. Sebutkan jangka waktu "
    "penyelesaian dan biaya retribusi bila tersedia.\n"
    "7. Domain Anda adalah SIAP Jateng, BUKAN OSS RBA nasional. Namun "
    "regulasi yang dikembalikan `siap_get_regulations` adalah registri resmi "
    "SIAP dan boleh dirujuk sebagai dasar hukum."
)


# ---------------------------------------------------------------------------
# Tool dispatch — name → callable. All five are async.
# ---------------------------------------------------------------------------

_TOOL_DISPATCH: dict[str, Any] = {
    "siap_get_requirements": siap_get_requirements,
    "siap_get_status": siap_get_status,
    "siap_get_status_timeline": siap_get_status_timeline,
    "siap_lookup_license": siap_lookup_license,
    "siap_list_licenses_by_sektor": siap_list_licenses_by_sektor,
    "siap_get_regulations": siap_get_regulations,
}


# ---------------------------------------------------------------------------
# Function declarations for Gemini.
#
# Gemini's `functionDeclarations` accepts the OpenAPI 3.0 subset — types
# string / integer / number / boolean / array / object only; no $ref, no
# oneOf, no additionalProperties. Same contract officer_copilot.py uses.
# ---------------------------------------------------------------------------

_FUNCTION_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "siap_get_requirements",
        "description": (
            "Ambil persyaratan dokumen, dasar hukum, jangka waktu "
            "penyelesaian, dan biaya retribusi untuk satu izin SIAP. "
            "Berikan license_id (lebih akurat) ATAU license_name. Untuk hasil "
            "terbaik, panggil siap_lookup_license dulu untuk mendapatkan "
            "license_id yang tepat."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "license_id": {
                    "type": "integer",
                    "description": "ID izin SIAP (dari siap_lookup_license).",
                },
                "license_name": {
                    "type": "string",
                    "description": (
                        "Nama izin (pencarian substring). Dipakai bila "
                        "license_id tidak diketahui."
                    ),
                },
            },
        },
    },
    {
        "name": "siap_get_status",
        "description": (
            "Ambil status terkini satu permohonan izin SIAP berdasarkan "
            "nomor tiket (9 digit, mis. '000077591')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "string",
                    "description": "Nomor tiket SIAP, 9 digit.",
                },
            },
            "required": ["ticket"],
        },
    },
    {
        "name": "siap_get_status_timeline",
        "description": (
            "Ambil riwayat tahapan kronologis satu tiket SIAP, termasuk "
            "penanda keterlambatan (GRAY/GREEN/YELLOW/RED). Gunakan untuk "
            "pertanyaan 'kenapa lama' atau 'berkas saya mandek di mana'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "string",
                    "description": "Nomor tiket SIAP, 9 digit.",
                },
            },
            "required": ["ticket"],
        },
    },
    {
        "name": "siap_lookup_license",
        "description": (
            "Cari katalog izin SIAP berdasarkan kata kunci nama. Mengembalikan "
            "daftar kandidat {license_id, name, sektor}. Gunakan untuk "
            "mengubah frasa izin dari warga menjadi license_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Kata kunci nama izin, mis. 'pemakaian tanah'."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Jumlah maksimum hasil (default 10, maks 25).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "siap_list_licenses_by_sektor",
        "description": (
            "Daftar semua izin yang dikelola DPMPTSP di bawah satu sektor "
            "(mis. 'Kelautan dan Perikanan', 'Pariwisata'). Gunakan ketika "
            "warga bertanya 'izin apa saja di bidang X'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sektor": {
                    "type": "string",
                    "description": "Nama sektor / bidang perizinan.",
                },
            },
            "required": ["sektor"],
        },
    },
    {
        "name": "siap_get_regulations",
        "description": (
            "Cari dasar hukum / regulasi resmi (Undang-Undang, Peraturan "
            "Pemerintah, Peraturan Gubernur, Peraturan Menteri, Peraturan "
            "Daerah) yang relevan dari registri regulasi SIAP Jateng. Gunakan "
            "untuk pertanyaan 'apa dasar hukum / peraturan yang mengatur ...', "
            "atau ketika warga meminta rujukan hukum sebuah izin atau proses."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Topik / kata kunci regulasi, mis. 'penyelenggaraan "
                        "PTSP Jawa Tengah' atau 'perizinan berusaha "
                        "terintegrasi elektronik'."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Jumlah maksimum regulasi (default 5, maks 10).",
                },
            },
            "required": ["query"],
        },
    },
]


# ---------------------------------------------------------------------------
# Helpers — mirror officer_copilot.py.
# ---------------------------------------------------------------------------

def _result_preview(value: Any, limit: int = 240) -> str:
    """Compact one-line preview of a tool result for logs."""
    try:
        s = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(value)
    s = s.replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


async def _invoke_tool(name: str, args: dict[str, Any]) -> Any:
    """Run one tool, awaiting if async. Catches exceptions so the model always
    gets a structured result back (never an unhandled raise)."""
    fn = _TOOL_DISPATCH.get(name)
    if fn is None:
        return {"error": f"Tool {name!r} tidak dikenal."}
    try:
        if inspect.iscoroutinefunction(fn):
            return await fn(**args)
        return fn(**args)
    except TypeError as e:
        logger.warning("Tool %s called with bad args=%s | err=%s", name, args, e)
        return {"error": f"Argumen tidak valid untuk {name}: {e}"}
    except Exception as e:  # pragma: no cover — defensive
        logger.exception("Tool %s raised", name)
        return {"error": f"Tool {name} gagal: {e}"}


# ---------------------------------------------------------------------------
# SiapAgent — the main class.
# ---------------------------------------------------------------------------


class SiapAgent:
    """
    Citizen-facing SIAP agent. One instance per process is fine; the class
    holds no per-request state. `chat` is fully self-contained.
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
        Convert the caller's history (list of {role, text} OR ai_handler's
        {role, parts:[{text}]} shape) plus the new user message into Gemini's
        `contents` array. Roles other than user/model are coerced to user.
        """
        contents: list[dict] = []
        for turn in history or []:
            role = turn.get("role", "user")
            # ai_handler stores history as {role, parts:[{text}]}; the officer
            # copilot uses {role, text}. Accept both so this agent can be
            # called from either path.
            if "parts" in turn:
                text = "".join(p.get("text", "") for p in turn.get("parts", []))
            else:
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
        history: Optional[list[dict]] = None,
    ) -> dict:
        """
        Run one citizen-message turn against the SIAP tool layer.

        Returns:
          {
            "reply":      str — final Indonesian natural-language answer,
            "tool_calls": list of {name, args, result_preview},
          }

        On any unrecoverable Gemini error the reply is a friendly Indonesian
        apology — this method never raises.
        """
        history = history or []

        if not is_configured():
            return {
                "reply": (
                    "Maaf, layanan SIAP BIMA belum dapat digunakan saat ini "
                    "karena konfigurasi server belum lengkap. Silakan coba "
                    "lagi nanti atau hubungi DPMPTSP Jateng."
                ),
                "tool_calls": [],
            }

        contents = self._build_initial_contents(message, history)
        tool_calls_log: list[dict[str, Any]] = []

        system_instruction = {
            "role": "system",
            "parts": [{"text": _SYSTEM_PROMPT}],
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
                        "SIAP agent Gemini timeout | round=%d | model=%s",
                        round_idx, self.model,
                    )
                    return self._error_reply(
                        "Maaf, permintaan ke layanan AI melewati batas waktu. "
                        "Silakan coba lagi sebentar.",
                        tool_calls_log,
                    )
                except httpx.HTTPStatusError as e:
                    logger.warning(
                        "SIAP agent Gemini HTTP %d | round=%d | body=%s",
                        e.response.status_code, round_idx,
                        e.response.text[:300],
                    )
                    return self._error_reply(
                        "Maaf, layanan AI sedang mengalami gangguan. "
                        "Silakan coba lagi nanti.",
                        tool_calls_log,
                    )
                except httpx.RequestError as e:
                    logger.warning("SIAP agent Gemini network error | err=%s", e)
                    return self._error_reply(
                        "Maaf, terjadi gangguan jaringan. Silakan coba lagi.",
                        tool_calls_log,
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
                            "Maaf, saya belum dapat menyusun jawaban untuk "
                            "pertanyaan ini. Bisa Anda perjelas?"
                        )
                    return {"reply": reply_text, "tool_calls": tool_calls_log}

                # Otherwise: execute every requested tool call, append both the
                # model's functionCall and our functionResponse, then loop.
                contents.append({"role": "model", "parts": parts})

                response_parts: list[dict[str, Any]] = []
                for fc in function_calls:
                    name = fc.get("name", "")
                    args = fc.get("args") or {}
                    logger.info(
                        "SIAP agent tool call | round=%d | name=%s | args=%s",
                        round_idx, name, _result_preview(args, 160),
                    )
                    result = await _invoke_tool(name, args)
                    preview = _result_preview(result)
                    logger.info(
                        "SIAP agent tool result | name=%s | result=%s",
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

        # Loop exhausted without a text reply.
        logger.warning(
            "SIAP agent exceeded max_rounds=%d without final reply",
            self.max_rounds,
        )
        return self._error_reply(
            "Maaf, saya kesulitan menyusun jawaban akhir. Mohon ajukan "
            "pertanyaan Anda dengan lebih spesifik.",
            tool_calls_log,
        )

    def _error_reply(
        self,
        reason: str,
        tool_calls_log: list[dict],
    ) -> dict:
        return {"reply": reason, "tool_calls": tool_calls_log}


# Module-level singleton for callers that want a default instance.
_default: Optional[SiapAgent] = None


def get_siap_agent() -> SiapAgent:
    global _default
    if _default is None:
        _default = SiapAgent()
    return _default
