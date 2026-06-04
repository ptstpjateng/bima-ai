"""
Gemini Vision client — separate from Gemma (`ai_handler._call_gemma`) because
the two model families have different capabilities and we route them
deliberately per [[Decisions]] §18.

Gemma  → citizen WhatsApp chat (cheap, fast Indonesian, no tools, no vision)
Gemini → tool-bearing agents + vision tasks (KTP/NIB OCR, officer copilot)

Same Google AI Studio API key (`GEMINI_API_KEY`), different model selection.
Same v1beta REST endpoint as the Gemma client, so the auth + transport story
stays uniform.

This module is intentionally narrow — just the vision+JSON-mode call. The
agent-specific prompts and schemas live in `services/agents/*.py`.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

# Vision-capable model. Defaults to 2.5-flash (good speed/quality balance,
# multimodal, supports structured output via responseSchema). Override per
# environment if a newer model lands.
_GEMINI_VISION_MODEL: str = os.getenv(
    "GEMINI_VISION_MODEL",
    "models/gemini-2.5-flash",
)

# Tool-calling model (no vision needed) — used by officer copilot and
# similar function-calling agents. Defaults to 2.5-flash because it
# supports both vision AND function declarations; we keep it as a
# separate var so they can be flipped independently.
_GEMINI_TOOL_MODEL: str = os.getenv(
    "GEMINI_TOOL_MODEL",
    "models/gemini-2.5-flash",
)

# Hard cap so a runaway image upload can't hang ai-engine. Sane KTP/NIB scans
# are ~1-3 MB; raise if real submissions need larger.
_VISION_TIMEOUT_SECONDS: float = float(os.getenv("GEMINI_VISION_TIMEOUT_SECONDS", "30"))


def is_configured() -> bool:
    return bool(_GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# Core call
# ---------------------------------------------------------------------------

async def extract_structured(
    *,
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
    response_schema: dict[str, Any],
    model_override: Optional[str] = None,
    temperature: float = 0.1,
    max_tokens: int = 2048,
) -> Optional[dict[str, Any]]:
    """
    Send one image + a prompt to Gemini Vision, get back a structured JSON
    response conforming to `response_schema`.

    `response_schema` is an OpenAPI 3.0 schema subset that Gemini's
    `responseSchema` field accepts. Example:
      {
        "type": "object",
        "properties": {
          "nik": {"type": "string"},
          "nama": {"type": "string"}
        },
        "required": ["nik", "nama"]
      }

    Returns the parsed dict on success; None on any error (network,
    non-200, JSON parse failure, schema mismatch). The caller is
    responsible for choosing what to do on None (fall back, retry,
    surface an error to the user).
    """
    if not is_configured():
        logger.warning("Gemini Vision not configured (GEMINI_API_KEY missing)")
        return None

    model_name = (model_override or _GEMINI_VISION_MODEL).removeprefix("models/")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_name}:generateContent?key={_GEMINI_API_KEY}"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=_VISION_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        logger.warning("Gemini Vision timeout | model=%s", model_name)
        return None
    except httpx.HTTPStatusError as e:
        logger.warning(
            "Gemini Vision HTTP %d | model=%s | body=%s",
            e.response.status_code,
            model_name,
            e.response.text[:300],
        )
        return None
    except httpx.RequestError as e:
        logger.warning("Gemini Vision network error | model=%s | err=%s", model_name, e)
        return None

    try:
        candidate = data["candidates"][0]
        finish_reason = candidate.get("finishReason", "UNKNOWN")
        if finish_reason not in ("STOP", "MAX_TOKENS"):
            logger.warning("Gemini Vision unexpected finishReason=%s", finish_reason)
        # Gemini with responseMimeType=application/json puts the JSON string
        # in parts[0].text. We just need to parse it.
        raw_text = "".join(p.get("text", "") for p in candidate["content"]["parts"])
    except (KeyError, IndexError) as e:
        logger.warning("Gemini Vision malformed response | err=%s | data=%s", e, str(data)[:300])
        return None

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.warning(
            "Gemini Vision returned non-JSON despite responseMimeType | err=%s | text=%s",
            e,
            raw_text[:300],
        )
        return None

    usage = data.get("usageMetadata", {})
    logger.info(
        "Gemini Vision OK | model=%s | tokens=%d | fields=%d",
        model_name,
        usage.get("candidatesTokenCount", 0),
        len(parsed) if isinstance(parsed, dict) else 0,
    )

    return parsed


# ---------------------------------------------------------------------------
# KTP field extraction — used by the guided-submission redesign so the
# applicant's name + NIK come from the uploaded document, NOT from a Q&A.
#
# This is a thin, purpose-built wrapper over `extract_structured`. It runs on
# the same Gemini Vision model and accepts the same image/PDF bytes the
# suitability judge already reads. Returns a small normalised dict or None on
# any failure (caller then asks for the field naturally — never hard-blocks).
#
# PII: bytes are never logged here (extract_structured logs only field counts).
# The caller is responsible for masking the NIK before it reaches a screen/log.
# ---------------------------------------------------------------------------

_KTP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_ktp": {
            "type": "boolean",
            "description": "True only if this document is an Indonesian KTP (Kartu Tanda Penduduk).",
        },
        "nama": {
            "type": "string",
            "description": "The full name (Nama) printed on the KTP, exactly as written.",
        },
        "nik": {
            "type": "string",
            "description": "The 16-digit NIK number printed on the KTP. Digits only.",
        },
        "jenis_kelamin": {
            "type": "string",
            "description": "Gender as printed: 'LAKI-LAKI' or 'PEREMPUAN'. Empty if not legible.",
        },
    },
    "required": ["is_ktp"],
}

_KTP_PROMPT = (
    "This is (or claims to be) an Indonesian KTP (Kartu Tanda Penduduk). "
    "Read it carefully and extract: is_ktp (true only if it really is a KTP), "
    "the full name (Nama), the 16-digit NIK (digits only, no spaces or dots), "
    "and the gender (Jenis Kelamin: LAKI-LAKI or PEREMPUAN). If a field is not "
    "legible, return an empty string for it. Do not guess or invent digits."
)


async def extract_ktp_fields(
    *,
    image_bytes: bytes,
    mime_type: str,
) -> Optional[dict[str, Any]]:
    """Extract {name, nik, gender, is_ktp} from a KTP image/PDF.

    Returns a normalised dict:
      {"is_ktp": bool, "name": str|None, "nik": str|None, "gender": str|None}
    or None when Vision is unconfigured / the call fails / output is unusable.

    `nik` is returned ONLY when it is exactly 16 digits (KTP invariant); a
    partial/garbled read yields nik=None so the caller asks for it rather than
    storing a wrong value. NEVER raises.
    """
    if not is_configured():
        logger.info("extract_ktp_fields: Gemini Vision not configured")
        return None

    parsed = await extract_structured(
        image_bytes=image_bytes,
        mime_type=mime_type,
        prompt=_KTP_PROMPT,
        response_schema=_KTP_SCHEMA,
    )
    if parsed is None:
        return None

    import re as _re

    name = str(parsed.get("nama", "") or "").strip() or None
    raw_nik = str(parsed.get("nik", "") or "")
    nik_digits = _re.sub(r"\D", "", raw_nik)
    nik = nik_digits if len(nik_digits) == 16 else None
    gender_raw = str(parsed.get("jenis_kelamin", "") or "").strip().upper()
    gender = None
    if "LAKI" in gender_raw:
        gender = "LAKI-LAKI"
    elif "PEREMPUAN" in gender_raw or "WANITA" in gender_raw:
        gender = "PEREMPUAN"

    return {
        "is_ktp": bool(parsed.get("is_ktp", False)),
        "name": name,
        "nik": nik,
        "gender": gender,
    }
