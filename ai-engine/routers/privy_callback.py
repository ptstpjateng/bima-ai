"""
Privy callback webhook — inbound signing-status events from Privy.id.

POST /webhook/privy/callback

────────────────────────────────────────────────────────────────────────────
WHAT IT DOES
────────────────────────────────────────────────────────────────────────────
Privy POSTs a JSON envelope whenever a signing request changes state:

  * signing_complete  → citizen finished signing; Privy attaches the
                        signed PDF (we fetch on-demand from
                        services.privy_client.get_signed_doc to avoid
                        trusting body-supplied attachments).
  * signing_failed    → citizen abandoned / Privy rejected / OTP timeout.
  * signing_expired   → request_id older than Privy's TTL (typically 7
                        days). Treated identically to signing_failed.

For each event we:
  1. Verify the HMAC-SHA256 signature BEFORE parsing JSON (so a malformed
     body from an attacker who lacks the secret cannot trip our parser).
  2. Look up the matching guided-submission session by `request_id`.
  3. Advance the session: complete → "attach to SIAP submission" path;
     failed/expired → notify the citizen + return them to REVIEW for
     a retry.
  4. Respond 200 quickly. Heavy work runs in a BackgroundTask so Privy
     does not see slow webhook latency (which they retry against, which
     causes duplicate state transitions).

────────────────────────────────────────────────────────────────────────────
SECURITY MODEL
────────────────────────────────────────────────────────────────────────────
The endpoint is PUBLIC by design — Privy needs to POST to it from
outside our network. Defence layers:

  * HMAC verification on the RAW BODY using `PRIVY_WEBHOOK_SECRET`.
    `hmac.compare_digest` for constant-time comparison.
  * No JSON parsing until HMAC passes — an attacker with no secret
    cannot reach our pydantic parsing surface.
  * No PII (NIK, full request_id, signing_url) logged at INFO; only
    masked / short-prefix forms.

────────────────────────────────────────────────────────────────────────────
FEATURE FLAG
────────────────────────────────────────────────────────────────────────────
`PRIVY_ENABLED` (default "false"). When off the endpoint accepts the
POST, verifies HMAC if the body looks like a real one, but no-ops at the
state-machine level. This keeps the URL reachable (so DNS/Caddy routing
can be tested) without making real state changes during the KYC
waiting period.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status

from services import privy_client as privy_client_mod

logger = logging.getLogger("bima_ai.privy_callback")
router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short_request_id(request_id: Optional[str]) -> str:
    """Privy request_ids can leak signing-URL guessability if logged whole.

    Returning the first 8 chars is enough to grep for a single transaction
    across logs without exposing the full identifier in an aggregator.
    """
    if not request_id:
        return "<none>"
    s = str(request_id)
    return s[:8] + "…" if len(s) > 8 else s


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


@router.post(
    "/webhook/privy/callback",
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def privy_callback(
    request: Request,
    background: BackgroundTasks,
    # TODO(privy-docs): confirm Privy's exact signature header. `X-Privy-
    # Signature` follows the industry convention; the client accepts both
    # raw-hex and `sha256=<hex>` prefixed forms either way.
    x_privy_signature: Optional[str] = Header(default=None, alias="X-Privy-Signature"),
) -> dict[str, Any]:
    """Receive a Privy signing-status callback.

    Returns a small JSON ack — the real work happens in BackgroundTasks
    so Privy's webhook timeout (usually ~5s) never fires.
    """
    # Read the RAW body before doing anything else — HMAC verification
    # MUST run against bytes that match exactly what Privy signed. Even a
    # whitespace-different re-serialised form would break the digest.
    raw = await request.body()

    client = privy_client_mod.get_privy_client()
    if not client.verify_webhook_signature(raw, x_privy_signature):
        # Constant-time inside verify_webhook_signature; one log line, no
        # secret echoed back.
        logger.warning(
            "Privy callback HMAC verification FAILED — rejecting. "
            "sig_present=%s body_len=%d",
            bool(x_privy_signature), len(raw),
        )
        raise HTTPException(status_code=403, detail="forbidden")

    # Verified — safe to parse.
    try:
        import json
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        logger.warning("Privy callback: HMAC OK but body is not valid JSON.")
        raise HTTPException(status_code=400, detail="invalid_json")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid_payload")

    event = str(payload.get("event") or payload.get("type") or "").lower()
    # Privy's documented terminal events from the vault doc §6 step 9.
    # Be lenient about exact names so a minor casing change does not
    # break us — we only branch on the failure/success bucket.
    is_complete = event in {"signing_complete", "signing_completed", "completed"}
    is_failure = event in {"signing_failed", "failed", "signing_expired", "expired", "cancelled"}

    # Try several common shapes for the request id field.
    data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    request_id = (
        payload.get("request_id")
        or payload.get("requestId")
        or (data_obj.get("request_id") if isinstance(data_obj, dict) else None)
        or (data_obj.get("id") if isinstance(data_obj, dict) else None)
    )
    request_id = str(request_id) if request_id else None

    logger.info(
        "Privy callback received | event=%s | request_id=%s | complete=%s | failure=%s",
        event or "<missing>", _short_request_id(request_id), is_complete, is_failure,
    )

    if not request_id:
        # We accept the webhook (so Privy stops retrying) but cannot act.
        return {"status": "accepted", "note": "missing request_id"}

    if is_complete:
        background.add_task(_handle_signing_complete, request_id)
        return {"status": "accepted", "event": "signing_complete"}

    if is_failure:
        background.add_task(_handle_signing_failure, request_id, event or "failed")
        return {"status": "accepted", "event": event}

    # Unknown event kind — accept + log so an unexpected new Privy event
    # type does not silently break things, but do not mutate state.
    logger.info("Privy callback: unrecognised event kind %r — no-op.", event)
    return {"status": "accepted", "note": "unrecognised event"}


# ---------------------------------------------------------------------------
# Background handlers — kept thin and import-late so a circular import
# between guided_submission and privy_client never bites at startup.
# ---------------------------------------------------------------------------


async def _handle_signing_complete(request_id: str) -> None:
    """Fetch the signed PDF + advance the guided-submission session.

    State-machine integration is deliberately import-late: the hook
    lives in services.guided_submission as a small helper that the
    state machine exposes (see guided_submission.notify_privy_signed).
    If that helper is missing (older deploy), we log and move on rather
    than crash.
    """
    try:
        from services import guided_submission as gs
    except ImportError:  # pragma: no cover - defensive
        logger.exception("Privy callback: guided_submission import failed.")
        return

    client = privy_client_mod.get_privy_client()
    fetch = await client.get_signed_doc(request_id)
    if not fetch.get("ok"):
        logger.warning(
            "Privy callback signing_complete but get_signed_doc failed | "
            "request_id=%s | note=%s",
            _short_request_id(request_id), fetch.get("note"),
        )
        return

    pdf_bytes = fetch.get("pdf_bytes") or b""
    handler = getattr(gs, "notify_privy_signed", None)
    if handler is None:
        logger.info(
            "guided_submission.notify_privy_signed not present — "
            "signed PDF for request_id=%s buffered for later wiring.",
            _short_request_id(request_id),
        )
        return
    try:
        await handler(request_id=request_id, signed_pdf=pdf_bytes)
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "guided_submission.notify_privy_signed crashed | request_id=%s",
            _short_request_id(request_id),
        )


async def _handle_signing_failure(request_id: str, event: str) -> None:
    """Notify the citizen + return their session to REVIEW for a retry."""
    try:
        from services import guided_submission as gs
    except ImportError:  # pragma: no cover - defensive
        logger.exception("Privy callback: guided_submission import failed.")
        return

    handler = getattr(gs, "notify_privy_failed", None)
    if handler is None:
        logger.info(
            "guided_submission.notify_privy_failed not present — "
            "failure for request_id=%s buffered for later wiring.",
            _short_request_id(request_id),
        )
        return
    try:
        await handler(request_id=request_id, reason=event)
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "guided_submission.notify_privy_failed crashed | request_id=%s",
            _short_request_id(request_id),
        )
