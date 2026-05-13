import { NextResponse } from "next/server";
import { cookies } from "next/headers";

/**
 * Same-origin proxy to admin-api.
 *
 * Browser fetches `/api/proxy/<path>` (same origin → no CORS, JS never sees
 * the JWT). This handler reads the httpOnly `admin_token` cookie set by
 * `/api/auth/set-token` after login, then forwards the request to
 * `${NEXT_PUBLIC_ADMIN_API_URL}/<path>` with `Authorization: Bearer ...`.
 *
 * Why a proxy and not direct browser → admin-api:
 *  - The login flow stores the JWT in an httpOnly cookie (XSS mitigation).
 *  - Client JS therefore can't attach the token itself; only this server
 *    handler can read the cookie.
 *  - Same-origin removes the need for CORS preflights for every list page.
 */

const ADMIN_API_URL =
  process.env.NEXT_PUBLIC_ADMIN_API_URL ?? "http://localhost:8001";

// 25s upstream timeout — admin-api list queries should always come back well
// within this; a hang past this point means upstream death and we want to
// surface it to the user instead of holding a Vercel Function open.
const UPSTREAM_TIMEOUT_MS = 25_000;

type Ctx = { params: Promise<{ path: string[] }> };

async function forward(req: Request, ctx: Ctx, method: string) {
  const { path } = await ctx.params;
  const segments = (path ?? []).map(encodeURIComponent).join("/");
  const incoming = new URL(req.url);
  const upstreamUrl = `${ADMIN_API_URL}/${segments}${incoming.search}`;

  const cookieStore = await cookies();
  const token = cookieStore.get("admin_token")?.value;

  if (!token) {
    return NextResponse.json(
      { error: "unauthenticated", message: "No admin session." },
      { status: 401 }
    );
  }

  // Forward only safe headers; skip cookies, host, and auth-related ones we
  // own. Body is streamed for write methods, omitted for GET/HEAD.
  const forwardedHeaders = new Headers();
  const incomingContentType = req.headers.get("content-type");
  if (incomingContentType) {
    forwardedHeaders.set("content-type", incomingContentType);
  }
  forwardedHeaders.set("authorization", `Bearer ${token}`);
  forwardedHeaders.set("accept", "application/json");

  const init: RequestInit = {
    method,
    headers: forwardedHeaders,
    cache: "no-store",
  };
  if (method !== "GET" && method !== "HEAD") {
    init.body = await req.arrayBuffer();
  }

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), UPSTREAM_TIMEOUT_MS);
  init.signal = ctrl.signal;

  let upstream: Response;
  try {
    upstream = await fetch(upstreamUrl, init);
  } catch (err) {
    clearTimeout(timer);
    const aborted = err instanceof Error && err.name === "AbortError";
    return NextResponse.json(
      {
        error: aborted ? "upstream_timeout" : "upstream_unreachable",
        message: aborted
          ? "Admin API tidak menjawab dalam batas waktu."
          : "Tidak bisa menghubungi admin API.",
      },
      { status: aborted ? 504 : 502 }
    );
  }
  clearTimeout(timer);

  // Stream the upstream response back, preserving status + content-type.
  // We strip set-cookie (admin-api may try to set its own cookies that we
  // don't want leaking through to the admin frontend domain).
  const responseHeaders = new Headers();
  const upstreamContentType = upstream.headers.get("content-type");
  if (upstreamContentType) {
    responseHeaders.set("content-type", upstreamContentType);
  }

  const body = await upstream.arrayBuffer();
  return new NextResponse(body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

export async function GET(req: Request, ctx: Ctx) {
  return forward(req, ctx, "GET");
}
export async function POST(req: Request, ctx: Ctx) {
  return forward(req, ctx, "POST");
}
export async function PUT(req: Request, ctx: Ctx) {
  return forward(req, ctx, "PUT");
}
export async function PATCH(req: Request, ctx: Ctx) {
  return forward(req, ctx, "PATCH");
}
export async function DELETE(req: Request, ctx: Ctx) {
  return forward(req, ctx, "DELETE");
}
