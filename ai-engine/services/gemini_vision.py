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
