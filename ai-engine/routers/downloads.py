"""Public download route for BIMA-generated documents (Pakta Integritas etc.).

Caddy routes `/dl/*` here so APTANA can fetch the PDF to deliver it as a
WhatsApp *document* message. Tokens are unguessable + short-lived
(services.generated_docs). No auth: the document is the citizen's own data and
the link is single-use-ish; for production, gate behind the citizen's SSO.
"""
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from services import generated_docs

logger = logging.getLogger("bima_ai.downloads")

router = APIRouter()


@router.get("/dl/{token}")
async def download(token: str):
    item = generated_docs.fetch(token)
    if item is None:
        logger.info("Download miss (expired/unknown token)")
        return JSONResponse({"error": "not_found_or_expired"}, status_code=404)
    pdf, filename = item
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
