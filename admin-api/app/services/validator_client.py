"""
ai-engine validator client.

Thin async wrapper around `POST /v1/validator/submission` on ai-engine. Used
by the `/case/{ticket}/validate` officer endpoint to score a citizen's permit
submission (KTP + NIB + NPWP + extras) without exposing ai-engine on the
public internet.

Why a separate file (vs inlining into the router):
  Mirrors `siap_client.py` — keeps the router file focused on auth + shape
  translation, and lets a unit test stub the HTTP boundary by patching the
  module-level `_client` singleton.

Auth: X-Internal-Key, same shared secret already used by tracking/webhook
calls between BIMA services. Officers' JWTs are checked at the router layer;
this client only talks service-to-service.

Failure mode: returns a `(ok, payload)` tuple. ai-engine's vision pipeline
takes 8-12s on a real KTP; demo fixtures return in <100ms. We give the call
a generous 60s ceiling so a slow Gemini response doesn't get clipped, but
short enough that a stuck endpoint surfaces as 502 within a minute.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger("bima_admin_api.validator_client")

# Generous ceiling — Gemini Vision can take 8-12s per doc; a 3-doc submission
# in worst-case sequential mode can approach 40s. 60s gives headroom without
# letting a wedged upstream block officers' UI indefinitely.
_DEFAULT_TIMEOUT_SECONDS = 60.0


class ValidatorClient:
    """Async HTTP client for ai-engine's submission validator."""

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

    async def validate(
        self,
        documents: Optional[list[dict]] = None,
        demo_fixture: Optional[str] = None,
    ) -> tuple[bool, dict[str, Any]]:
        """
        Call ai-engine's `/v1/validator/submission`.

        Args:
          documents: list of `{doc_type, filename, mime_type, content_base64}`
            dicts, matching ai-engine's `DocumentInput` schema. Optional in
            demo mode — ai-engine's validator router now accepts a
            body-less call when `demo_fixture` is set (per QA fix H1+H2,
            2026-05-19). When `documents` is None and `demo_fixture` is
            None, ai-engine returns 422.
          demo_fixture: optional fixture name (`clean` | `name_mismatch` |
            `nik_typo`). Passed as `?demo_fixture=...` query param; ai-engine
            short-circuits to a pre-baked response. Used in Sprint D demos.

        Returns:
          `(ok, payload)`. `ok=True` only on HTTP 200 with parseable JSON.
          Errors are logged at WARNING and surface as `(False, {...})` so
          the router can decide between 502 vs. propagating ai-engine's body.
        """
        if not self.is_configured():
            logger.warning(
                "Validator client not configured (AI_ENGINE_URL or INTERNAL_API_KEY missing)"
            )
            return False, {"error": "validator_not_configured"}

        url = f"{self.base}/v1/validator/submission"
        params = {"demo_fixture": demo_fixture} if demo_fixture else None
        # In demo mode the body is omitted entirely — no stub documents,
        # no empty array. ai-engine accepts a body-less POST when
        # demo_fixture is set in the query string (QA fix H1+H2).
        body = {"documents": documents} if documents else None

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    url,
                    headers=self._headers(),
                    params=params,
                    json=body,
                )
        except httpx.TimeoutException:
            logger.warning("Validator timeout | demo_fixture=%s", demo_fixture)
            return False, {"error": "validator_timeout"}
        except httpx.RequestError as exc:
            logger.warning("Validator network error | err=%s", exc)
            return False, {"error": "validator_unreachable"}

        if resp.status_code != 200:
            # Pass the upstream body through so officers see ai-engine's
            # specific validation error (e.g. "doc_type 'foo' not supported").
            try:
                upstream = resp.json()
            except ValueError:
                upstream = {"detail": resp.text[:500]}
            logger.warning(
                "Validator non-200 | status=%d | body=%s",
                resp.status_code,
                upstream,
            )
            return False, {"error": "validator_http_error", "status": resp.status_code, "upstream": upstream}

        try:
            payload = resp.json()
        except ValueError:
            logger.warning("Validator returned non-JSON body")
            return False, {"error": "validator_invalid_json"}

        return True, payload


_client: Optional[ValidatorClient] = None


def get_validator_client() -> ValidatorClient:
    """Singleton accessor — instantiate once per process."""
    global _client
    if _client is None:
        _client = ValidatorClient()
    return _client
