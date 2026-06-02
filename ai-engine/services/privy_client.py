"""
Privy.id REST client — citizen e-signing + e-meterai for BIMA Phase 5.

[[BIMA Vision]] Phase 5 / Vault: `BIMA-Vault/Phase 5+ Plan.md §6` — when a SIAP
licence requires a signed Pakta Integritas, a stamped (e-meterai) Surat
Pernyataan, or any other legally-binding document, BIMA orchestrates the
signing flow through Privy without the citizen leaving the chat.

────────────────────────────────────────────────────────────────────────────
WHY THIS LIVES OUTSIDE guided_submission.py
────────────────────────────────────────────────────────────────────────────
This module is the ONLY place in ai-engine that talks to api.privy.id. It
hides the (still-evolving) Privy REST shape behind a stable async surface —
`initiate_signing`, `apply_emeterai`, `get_signed_doc`, and
`verify_webhook_signature` — so the rest of BIMA (the guided-submission
state machine, the webhook router) is decoupled from Privy's exact wire
format and can be unit-tested with a mock client.

────────────────────────────────────────────────────────────────────────────
KYC GATE — why this PR ships behind a flag
────────────────────────────────────────────────────────────────────────────
The Privy production API key is only issued AFTER Privy's KYC review of
DPMPTSP / Diskominfo Jateng — 2 to 7 business days outside our control
(see vault doc §6 "Critical path: KYC + API access"). Until the key
lands, every method in this module short-circuits to a structured
"not configured" result. The master kill-switch is `PRIVY_ENABLED`; the
secrets are `PRIVY_API_KEY` (signing requests) and `PRIVY_WEBHOOK_SECRET`
(HMAC-verifying inbound callbacks).

A DRAFT PR is exactly the right shape for this slice: real code, real
tests, no live signing until the user flips the flag post-KYC.

────────────────────────────────────────────────────────────────────────────
API SHAPE — placeholders until docs.privy.id is reachable
────────────────────────────────────────────────────────────────────────────
At the time this client was written `docs.privy.id` was unreachable from
the sandbox; the privy.id/business landing page only mentions the
"Digital Signature API" without endpoint detail. The endpoint paths and
request/response shapes below therefore follow the Phase 5 plan +
industry-standard conventions and are tagged `TODO(privy-docs)` so they
can be fast-corrected once we get the real OpenAPI spec out of Privy
support (helpdesk@privy.id). The TESTS mock the HTTP layer so they keep
passing regardless of the exact wire shape — what they pin is the
client's stable Python surface.

────────────────────────────────────────────────────────────────────────────
FAILURE MODEL — degrade, never raise
────────────────────────────────────────────────────────────────────────────
Every public method returns a structured dict — `{"ok": bool, ...}` — and
never raises out. Timeouts, network errors, non-2xx HTTP, JSON decode
failures all become `{"ok": False, "note": "..."}` so the caller (the
guided-submission state machine) can phrase the failure for the citizen
instead of crashing the WhatsApp/Telegram reply.

────────────────────────────────────────────────────────────────────────────
PII + SECRET HYGIENE
────────────────────────────────────────────────────────────────────────────
The API key is read once at construction time and never logged. NIK is
always masked in logs via the shared `_mask` helper. PDF bytes are
deliberately not logged at any level — they may contain the citizen's
full NIK, name, address.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("bima_ai.privy")


# ---------------------------------------------------------------------------
# Configuration — every value re-read at client construction so tests/ops can
# flip env without restart.
# ---------------------------------------------------------------------------

# Privy ships two distinct hosts; flip this env var to switch.
#   Sandbox:    https://sandbox-api.privy.id   (or whatever Privy issues
#                                                during onboarding)
#   Production: https://api.privy.id
# TODO(privy-docs): confirm exact sandbox URL on Privy enterprise dashboard
# after KYC kicks off. Default to production-shaped string but rely on the
# `PRIVY_ENABLED` flag to keep this dormant.
_DEFAULT_API_BASE = "https://api.privy.id"

# Master kill-switch — until KYC clears + we flip this to "true", every
# method short-circuits without an HTTP call.
def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


# Retry budget for transient Privy 5xx. Three attempts at 0.5s, 1s, 2s is
# enough for Privy's documented "occasional Peruri queueing" hiccups
# without blowing past the citizen's WhatsApp/Telegram patience.
_RETRY_HTTP_STATUS = {500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.5


def _mask_nik(nik: Optional[str]) -> str:
    """Mask a NIK for logs — never log the full 16-digit value.

    Keeps the first 2 and last 2 digits so an operator can cross-check
    against a record without re-identifying the citizen from the log.
    """
    if not nik:
        return "<none>"
    s = str(nik)
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class PrivyClient:
    """Async wrapper around the Privy REST API.

    One instance per process is fine — it holds no per-request state. All
    public methods return `{"ok": bool, ...}` dicts and never raise.
    """

    def __init__(
        self,
        base: Optional[str] = None,
        api_key: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        timeout: Optional[float] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self.base = (base or os.getenv("PRIVY_API_BASE", _DEFAULT_API_BASE)).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("PRIVY_API_KEY", "")
        self.webhook_secret = (
            webhook_secret
            if webhook_secret is not None
            else os.getenv("PRIVY_WEBHOOK_SECRET", "")
        )
        self.timeout = float(
            timeout
            if timeout is not None
            else os.getenv("PRIVY_TIMEOUT_SECONDS", "30")
        )
        self.enabled = (
            enabled if enabled is not None else _flag("PRIVY_ENABLED", "false")
        )

    # ----- public introspection -------------------------------------------

    def is_configured(self) -> bool:
        """True only when the master flag is on AND the secrets are present.

        We deliberately AND the flag in: even if an operator copies the
        API key into the env early, the flag stays off until the team
        explicitly green-lights live signing on the VPS.
        """
        return bool(self.enabled and self.base and self.api_key)

    # ----- internals ------------------------------------------------------

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        """Build auth headers. The API key never appears in logs."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _not_configured(action: str) -> dict[str, Any]:
        return {
            "ok": False,
            "configured": False,
            "note": (
                f"Integrasi Privy belum aktif pada lingkungan ini "
                f"(PRIVY_ENABLED / PRIVY_API_KEY belum diisi). Aksi "
                f"'{action}' tidak dijalankan."
            ),
        }

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: Optional[dict[str, Any]] = None,
        content: Optional[bytes] = None,
    ) -> dict[str, Any]:
        """Single retry-wrapped HTTP call.

        Retries on transient 5xx with exponential backoff. Returns a
        structured dict — never raises. The caller decides how to map
        `ok=False` results to citizen-facing messages.
        """
        last_status: Optional[int] = None
        last_body: str = ""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.request(
                        method, url,
                        headers=headers,
                        json=json_body,
                        content=content,
                    )
            except httpx.TimeoutException:
                logger.warning(
                    "Privy %s timeout | url=%s | attempt=%d",
                    method, url, attempt,
                )
                if attempt == _MAX_ATTEMPTS:
                    return {
                        "ok": False,
                        "configured": True,
                        "note": (
                            "Privy tidak merespon tepat waktu. Permohonan "
                            "tanda tangan tidak dilanjutkan."
                        ),
                    }
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                continue
            except httpx.RequestError as exc:
                logger.warning(
                    "Privy %s network error | url=%s | err=%s",
                    method, url, exc,
                )
                if attempt == _MAX_ATTEMPTS:
                    return {
                        "ok": False,
                        "configured": True,
                        "note": (
                            "Gangguan jaringan saat menghubungi Privy. "
                            "Mohon coba lagi sebentar lagi."
                        ),
                    }
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                continue

            last_status = resp.status_code
            try:
                payload = resp.json()
            except ValueError:
                payload = {}

            if 200 <= resp.status_code < 300:
                return {
                    "ok": True,
                    "configured": True,
                    "http_status": resp.status_code,
                    "data": payload,
                }

            # Retry only on transient 5xx, not on 4xx (those are our bugs
            # or bad data — retrying won't help).
            if resp.status_code in _RETRY_HTTP_STATUS and attempt < _MAX_ATTEMPTS:
                logger.info(
                    "Privy %s transient http=%d | attempt=%d — retrying",
                    method, resp.status_code, attempt,
                )
                last_body = resp.text[:300]
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                continue

            siap_message = ""
            if isinstance(payload, dict):
                siap_message = str(
                    payload.get("message") or payload.get("error") or ""
                ).strip()
            logger.warning(
                "Privy %s non-2xx | http=%d | body=%s",
                method, resp.status_code, resp.text[:300],
            )
            return {
                "ok": False,
                "configured": True,
                "http_status": resp.status_code,
                "note": (
                    f"Privy merespon dengan HTTP {resp.status_code}."
                    + (f" Pesan: {siap_message}" if siap_message else "")
                ),
            }

        # Should be unreachable — every branch above returns. Defensive.
        return {
            "ok": False,
            "configured": True,
            "note": (
                f"Privy gagal merespon setelah {_MAX_ATTEMPTS} percobaan "
                f"(status terakhir={last_status})."
            ),
            "last_body": last_body,
        }

    # ----- public API -----------------------------------------------------

    async def initiate_signing(
        self,
        pdf_bytes: bytes,
        signer_nik: str,
        signer_name: str,
        document_title: str,
    ) -> dict[str, Any]:
        """Upload a PDF + open a signing request scoped to one citizen.

        Args:
          pdf_bytes:      The unsigned PDF (bytes). Generated by the caller
                          from a Jinja2 template + the citizen's collected
                          fields. Never logged.
          signer_nik:     16-digit NIK; Privy uses it to look up the
                          citizen's PSrE certificate. Masked in logs.
          signer_name:    Citizen's full name as it appears on KTP.
          document_title: Short title shown to the citizen on Privy's
                          signing page (e.g. "Pakta Integritas — Izin X").

        Returns:
          done   → {"ok": True, "request_id": str, "signing_url": str, ...}
          failed → {"ok": False, "note": str, ...}

        The caller (guided-submission state machine) surfaces `signing_url`
        to the citizen as a tap-to-sign deep link and persists `request_id`
        on the session so the inbound webhook can match it later.

        TODO(privy-docs): the actual request shape uses base64-encoded PDF
        inside JSON for parity with Privy's existing "Sign" Postman
        collection from 2024. If the new OpenAPI spec uses multipart
        instead, swap to httpx files= here — the public method signature
        does not change.
        """
        if not self.is_configured():
            return self._not_configured("initiate_signing")

        if not pdf_bytes:
            return {
                "ok": False,
                "configured": True,
                "note": "PDF dokumen kosong — tidak dapat memulai tanda tangan.",
            }

        b64_pdf = base64.b64encode(pdf_bytes).decode("ascii")
        body = {
            "document": {
                "title": document_title,
                # base64-encoded so we can stay JSON-only (no multipart)
                # until Privy's spec confirms otherwise.
                "content_b64": b64_pdf,
                "mime_type": "application/pdf",
            },
            "signer": {
                "nik": signer_nik,
                "name": signer_name,
            },
            "options": {
                # Citizen-friendly signing page (Privy default in their
                # business product). Toggle via env once we know the
                # parameter name post-KYC.
                "redirect_url": os.getenv("PRIVY_REDIRECT_URL", ""),
            },
        }
        url = f"{self.base}/api/v1/sign"  # TODO(privy-docs): confirm exact path
        logger.info(
            "Privy initiate_signing | title=%s | nik=%s | size=%dB",
            document_title, _mask_nik(signer_nik), len(pdf_bytes),
        )

        result = await self._request(
            "POST", url, headers=self._headers({"Content-Type": "application/json"}),
            json_body=body,
        )
        if not result.get("ok"):
            return result

        data = result.get("data") or {}
        # Privy may nest the record under `data` or flat — be forgiving.
        envelope = data.get("data", data) if isinstance(data, dict) else {}
        if not isinstance(envelope, dict):
            envelope = {}
        request_id = envelope.get("request_id") or envelope.get("id") or envelope.get("requestId")
        signing_url = envelope.get("signing_url") or envelope.get("url") or envelope.get("signUrl")
        if not request_id or not signing_url:
            logger.warning(
                "Privy initiate_signing response missing fields | keys=%s",
                sorted(envelope.keys()),
            )
            return {
                "ok": False,
                "configured": True,
                "note": (
                    "Privy menerima permintaan tapi tidak mengembalikan "
                    "request_id / signing_url. Mohon hubungi admin."
                ),
            }
        return {
            "ok": True,
            "configured": True,
            "request_id": str(request_id),
            "signing_url": str(signing_url),
            "data": envelope,
        }

    async def apply_emeterai(
        self,
        pdf_bytes: bytes,
        owner_nik: str,
    ) -> dict[str, Any]:
        """Apply a Peruri e-meterai (Rp 10,000 digital stamp) to a PDF.

        Returns the stamped PDF bytes. Per UU 10/2020 the stamp is
        legally equivalent to the physical meterai for the document being
        signed alongside.

        Args:
          pdf_bytes: The PDF to be stamped (typically a Surat Pernyataan).
                     For a sign+stamp document the canonical order is
                     STAMP FIRST → SIGN, so the signature covers the stamp.
          owner_nik: 16-digit NIK of the document owner (the citizen).
                     Privy ties the stamp to this identity for audit.

        Returns:
          done   → {"ok": True, "stamped_pdf": bytes, "stamp_id": str, ...}
          failed → {"ok": False, "note": str, ...}

        TODO(privy-docs): Privy's "e-Meterai" endpoint is publicly
        documented as a separate billing line from "Sign". Confirm the
        exact path + whether it accepts base64 inside JSON or multipart.
        """
        if not self.is_configured():
            return self._not_configured("apply_emeterai")

        if not pdf_bytes:
            return {
                "ok": False,
                "configured": True,
                "note": "PDF kosong — tidak dapat menerapkan e-meterai.",
            }

        b64_pdf = base64.b64encode(pdf_bytes).decode("ascii")
        body = {
            "document": {
                "content_b64": b64_pdf,
                "mime_type": "application/pdf",
            },
            "owner": {"nik": owner_nik},
        }
        url = f"{self.base}/api/v1/emeterai"  # TODO(privy-docs): confirm path
        logger.info(
            "Privy apply_emeterai | nik=%s | size=%dB",
            _mask_nik(owner_nik), len(pdf_bytes),
        )

        result = await self._request(
            "POST", url, headers=self._headers({"Content-Type": "application/json"}),
            json_body=body,
        )
        if not result.get("ok"):
            return result

        data = result.get("data") or {}
        envelope = data.get("data", data) if isinstance(data, dict) else {}
        if not isinstance(envelope, dict):
            envelope = {}
        stamped_b64 = (
            envelope.get("stamped_b64")
            or envelope.get("content_b64")
            or envelope.get("document_b64")
        )
        stamp_id = envelope.get("stamp_id") or envelope.get("id")
        if not stamped_b64:
            logger.warning(
                "Privy apply_emeterai response missing stamped PDF | keys=%s",
                sorted(envelope.keys()),
            )
            return {
                "ok": False,
                "configured": True,
                "note": (
                    "Privy menerima permintaan e-meterai tapi tidak "
                    "mengembalikan dokumen bermeterai."
                ),
            }
        try:
            stamped_pdf = base64.b64decode(stamped_b64)
        except (ValueError, TypeError) as exc:
            logger.warning("Privy emeterai b64 decode failed | err=%s", exc)
            return {
                "ok": False,
                "configured": True,
                "note": "Format dokumen bermeterai dari Privy tidak valid.",
            }
        return {
            "ok": True,
            "configured": True,
            "stamped_pdf": stamped_pdf,
            "stamp_id": str(stamp_id) if stamp_id else None,
            "data": envelope,
        }

    async def get_signed_doc(self, request_id: str) -> dict[str, Any]:
        """Fetch the signed PDF for a completed Privy signing request.

        Called from the webhook handler on `signing_complete` so the
        signed bundle can be attached to the eventual SIAP submission.
        Also exposed as a polling fallback for "sudah belum?" citizen
        messages — see guided_submission.AWAITING_PRIVY_SIGN.

        Returns:
          done    → {"ok": True, "pdf_bytes": bytes, "status": str, ...}
          pending → {"ok": False, "pending": True, "status": str, "note": "..."}
          failed  → {"ok": False, "note": str, ...}
        """
        if not self.is_configured():
            return self._not_configured("get_signed_doc")
        if not request_id:
            return {
                "ok": False,
                "configured": True,
                "note": "request_id Privy kosong.",
            }

        url = f"{self.base}/api/v1/sign/{request_id}"  # TODO(privy-docs)
        logger.info("Privy get_signed_doc | request_id=%s", request_id)

        result = await self._request("GET", url, headers=self._headers())
        if not result.get("ok"):
            return result

        data = result.get("data") or {}
        envelope = data.get("data", data) if isinstance(data, dict) else {}
        if not isinstance(envelope, dict):
            envelope = {}
        status = str(envelope.get("status", "")).lower()
        # Privy's documented status vocabulary (Phase 5 plan §6 step 9):
        #   signed / completed → success
        #   pending / awaiting_signer → not done yet
        #   failed / expired / cancelled → terminal failure
        if status in {"signed", "completed", "done"}:
            signed_b64 = (
                envelope.get("signed_b64")
                or envelope.get("content_b64")
                or envelope.get("document_b64")
            )
            if not signed_b64:
                return {
                    "ok": False,
                    "configured": True,
                    "status": status,
                    "note": "Privy melaporkan dokumen sudah ditandatangani "
                            "tetapi tidak mengembalikan isinya.",
                }
            try:
                pdf_bytes = base64.b64decode(signed_b64)
            except (ValueError, TypeError):
                return {
                    "ok": False,
                    "configured": True,
                    "status": status,
                    "note": "Format dokumen tertandatangani dari Privy tidak valid.",
                }
            return {
                "ok": True,
                "configured": True,
                "status": status,
                "pdf_bytes": pdf_bytes,
                "data": envelope,
            }
        if status in {"pending", "awaiting_signer", "in_progress", "sent"}:
            return {
                "ok": False,
                "configured": True,
                "pending": True,
                "status": status,
                "note": "Tanda tangan citizen belum selesai diproses.",
            }
        # failed / expired / cancelled / unknown
        return {
            "ok": False,
            "configured": True,
            "status": status or "unknown",
            "note": f"Status Privy: {status or 'tidak diketahui'}.",
        }

    # ----- webhook verification ------------------------------------------

    def verify_webhook_signature(
        self,
        body: bytes,
        provided_signature: Optional[str],
    ) -> bool:
        """Constant-time HMAC-SHA256 verification of a Privy callback.

        Privy signs every webhook body with `PRIVY_WEBHOOK_SECRET` and
        ships the signature in a header (TODO(privy-docs): confirm exact
        header name — likely `X-Privy-Signature` based on industry
        convention). The router MUST verify before parsing JSON: a
        malformed-but-valid attacker payload would otherwise still hit
        our state machine.

        Implementation notes:
          * `hmac.compare_digest` to defeat timing oracles.
          * Accepts both raw hex digest and `sha256=<hex>` prefix forms
            (the second is what GitHub/Stripe/Slack-style senders use,
            and Privy may follow suit — being lenient costs nothing).
          * Refuses to verify if no webhook secret is configured: an
            empty secret would otherwise authenticate every request.
        """
        if not self.webhook_secret:
            logger.warning(
                "Privy webhook signature check requested but "
                "PRIVY_WEBHOOK_SECRET is unset — refusing."
            )
            return False
        if not provided_signature:
            return False

        # Strip optional `sha256=` prefix, lowercase for case-insensitive
        # comparison of hex digits.
        sig = provided_signature.strip()
        if sig.lower().startswith("sha256="):
            sig = sig.split("=", 1)[1]
        sig = sig.lower()

        expected = hmac.new(
            self.webhook_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest().lower()

        return hmac.compare_digest(expected, sig)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_client: Optional[PrivyClient] = None


def get_privy_client() -> PrivyClient:
    """Return the process-wide PrivyClient (constructed lazily).

    Tests build their own `PrivyClient(...)` instances directly — this
    helper exists for the routers + state machine to share one client
    and avoid re-reading env on every call.
    """
    global _client
    if _client is None:
        _client = PrivyClient()
    return _client


def reset_privy_client_for_tests() -> None:
    """Drop the singleton so the next get_privy_client() re-reads env.

    Tests that mutate env vars use this to avoid cross-test bleed. NOT
    part of the public API — name is the warning label.
    """
    global _client
    _client = None
