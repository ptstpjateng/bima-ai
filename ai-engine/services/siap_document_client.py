"""
Document client for SIAP Jateng — the BIMA↔SIAP document seam (Wave 3.5).

`siap_submission_client.py` files a brand-new licence-request (the citizen's
application record). This module is its companion: the only place in ai-engine
that calls SIAP's per-request DOCUMENT endpoints, used two ways —

  1. Guided-submission upload-on-submit — after `create_request` allocates a
     SIAP request_id, BIMA pushes the citizen's collected requirement files up
     to SIAP so the reviewing officer sees the actual attachments, not just the
     application shell.
  2. Officer copilot doc-loader — for a BIMA-created request the applicant-
     upload docs live behind this API (not in `profile_requirements`), so the
     copilot reads them here to review the full packet at any desk.

Endpoints (all shipped on Beta-SIAP):

  POST /api/v1/license-request/{id}/documents
    Attaches one file to a request. multipart/form-data, field name `file`.
    Returns: 201 {"status":"success","data":{"file_id": int, "request_id": int,
      "file_name": str, "file_type": str, "internal_filename": str}} — we read
      the new file's id from `data.file_id`.
    Ability required on the token: `document:upload`

  GET  /api/v1/license-request/{id}/documents
    Lists the request's documents (only APPLICANT-UPLOAD rows for this request).
    Returns: 200 {"status":"success","data":{"request_id": int, "count": int,
      "documents":[{"file_id": int, "file_name": str, "file_type": str,
      "created_on": ts}, ...]}}. NOTE SIAP's real keys are file_name/file_type/
      created_on — `_normalise_document` maps them to filename/mime/created_at.
    Ability required on the token: `document:read`

  GET  /api/v1/documents/{file_id}/download
    Streams the raw bytes of one file (APPLICANT-UPLOAD + ACTIVE only).
    Returns: 200 <raw bytes>
    Ability required on the token: `document:read`

Auth model:
  A SEPARATE Sanctum bearer from every other SIAP token BIMA holds:
    - SIAP_API_TOKEN          — read-only monitoring-berkas
    - SIAP_WRITE_API_TOKEN    — officer writes (workflow:advance + decision:draft)
    - SIAP_SUBMISSION_API_TOKEN — citizen submission (submission:create)
    - SIAP_EVENTS_API_TOKEN   — events:read changes feed
  This one — SIAP_DOCUMENT_API_TOKEN — must carry BOTH the `document:upload`
  and `document:read` abilities and nothing else. Keeping it distinct is the
  whole point of SIAP's scoped-ability design: a leaked document token can read
  and attach files but can never forward, decide, or create a request.

  Mint it on Beta-SIAP with:
    php artisan bima:issue-token --user=bima@dpmptsp.go.id \
        --abilities=document:upload,document:read --name=bima-documents

Network model:
  Reuses `SIAP_API_BASE` (same SIAP host as every other SIAP client).
    Local dev:  http://127.0.0.1:8500
    Production: Beta-SIAP during Wave 3.5, never production SIAP autonomously.

Failure model — degrade, never raise:
  * If `SIAP_DOCUMENT_API_TOKEN` (or `SIAP_API_BASE`) is blank the client is
    "not configured": upload/list short-circuit to a structured
    `{"ok": False, "configured": False, ...}` result with a clear note, and
    `download_document` returns None. The upload-on-submit path is strictly
    best-effort and the officer doc-loader falls back to its existing source,
    so both degrade to the current behaviour instead of crashing.
  * A timeout, network error, or non-2xx from SIAP returns a structured
    `{"ok": False, ...}` dict (upload/list) or None (download). Callers always
    get a stable shape. We never raise out of a public method.

This module performs NO validation or state-machine logic of its own — that
lives one layer up in `guided_submission.py` / `officer_bridge.py`. The token
is redacted in every log line; the raw bytes and filenames are never logged.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("bima_ai.siap_document")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# SIAP host — shared with every other SIAP client (siap_client.py etc).
_SIAP_API_BASE: str = os.getenv("SIAP_API_BASE", "").rstrip("/")

# DISTINCT from SIAP_API_TOKEN / SIAP_WRITE_API_TOKEN / SIAP_SUBMISSION_API_TOKEN
# / SIAP_EVENTS_API_TOKEN: this token carries BOTH the `document:upload` and
# `document:read` abilities. Blank → client disabled.
_SIAP_DOCUMENT_API_TOKEN: str = os.getenv("SIAP_DOCUMENT_API_TOKEN", "")

# Uploads stream a file body through SIAP to storage; downloads pull one back.
# Give them the same headroom as the other write-side clients.
_SIAP_DOCUMENT_TIMEOUT_SECONDS: float = float(
    os.getenv("SIAP_DOCUMENT_TIMEOUT_SECONDS", "15")
)

# Per-document byte ceiling on the DOWNLOAD path. These bytes flow into the
# officer's in-memory OfficerCaseSession and the durable Redis blob, so an
# unbounded download would let a hostile/oversize SIAP doc bloat both. Mirrors
# siap_templates' 8 MB template cap (_MAX_TEMPLATE_BYTES); env-overridable so
# ops can tune it without a redeploy. An oversize body is treated as just
# another failure — download_document returns None and the officer loader
# skips it (it never shows a doc BIMA couldn't fully read).
_SIAP_DOCUMENT_MAX_BYTES: int = int(
    os.getenv("SIAP_DOCUMENT_MAX_BYTES", str(8 * 1024 * 1024))
)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SiapDocumentClient:
    """Async wrapper around SIAP's per-request document endpoints.

    One instance per process is fine — it holds no per-request state.
    """

    def __init__(
        self,
        base: str = _SIAP_API_BASE,
        token: str = _SIAP_DOCUMENT_API_TOKEN,
        timeout: float = _SIAP_DOCUMENT_TIMEOUT_SECONDS,
    ) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def is_configured(self) -> bool:
        """True only when both the SIAP host and the document token are set.

        When False, upload/list short-circuit to a structured "not configured"
        result and `download_document` returns None — every caller degrades to
        its existing behaviour instead of crashing.

        A plain method (not a property) for parity with every other SIAP client
        (siap_write_client / siap_submission_client / siap_client) — callers
        invoke it WITH parens.
        """
        return bool(self.base and self.token)

    def _headers(self, *, accept_json: bool = True) -> dict[str, str]:
        # Content-Type is intentionally omitted for multipart uploads — httpx
        # sets it (with the boundary) from the `files=` argument.
        headers = {"Authorization": f"Bearer {self.token}"}
        if accept_json:
            headers["Accept"] = "application/json"
        return headers

    @staticmethod
    def _not_configured() -> dict[str, Any]:
        return {
            "ok": False,
            "configured": False,
            "note": (
                "Integrasi dokumen SIAP belum dikonfigurasi pada lingkungan ini "
                "(SIAP_DOCUMENT_API_TOKEN / SIAP_API_BASE belum diisi)."
            ),
        }

    @staticmethod
    def _explain_http_error(http_status: int, siap_message: str) -> str:
        """Turn a SIAP error status into an Indonesian explanation."""
        suffix = f" Pesan SIAP: {siap_message}" if siap_message else ""
        if http_status == 401:
            return (
                "Token layanan BIMA tidak diterima SIAP — kemungkinan token "
                "dokumen sudah kedaluwarsa." + suffix
            )
        if http_status == 403:
            return (
                "Token layanan BIMA tidak memiliki izin untuk dokumen ini "
                "(ability document:upload / document:read belum diberikan)." + suffix
            )
        if http_status == 404:
            return "SIAP tidak menemukan permohonan atau dokumen tersebut." + suffix
        if http_status == 422:
            return "Berkas yang dikirim tidak valid menurut SIAP." + suffix
        return f"SIAP merespon dengan HTTP {http_status}." + suffix

    @staticmethod
    def _normalise_document(entry: dict[str, Any]) -> dict[str, Any]:
        """Map one SIAP list entry onto the seam's stable doc shape.

        SIAP sends `{file_id, file_name, file_type, created_on}`; the officer
        loader downstream reads `filename` (the label — load-bearing for dedupe
        and `_resolve_doc_ref`) and `mime`. We prefer SIAP's real keys and fall
        back to the historical `filename`/`mime`/`created_at` so a future shape
        change degrades gracefully. `file_id` is passed through unchanged.
        """
        file_id = entry.get("file_id") or entry.get("id")
        filename = str(entry.get("file_name") or entry.get("filename") or "").strip()
        mime = str(entry.get("file_type") or entry.get("mime") or "").strip()
        created_at = entry.get("created_on") or entry.get("created_at")
        return {
            "file_id": file_id,
            "filename": filename,
            "mime": mime,
            "created_at": created_at,
        }

    # -- public methods -------------------------------------------------------

    async def upload_document(
        self,
        request_id: int,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        """Attach one file to request `request_id` in SIAP.

        Calls `POST /api/v1/license-request/{id}/documents` as multipart/form-
        data with the file under the field name `file`. `filename` is the name
        SIAP records, `content` the raw bytes, `content_type` the MIME type.

        Returns a structured dict — never raises:
          done   → {"ok": True, "configured": True, "file_id": int|None,
                    "note": "", "http_status": int}
          failed → {"ok": False, "configured": True|False, "file_id": None,
                    "note": str, ...}
        """
        if not self.is_configured():
            result = self._not_configured()
            result["file_id"] = None
            return result

        try:
            request_id_int = int(request_id)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "configured": True,
                "file_id": None,
                "note": "request_id harus berupa angka.",
            }

        safe_name = (filename or "").strip() or "dokumen"
        mime = (content_type or "").strip() or "application/octet-stream"
        files = {"file": (safe_name, content, mime)}

        url = f"{self.base}/api/v1/license-request/{request_id_int}/documents"
        # request_id + byte count are non-PII; the filename may echo the
        # applicant's name, so it is NOT logged.
        logger.info(
            "SIAP document upload | request_id=%s | bytes=%d",
            request_id_int, len(content or b""),
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    url, headers=self._headers(), files=files
                )
        except httpx.TimeoutException:
            logger.warning(
                "SIAP document upload timeout | request_id=%s", request_id_int
            )
            return {
                "ok": False,
                "configured": True,
                "file_id": None,
                "note": "SIAP tidak merespon tepat waktu saat mengunggah dokumen.",
            }
        except httpx.RequestError as exc:
            logger.warning(
                "SIAP document upload network error | request_id=%s | err=%s",
                request_id_int, exc,
            )
            return {
                "ok": False,
                "configured": True,
                "file_id": None,
                "note": "Gangguan jaringan saat mengunggah dokumen ke SIAP.",
            }

        try:
            payload = resp.json()
        except ValueError:
            payload = {}

        if 200 <= resp.status_code < 300:
            data = (
                payload.get("data", payload)
                if isinstance(payload, dict)
                else payload
            )
            file_id = None
            if isinstance(data, dict):
                # SIAP's DocumentController returns the new file id as `file_id`
                # (see upload response shape); `id` is a tolerant fallback in
                # case the shape ever changes.
                file_id = data.get("file_id") or data.get("id")
            logger.info(
                "SIAP document upload ok | request_id=%s | http=%d | file_id=%s",
                request_id_int, resp.status_code, file_id,
            )
            return {
                "ok": True,
                "configured": True,
                "http_status": resp.status_code,
                "file_id": file_id,
                "note": "",
            }

        siap_message = ""
        if isinstance(payload, dict):
            siap_message = str(
                payload.get("message") or payload.get("error") or ""
            ).strip()
        logger.warning(
            "SIAP document upload non-2xx | request_id=%s | http=%d | body=%s",
            request_id_int, resp.status_code, resp.text[:300],
        )
        return {
            "ok": False,
            "configured": True,
            "http_status": resp.status_code,
            "file_id": None,
            "note": self._explain_http_error(resp.status_code, siap_message),
        }

    async def list_documents(self, request_id: int) -> dict[str, Any]:
        """List the documents attached to request `request_id` in SIAP.

        Calls `GET /api/v1/license-request/{id}/documents`.

        Returns a structured dict — never raises:
          done   → {"ok": True, "configured": True, "documents": [ {file_id,
                    filename, mime, created_at}, ... ], "note": "",
                    "http_status": int}
          failed → {"ok": False, "configured": True|False, "documents": [],
                    "note": str, ...}

        SIAP's DocumentController sends each document as
        `{file_id, file_name, file_type, created_on}`, NOT the
        `filename/mime/size/created_at` this seam historically assumed. We
        NORMALISE every entry here so the officer loader downstream reads a
        populated `filename`/`mime` — the label drives both the cross-source
        dedupe and officer_copilot's `_resolve_doc_ref` matching, so an empty
        one would silently break them. The old keys are kept as a tolerant
        fallback in case SIAP's shape changes again.
        """
        if not self.is_configured():
            result = self._not_configured()
            result["documents"] = []
            return result

        try:
            request_id_int = int(request_id)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "configured": True,
                "documents": [],
                "note": "request_id harus berupa angka.",
            }

        url = f"{self.base}/api/v1/license-request/{request_id_int}/documents"
        logger.info("SIAP document list | request_id=%s", request_id_int)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers=self._headers())
        except httpx.TimeoutException:
            logger.warning(
                "SIAP document list timeout | request_id=%s", request_id_int
            )
            return {
                "ok": False,
                "configured": True,
                "documents": [],
                "note": "SIAP tidak merespon tepat waktu saat memuat daftar dokumen.",
            }
        except httpx.RequestError as exc:
            logger.warning(
                "SIAP document list network error | request_id=%s | err=%s",
                request_id_int, exc,
            )
            return {
                "ok": False,
                "configured": True,
                "documents": [],
                "note": "Gangguan jaringan saat memuat daftar dokumen dari SIAP.",
            }

        try:
            payload = resp.json()
        except ValueError:
            payload = {}

        if 200 <= resp.status_code < 300:
            # Unwrap SIAP's shape. The real response nests the list under
            # `data.documents` ({request_id, count, documents: [...]}); older/
            # simpler shapes put a flat list under `data`, or at the top level.
            data: Any = payload
            if isinstance(payload, dict):
                data = payload.get("data", payload)
            if isinstance(data, dict):
                data = data.get("documents", [])
            raw_docs = [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
            # Normalise each entry to the keys the officer loader consumes,
            # mapping SIAP's real `file_name`/`file_type`/`created_on` onto
            # `filename`/`mime`/`created_at`. Prefer the SIAP keys, fall back to
            # the historical ones. A populated `filename` is load-bearing (dedupe
            # + _resolve_doc_ref), so never let it silently vanish.
            documents = [self._normalise_document(d) for d in raw_docs]
            logger.info(
                "SIAP document list ok | request_id=%s | http=%d | docs=%d",
                request_id_int, resp.status_code, len(documents),
            )
            return {
                "ok": True,
                "configured": True,
                "http_status": resp.status_code,
                "documents": documents,
                "note": "",
            }

        siap_message = ""
        if isinstance(payload, dict):
            siap_message = str(
                payload.get("message") or payload.get("error") or ""
            ).strip()
        logger.warning(
            "SIAP document list non-2xx | request_id=%s | http=%d | body=%s",
            request_id_int, resp.status_code, resp.text[:300],
        )
        return {
            "ok": False,
            "configured": True,
            "http_status": resp.status_code,
            "documents": [],
            "note": self._explain_http_error(resp.status_code, siap_message),
        }

    async def download_document(self, file_id: int) -> Optional[bytes]:
        """Fetch the raw bytes of one SIAP document by its file id.

        Calls `GET /api/v1/documents/{file_id}/download`. Returns the raw bytes
        on a 2xx, or None on ANY failure (not configured, bad id, timeout,
        network error, non-2xx, empty body, OVERSIZE). NEVER raises — callers on
        the officer doc-loader path must degrade, not crash.

        SIZE-CAPPED: these bytes land in the officer's in-memory session and the
        durable Redis blob, so the body is streamed and aborted the moment it
        exceeds `_SIAP_DOCUMENT_MAX_BYTES` (with a Content-Length pre-flight),
        mirroring siap_templates' template cap and whatsapp_media's media cap.
        An oversize doc is just another None — the officer loader then skips it.
        """
        if not self.is_configured():
            return None

        try:
            file_id_int = int(file_id)
        except (TypeError, ValueError):
            logger.warning("SIAP document download: bad file_id=%r", file_id)
            return None

        url = f"{self.base}/api/v1/documents/{file_id_int}/download"
        logger.info("SIAP document download | file_id=%s", file_id_int)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "GET", url, headers=self._headers(accept_json=False)
                ) as resp:
                    if not (200 <= resp.status_code < 300):
                        logger.warning(
                            "SIAP document download non-2xx | file_id=%s | http=%d",
                            file_id_int, resp.status_code,
                        )
                        return None
                    # Pre-flight on the declared Content-Length when present, so
                    # an oversize doc is refused before we read a single byte.
                    clen = resp.headers.get("content-length")
                    if clen and clen.isdigit() and int(clen) > _SIAP_DOCUMENT_MAX_BYTES:
                        logger.warning(
                            "SIAP document download too large (declared) | "
                            "file_id=%s | bytes=%s", file_id_int, clen,
                        )
                        return None
                    # Stream the body, aborting the moment it exceeds the cap —
                    # a missing/wrong Content-Length can't slip an oversize doc
                    # into the officer session / Redis blob.
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > _SIAP_DOCUMENT_MAX_BYTES:
                            logger.warning(
                                "SIAP document download aborted — exceeded %d bytes "
                                "| file_id=%s",
                                _SIAP_DOCUMENT_MAX_BYTES, file_id_int,
                            )
                            return None
        except httpx.TimeoutException:
            logger.warning(
                "SIAP document download timeout | file_id=%s", file_id_int
            )
            return None
        except httpx.RequestError as exc:
            logger.warning(
                "SIAP document download network error | file_id=%s | err=%s",
                file_id_int, exc,
            )
            return None

        content = bytes(buf)
        if not content:
            logger.warning(
                "SIAP document download empty body | file_id=%s", file_id_int
            )
            return None
        logger.info(
            "SIAP document download ok | file_id=%s | http=%d | bytes=%d",
            file_id_int, resp.status_code, len(content),
        )
        return content


# Singleton — built once per process to reuse env values.
_client: Optional[SiapDocumentClient] = None


def get_siap_document_client() -> SiapDocumentClient:
    global _client
    if _client is None:
        _client = SiapDocumentClient()
    return _client
