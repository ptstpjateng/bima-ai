"""APTANA WhatsApp BSP outbound TEMPLATE sender.

Companion to whatsapp_sender.send_text(). Sends a pre-approved Meta
template message — the only outbound path that works OUTSIDE the
24-hour session window. Required for proactive notifications:
new-case ping, SLA warn, progress, completed, needs-fix.

Why a separate module instead of extending whatsapp_sender:
  - Templates have a fundamentally different payload shape (no `text`
    body; `template` object with components + parameters).
  - Template sends fail differently (132xxx Meta error codes for
    schema/approval issues, not transient network errors).
  - Keeping the two callers separate makes it obvious from the call
    site which message kind is in play.

APTANA contract (inferred from their Postman v1/messages doc; revisit
if their official template endpoint differs):
    POST {APTANA_API_BASE}/api/v1/messages
    Headers: Api-Token, Content-Type, Accept
    Body: {
        channel: "wa",
        sender, recipient,
        type: "template",
        template: {
            name: "<approved_template_name>",
            language: {code: "id"},
            components: [
                {type: "body", parameters: [{type: "text", text: "..."}]}
            ]
        }
    }
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Sequence

import httpx

from services.whatsapp_sender import normalize_phone

logger = logging.getLogger(__name__)

_API_BASE = os.getenv("APTANA_API_BASE", "https://api-multichannel.aptana.co.id").rstrip("/")
_API_TOKEN = os.getenv("APTANA_API_TOKEN", "")
_SENDER_NUMBER = os.getenv("APTANA_SENDER_NUMBER", "")

_TIMEOUT_SECONDS = 30.0
_MAX_ATTEMPTS = 3
_RETRY_STATUS = {429, 500, 502, 503, 504}


async def send_template(
    *,
    recipient_phone: str,
    template_name: str,
    body_params: Sequence[str],
    language_code: str = "id",
) -> bool:
    """Send an approved WhatsApp template message via APTANA.

    Returns True on 2xx, False on permanent failure (after retries).
    Never raises — callers can fire-and-forget.

    `body_params` are positional substitutions for the template's {{1}},
    {{2}}, ... placeholders in declaration order. Truncated/padded
    silently by Meta is unsafe, so we DO NOT pad here — callers must
    supply the exact arity the template was approved with.
    """
    if not _API_TOKEN or not _SENDER_NUMBER:
        logger.error(
            "Cannot send WhatsApp template: APTANA_API_TOKEN or "
            "APTANA_SENDER_NUMBER unset."
        )
        return False

    url = f"{_API_BASE}/api/v1/messages"
    headers = {
        "Api-Token": _API_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    parameters = [{"type": "text", "text": str(p)} for p in body_params]
    components = (
        [{"type": "body", "parameters": parameters}] if parameters else []
    )
    payload = {
        "channel": "wa",
        "sender": _SENDER_NUMBER,
        "recipient": normalize_phone(recipient_phone),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": components,
        },
    }

    masked_to = payload["recipient"][:4] + "…" + payload["recipient"][-4:]

    for attempt in range(_MAX_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=payload, headers=headers)

            if resp.status_code < 300:
                logger.info(
                    "APTANA template ok | to=%s tpl=%s status=%d attempt=%d",
                    masked_to,
                    template_name,
                    resp.status_code,
                    attempt + 1,
                )
                return True

            if resp.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS - 1:
                wait = 2 ** attempt
                logger.warning(
                    "APTANA template transient | to=%s tpl=%s status=%d "
                    "attempt=%d retry_in=%ds",
                    masked_to,
                    template_name,
                    resp.status_code,
                    attempt + 1,
                    wait,
                )
                await asyncio.sleep(wait)
                continue

            # Non-retriable Meta-side failure (e.g. 400 with code 132xxx
            # for template-not-approved). Log full body so we can read
            # Meta's error code straight out of the log.
            logger.error(
                "APTANA template failed | to=%s tpl=%s status=%d body=%s",
                masked_to,
                template_name,
                resp.status_code,
                resp.text[:500],
            )
            return False

        except (httpx.RequestError, asyncio.TimeoutError) as exc:
            if attempt < _MAX_ATTEMPTS - 1:
                wait = 2 ** attempt
                logger.warning(
                    "APTANA template error | to=%s tpl=%s err=%s "
                    "attempt=%d retry_in=%ds",
                    masked_to,
                    template_name,
                    exc.__class__.__name__,
                    attempt + 1,
                    wait,
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "APTANA template exhausted retries | to=%s tpl=%s err=%s",
                    masked_to,
                    template_name,
                    exc,
                )

    return False
