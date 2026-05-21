"""
ai-engine Officer Copilot client.

Thin async wrapper around `POST /v1/copilot/chat` on ai-engine. Used by the
`/case/{ticket}/copilot/chat` officer endpoint to run one copilot turn
without exposing ai-engine on the public internet.

Why a separate file (vs inlining into the router):
  Mirrors `validator_client.py` / `siap_client.py` — keeps the router file
  focused on auth, session persistence, and shape translation, and lets a
  unit test stub the HTTP boundary by patching the module-level `_client`
  singleton.

Statelessness contract:
  ai-engine's copilot endpoint keeps NO conversation memory. admin-api owns
  the `copilot_session` table: it loads the prior `history`, passes it here,
  and persists the `history` ai-engine returns. This client never holds
  state — it is a pure request/response shim.

Auth: X-Internal-Key, the same shared secret used by tracking/webhook/
validator calls between BIMA services. Officers' JWTs are checked at the
router layer; this client only talks service-to-service.

Failure mode: returns a `(ok, payload)` tuple. The copilot agent runs
Gemini function-calling rounds (worst case ~30s), so we give a generous
45s ceiling — long enough for a slow multi-tool turn, short enough that a
wedged upstream surfaces as a 502 quickly.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger("bima_admin_api.copilot_client")

# Copilot can run up to 5 Gemini function-calling rounds; the agent's own
# per-call timeout is 30s. 45s gives headroom for the round loop without
# letting a stuck upstream block the officer's UI indefinitely.
_DEFAULT_TIMEOUT_SECONDS = 45.0


class CopilotClient:
    """Async HTTP client for ai-engine's Officer Copilot."""

    def __init__(
        self,
        base: Optional[str] = None,
        internal_key: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        settings = get_settings()
        self.base = (base if base is not None else settings.AI_ENGINE_URL).rstrip("/")
        self.internal_key = (
            internal_key if internal_key is not None else settings.INTERNAL_API_KEY
        )
        self.timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT_SECONDS

    def is_configured(self) -> bool:
        # INTERNAL_API_KEY has a placeholder default in config so "configured"
        # really means "non-empty base URL" + "key is not the change-me stub".
        return bool(self.base) and self.internal_key not in ("", "change-me-internal")

    def _headers(self) -> dict:
        return {
            "X-Internal-Key": self.internal_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def chat(
        self,
        *,
        ticket: str,
        officer_id: int,
        message: str,
        history: list[dict[str, Any]],
        validation: Optional[dict[str, Any]] = None,
    ) -> tuple[bool, dict[str, Any]]:
        """
        Call ai-engine's `/v1/copilot/chat` for one conversation turn.

        Args:
          ticket: SIAP ticket the conversation is anchored to.
          officer_id: the authenticated officer's `users.id`. SECURITY — this
            MUST come from the verified JWT at the router layer, never from a
            request body. ai-engine uses it for masked logging/audit only.
          message: the officer's new message.
          history: prior {role, text} turns loaded from `copilot_session`.
          validation: the BIMA validator result for the ticket (score,
            status, summary, issues), so the copilot's `get_validation_summary`
            tool can surface it. Optional — omit when no validation exists.

        Returns:
          `(ok, payload)`. `ok=True` only on HTTP 200 with parseable JSON.
          The payload then carries `reply`, `tool_calls`, and the updated
          `history` (which the router persists). Errors are logged at
          WARNING and surface as `(False, {...})`.
        """
        if not self.is_configured():
            logger.warning(
                "Copilot client not configured (AI_ENGINE_URL or "
                "INTERNAL_API_KEY missing)"
            )
            return False, {"error": "copilot_not_configured"}

        url = f"{self.base}/v1/copilot/chat"
        body: dict[str, Any] = {
            "ticket": ticket,
            "officer_id": officer_id,
            "message": message,
            "history": history,
        }
        if validation is not None:
            body["validation"] = validation

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=self._headers(), json=body)
        except httpx.TimeoutException:
            logger.warning("Copilot timeout | ticket=%s", ticket)
            return False, {"error": "copilot_timeout"}
        except httpx.RequestError as exc:
            logger.warning("Copilot network error | ticket=%s | err=%s", ticket, exc)
            return False, {"error": "copilot_unreachable"}

        if resp.status_code != 200:
            try:
                upstream = resp.json()
            except ValueError:
                upstream = {"detail": resp.text[:500]}
            logger.warning(
                "Copilot non-200 | ticket=%s | status=%d | body=%s",
                ticket,
                resp.status_code,
                upstream,
            )
            return False, {
                "error": "copilot_http_error",
                "status": resp.status_code,
                "upstream": upstream,
            }

        try:
            payload = resp.json()
        except ValueError:
            logger.warning("Copilot returned non-JSON body | ticket=%s", ticket)
            return False, {"error": "copilot_invalid_json"}

        return True, payload


_client: Optional[CopilotClient] = None


def get_copilot_client() -> CopilotClient:
    """Singleton accessor — instantiate once per process."""
    global _client
    if _client is None:
        _client = CopilotClient()
    return _client
