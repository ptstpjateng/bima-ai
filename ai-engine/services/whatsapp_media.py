"""
WhatsApp inbound MEDIA download — fetch the bytes of an image/PDF a citizen
sent over WhatsApp so the guided-submission flow can content-score it.

────────────────────────────────────────────────────────────────────────────
WHAT IS / ISN'T KNOWN (read before trusting this module)
────────────────────────────────────────────────────────────────────────────
APTANA's inbound webhook (routers/aptana.py) forwards Meta's message JSON in
its `payload` field. For TEXT that shape is confirmed. For MEDIA it is NOT —
APTANA's Postman docs show text only, and we have no captured media payload
yet. So the EXTRACTION (services/aptana media parser) is best-effort against
Meta's documented Cloud-API media shape, and the DOWNLOAD has two supported
mechanisms because APTANA's docs don't say which one applies:

  1. INLINE LINK  — some payloads carry a ready `link`/`url` to the binary
     (Meta inlines this for `document` in certain configs; a BSP may also
     pre-resolve the media to a hosted URL). We fetch it directly, but ONLY
     from an allow-list of hosts (SSRF guard — see `_HOST_ALLOWLIST`).

  2. OPAQUE META MEDIA ID — the documented Meta Cloud-API flow: the message
     carries an opaque `id`; you GET `https://graph.facebook.com/<ver>/<id>`
     with the Meta bearer token to get a short-lived signed `url`, then GET
     that url (also bearer-authed) for the bytes. This path needs
     `META_WA_TOKEN` + `META_GRAPH_VERSION` (same env the native-typing path
     already uses). When the token isn't set, this path is skipped.

If a real captured payload later shows an APTANA-NATIVE media endpoint (e.g.
`GET {APTANA_API_BASE}/api/v1/media/{id}` with the `Api-Token` header), it
slots in as a third branch in `download_media()` — the call sites don't change.

────────────────────────────────────────────────────────────────────────────
SECURITY (hard requirements — do not relax without review)
────────────────────────────────────────────────────────────────────────────
* SSRF: only ever fetch from hosts in `_HOST_ALLOWLIST` (APTANA + Meta Graph +
  Meta's `lookaside`/`*.fbcdn.net` media CDN). An arbitrary `link` in a hostile
  payload is REJECTED. No redirects are followed to off-allow-list hosts.
* SIZE: streamed read aborts past `_MAX_MEDIA_BYTES` (~10 MB) — a hostile or
  huge file can't exhaust memory.
* CONTENT-TYPE: only `image/jpeg`, `image/png`, `image/webp`,
  `application/pdf` are accepted (matches what the suitability judge can read).
* TIMEOUT: every fetch is bounded by `_FETCH_TIMEOUT_SECONDS`.
* PII: document bytes are NEVER logged. Only the masked media id, host, content
  type, and byte length are logged.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger("bima_ai.whatsapp_media")


# ===========================================================================
# Security constants
# ===========================================================================

# ~10 MB hard cap (the user decision). WhatsApp images are <16 MB and docs
# <100 MB at the API level, but for a chat-scored permit packet 10 MB is plenty
# and keeps a single inbound burst from exhausting memory.
_MAX_MEDIA_BYTES = 10 * 1024 * 1024

# Allow-list of MIME types the suitability judge / Gemini Vision can read.
ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
}

_FETCH_TIMEOUT_SECONDS = 30.0

# Redirects are NOT auto-followed (httpx follow_redirects=False); we resolve
# them manually so the allow-list + IP-safety guards run on EVERY hop. This
# caps how many 30x hops we'll chase before giving up (defeats redirect loops
# and long redirect chains used to slip past a single entry-URL check).
_MAX_REDIRECT_HOPS = 3

# SSRF allow-list. A fetched URL's host must END WITH one of these suffixes,
# otherwise the download is refused. Covers:
#   * APTANA's API host (in case it hosts/proxies the media binary)
#   * Meta Graph (opaque-id resolution + the signed media url it returns,
#     which is itself on graph.facebook.com)
#   * Meta media CDN (`lookaside.fbsbx.com`, `*.fbcdn.net`) which the signed
#     url sometimes redirects to.
_HOST_ALLOWLIST: tuple[str, ...] = (
    "api-multichannel.aptana.co.id",
    "aptana.co.id",
    "graph.facebook.com",
    "lookaside.fbsbx.com",
    "fbcdn.net",
)

_GRAPH_API_VERSION = os.getenv("META_GRAPH_VERSION", "v22.0").strip() or "v22.0"


def _aptana_host() -> str:
    base = os.getenv("APTANA_API_BASE", "https://api-multichannel.aptana.co.id")
    try:
        return (urlparse(base).hostname or "").lower()
    except Exception:
        return ""


def _meta_token() -> str:
    return os.getenv("META_WA_TOKEN", "").strip()


def _ip_is_blocked(ip: "ipaddress._BaseAddress") -> bool:
    """True for addresses that must never be a download destination: private
    (RFC1918 / ULA), loopback, link-local, or otherwise non-globally-routable.
    This is the core SSRF-to-internal-network guard."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def _dest_addresses_ok(host: str) -> bool:
    """Reject hosts that point at the internal network.

    * IP-literal host → parse it directly and reject if private/loopback/
      link-local/etc. (a hostile `link` can name an IP straight up, e.g.
      https://169.254.169.254/… or https://127.0.0.1/…).
    * Hostname → resolve via socket.getaddrinfo and reject if ANY resolved IP
      is blocked (defeats DNS that points an otherwise innocent name at an
      internal address). Resolution failure is treated as NOT ok (fail
      closed). Never raises.
    """
    if not host:
        return False
    # IP literal? (also handles bracketed IPv6 once the brackets are stripped)
    literal = host.strip("[]")
    try:
        ip = ipaddress.ip_address(literal)
        return not _ip_is_blocked(ip)
    except ValueError:
        pass  # not an IP literal — resolve the hostname below
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError, UnicodeError):
        logger.warning("media download refused (host did not resolve) | host=%s", host)
        return False
    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if _ip_is_blocked(ip):
            logger.warning(
                "media download refused (resolves to non-routable IP) | host=%s",
                host,
            )
            return False
    return True


def _host_allowed(url: str) -> bool:
    """True when `url` is https AND its host matches the allow-list (suffix
    match so subdomains of an allowed domain pass, e.g. *.fbcdn.net) AND the
    host does not resolve to a private/loopback/link-local address. Also
    accepts the configured APTANA host even if its domain differs from the
    static list. SSRF guard — refuse everything else."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    allowed = set(_HOST_ALLOWLIST)
    extra = _aptana_host()
    if extra:
        allowed.add(extra)
    # Exact match OR a subdomain of an allowed domain (".fbcdn.net" matches
    # "scontent.xx.fbcdn.net"). Suffix-with-dot prevents "evilfbcdn.net".
    if not any(host == a or host.endswith("." + a) for a in allowed):
        return False
    # Even an allow-listed name must not resolve to the internal network — a
    # compromised/poisoned DNS record for an allowed host, or a literal IP
    # that happens to suffix-match, is still refused.
    return _dest_addresses_ok(host)


def _mask(value: Optional[str]) -> str:
    if not value:
        return "<none>"
    s = str(value)
    if len(s) <= 6:
        return "*" * len(s)
    return s[:3] + "…" + s[-3:]


# ===========================================================================
# Parsed-media descriptor (what the aptana parser hands us)
# ===========================================================================


@dataclass
class InboundMedia:
    """One media item extracted from an inbound WhatsApp message.

    Exactly one of `link` / `media_id` is the download handle:
      * link     — a direct (allow-listed) https URL to the binary.
      * media_id — an opaque Meta media id resolved via the Graph API.
    `mime_type` / `filename` are best-effort hints from the payload (may be
    None; we re-check the content-type from the HTTP response on download)."""

    kind: str                       # image | document | audio | video | sticker
    media_id: Optional[str] = None
    link: Optional[str] = None
    mime_type: Optional[str] = None
    filename: Optional[str] = None


@dataclass
class DownloadedMedia:
    content: bytes
    mime_type: str
    filename: str


# ===========================================================================
# Download
# ===========================================================================


# Leading magic bytes for the four accepted content types. Used to sniff the
# real type when the HTTP response has no (or an empty) Content-Type, so we
# never fall back to trusting the caller-supplied `mime_type` hint alone.
def _sniff_content_type(head: bytes) -> str:
    """Return the ALLOWED content type implied by `head`'s magic bytes, or ""
    when nothing recognised matches."""
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    # RIFF....WEBP  (4-byte "RIFF", 4-byte size, "WEBP")
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _redirect_location(resp: httpx.Response, current_url: str) -> Optional[str]:
    """Extract and absolutise a redirect Location header, or None when absent."""
    loc = resp.headers.get("location") or resp.headers.get("Location")
    if not loc:
        return None
    # Resolve relative redirects against the URL we just fetched.
    return urljoin(current_url, loc.strip())


async def _stream_to_bytes(
    client: httpx.AsyncClient, url: str, *, headers: Optional[dict] = None
) -> Optional[tuple[bytes, str]]:
    """GET `url` and stream the body, aborting past the size cap. Returns
    (bytes, content_type) or None on any failure / disallowed host / oversize /
    disallowed content type. Never raises. Bytes are never logged.

    Redirects are followed MANUALLY (the client is created with
    follow_redirects=False) so the SSRF allow-list + IP-safety guard run on
    EVERY hop's resolved Location before we re-issue the request — an
    auto-followed 30x from an allow-listed host to an internal/arbitrary host
    would otherwise bypass the entry-URL check. Capped at _MAX_REDIRECT_HOPS.
    The Authorization header (the Meta bearer) is DROPPED whenever a redirect
    changes the host, so the token never leaks off-Meta.
    """
    current_url = url
    current_headers = dict(headers or {})

    for hop in range(_MAX_REDIRECT_HOPS + 1):
        if not _host_allowed(current_url):
            logger.warning(
                "media download refused (host not allow-listed) | host=%s",
                (urlparse(current_url).hostname or "<none>"),
            )
            return None
        try:
            async with client.stream(
                "GET", current_url, headers=current_headers
            ) as resp:
                status = resp.status_code

                # --- Redirect: validate the NEXT hop before following it. ---
                if 300 <= status < 400:
                    location = _redirect_location(resp, current_url)
                    if not location:
                        logger.warning(
                            "media download redirect without Location | status=%d",
                            status,
                        )
                        return None
                    if hop >= _MAX_REDIRECT_HOPS:
                        logger.warning(
                            "media download too many redirects (>%d)",
                            _MAX_REDIRECT_HOPS,
                        )
                        return None
                    # Drop the Authorization header when the host changes so the
                    # Meta bearer is never sent off-Meta. _host_allowed() runs on
                    # the new URL at the top of the next loop iteration.
                    old_host = (urlparse(current_url).hostname or "").lower()
                    new_host = (urlparse(location).hostname or "").lower()
                    if new_host != old_host and "authorization" in {
                        k.lower() for k in current_headers
                    }:
                        current_headers = {
                            k: v
                            for k, v in current_headers.items()
                            if k.lower() != "authorization"
                        }
                    current_url = location
                    continue  # re-issue against the validated next hop

                if status >= 300:
                    logger.warning("media download non-2xx | status=%d", status)
                    return None

                ctype = (
                    (resp.headers.get("content-type") or "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                # A PRESENT content-type must be in the allow-list outright.
                if ctype and ctype not in ALLOWED_CONTENT_TYPES:
                    logger.warning("media download rejected content-type=%s", ctype)
                    return None
                # Pre-flight on Content-Length when present.
                clen = resp.headers.get("content-length")
                if clen and clen.isdigit() and int(clen) > _MAX_MEDIA_BYTES:
                    logger.warning(
                        "media download too large (declared) | bytes=%s", clen
                    )
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > _MAX_MEDIA_BYTES:
                        logger.warning(
                            "media download aborted — exceeded %d bytes",
                            _MAX_MEDIA_BYTES,
                        )
                        return None

                content = bytes(buf)
                # When the response carried NO (or an empty) Content-Type, do
                # not trust the payload hint — sniff the magic bytes and require
                # a recognised, allow-listed type. An unrecognised body is
                # refused here rather than passed through to the mime-hint
                # fallback in download_media().
                if not ctype:
                    sniffed = _sniff_content_type(content[:16])
                    if sniffed not in ALLOWED_CONTENT_TYPES:
                        logger.warning(
                            "media download rejected — missing content-type and "
                            "unrecognised magic bytes"
                        )
                        return None
                    ctype = sniffed
                return content, ctype
        except (httpx.RequestError, httpx.HTTPError) as exc:
            logger.warning("media download error | err=%s", exc.__class__.__name__)
            return None

    # Loop fell through (all hops were redirects up to the cap).
    logger.warning("media download too many redirects (>%d)", _MAX_REDIRECT_HOPS)
    return None


async def _resolve_meta_media_url(client: httpx.AsyncClient, media_id: str) -> Optional[str]:
    """Resolve an opaque Meta media id to a short-lived signed URL via the
    Graph API. Needs META_WA_TOKEN. Returns the url or None. Never raises."""
    token = _meta_token()
    if not token:
        logger.info(
            "media: opaque id but META_WA_TOKEN unset — cannot resolve via Graph "
            "| id=%s", _mask(media_id),
        )
        return None
    url = f"https://graph.facebook.com/{_GRAPH_API_VERSION}/{media_id}"
    try:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code >= 300:
            logger.warning("media id resolve non-2xx | status=%d", resp.status_code)
            return None
        data = resp.json()
        link = data.get("url") if isinstance(data, dict) else None
        return link if isinstance(link, str) and link else None
    except (httpx.RequestError, httpx.HTTPError, ValueError) as exc:
        logger.warning("media id resolve error | err=%s", exc.__class__.__name__)
        return None


_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


async def download_media(media: InboundMedia) -> Optional[DownloadedMedia]:
    """Download the bytes for one inbound media item, with all security guards.

    Resolution order:
      1. If `media.link` is set → fetch it directly (allow-listed host only).
      2. Else if `media.media_id` is set → resolve via Meta Graph to a signed
         url, then fetch (both bearer-authed, both allow-listed).
    Returns a `DownloadedMedia` (content + verified mime + a safe filename) or
    None on any failure. Never raises. Bytes are never logged."""
    token = _meta_token()
    auth_headers = {"Authorization": f"Bearer {token}"} if token else None

    # follow_redirects=False: redirects are resolved manually in
    # _stream_to_bytes so the allow-list + IP-safety guard run on every hop
    # (an auto-followed 30x would bypass the entry-URL SSRF check).
    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=False
    ) as client:
        fetched: Optional[tuple[bytes, str]] = None

        if media.link:
            # A direct link in the payload. Meta's signed urls live on
            # graph.facebook.com and want the bearer token; an APTANA-hosted
            # url is allow-listed too. Sending the bearer to an allow-listed
            # non-Meta host is harmless (it's our own token, only over TLS to a
            # trusted host) but we scope it to Meta hosts to be safe.
            link_host = (urlparse(media.link).hostname or "").lower()
            send_auth = auth_headers if ("facebook.com" in link_host or "fbsbx.com" in link_host or "fbcdn.net" in link_host) else None
            fetched = await _stream_to_bytes(client, media.link, headers=send_auth)
        elif media.media_id:
            signed = await _resolve_meta_media_url(client, media.media_id)
            if signed:
                fetched = await _stream_to_bytes(client, signed, headers=auth_headers)

        if fetched is None:
            return None

        content, resp_ctype = fetched

    # Decide the final mime: trust the HTTP content-type when it's allow-listed,
    # else fall back to the payload hint when THAT is allow-listed.
    mime = resp_ctype if resp_ctype in ALLOWED_CONTENT_TYPES else ""
    if not mime and media.mime_type in ALLOWED_CONTENT_TYPES:
        mime = media.mime_type  # type: ignore[assignment]
    if mime not in ALLOWED_CONTENT_TYPES:
        logger.warning(
            "media rejected — unresolved/disallowed content type | resp=%s hint=%s",
            resp_ctype or "<none>", media.mime_type or "<none>",
        )
        return None

    if not content:
        return None

    # Build a safe filename (never trust the payload filename verbatim for a
    # path — we only use it as a label). Derive an extension from the mime.
    ext = _EXT_BY_MIME.get(mime, "")
    base = media.kind or "dokumen"
    filename = (media.filename or f"{base}{ext}").strip() or f"{base}{ext}"

    logger.info(
        "media downloaded | kind=%s id=%s mime=%s bytes=%d",
        media.kind, _mask(media.media_id or media.link), mime, len(content),
    )
    return DownloadedMedia(content=content, mime_type=mime, filename=filename)
