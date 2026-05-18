/**
 * Catch-all Route Handler for SSO traffic between the portal browser and
 * admin-api.
 *
 * The browser never sees admin-api directly — it talks to /api/sso/*, and
 * THIS handler is what:
 *   - Forwards the request to admin-api with the BIMA_ADMIN_API_URL base
 *   - Reads/writes the httpOnly `bima_pemohon_token` cookie
 *   - Strips the bearer token out of the JSON before it reaches the browser
 *
 * Routes handled (slug = the path segments after /api/sso/):
 *   POST /api/sso/login    → admin-api POST /sso/login   (sets cookie on 200)
 *   GET  /api/sso/me       → admin-api GET  /sso/me      (sends cookie as bearer)
 *   POST /api/sso/logout   → admin-api POST /sso/logout  (clears cookie)
 *
 * Anything else under /api/sso/* gets a 404 so a typo in the client doesn't
 * silently become a wide proxy.
 */

import { cookies } from "next/headers";
import { NextRequest, NextResponse } from "next/server";

// The portal is HTTP-only against the browser but we ship it as a fully SSR
// route — explicit runtime + dynamic ensures we don't accidentally cache
// authenticated responses at the framework edge.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Cookie name kept narrow and namespaced so it never collides with the admin
// app's NextAuth cookies. Max-Age = 7d matches admin-api JWT_EXPIRY_DAYS.
const COOKIE_NAME = "bima_pemohon_token";
const COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 7;

// We split slugs into a small set rather than a single passthrough — every
// admin-api endpoint that this handler relays needs explicit cookie wiring,
// and an allow-list is the right shape for that.
type Action = "login" | "me" | "logout";

function pickAction(slug: string[] | undefined, method: string): Action | null {
  if (!slug || slug.length !== 1) return null;
  const [head] = slug;
  if (head === "login" && method === "POST") return "login";
  if (head === "me" && method === "GET") return "me";
  if (head === "logout" && method === "POST") return "logout";
  return null;
}

function adminApiBase(): string | null {
  const raw = process.env.BIMA_ADMIN_API_URL;
  if (!raw) return null;
  return raw.replace(/\/$/, "");
}

// ----- POST /api/sso/login --------------------------------------------------
async function handleLogin(req: NextRequest): Promise<NextResponse> {
  const base = adminApiBase();
  if (!base) {
    return NextResponse.json(
      { error: "BIMA_ADMIN_API_URL not configured." },
      { status: 503 },
    );
  }

  const body = await req.json().catch(() => null);
  if (!body || typeof body !== "object") {
    return NextResponse.json({ error: "Invalid JSON body." }, { status: 400 });
  }

  const upstream = await fetch(`${base}/sso/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  }).catch(() => null);

  if (!upstream) {
    return NextResponse.json(
      { error: "admin-api unreachable." },
      { status: 502 },
    );
  }

  if (!upstream.ok) {
    // Pass status through unchanged so the client maps 404/422/503 itself.
    // We deliberately don't pass the admin-api detail body — those strings
    // are server-tier copy; the client owns user-facing translations.
    return NextResponse.json(
      { error: `admin-api returned ${upstream.status}` },
      { status: upstream.status },
    );
  }

  // Successful login — extract token, drop it into a cookie, return only
  // the user payload to the browser.
  const payload = (await upstream.json()) as {
    access_token: string;
    token_type: string;
    user: unknown;
  };

  const resp = NextResponse.json({ user: payload.user });
  resp.cookies.set({
    name: COOKIE_NAME,
    value: payload.access_token,
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: COOKIE_MAX_AGE_SECONDS,
  });
  return resp;
}

// ----- GET /api/sso/me ------------------------------------------------------
async function handleMe(): Promise<NextResponse> {
  const base = adminApiBase();
  if (!base) {
    return NextResponse.json(
      { error: "BIMA_ADMIN_API_URL not configured." },
      { status: 503 },
    );
  }

  const jar = await cookies();
  const token = jar.get(COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const upstream = await fetch(`${base}/sso/me`, {
    method: "GET",
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  }).catch(() => null);

  if (!upstream) {
    return NextResponse.json(
      { error: "admin-api unreachable." },
      { status: 502 },
    );
  }
  if (!upstream.ok) {
    // If the upstream says 401 (e.g. expired token), proactively clear the
    // cookie. The browser would otherwise keep re-sending it and the user
    // would loop on stale auth.
    const out = NextResponse.json(
      { error: `admin-api returned ${upstream.status}` },
      { status: upstream.status },
    );
    if (upstream.status === 401) {
      out.cookies.delete(COOKIE_NAME);
    }
    return out;
  }

  const data = await upstream.json();
  return NextResponse.json(data);
}

// ----- POST /api/sso/logout -------------------------------------------------
async function handleLogout(): Promise<NextResponse> {
  const base = adminApiBase();
  // Best-effort upstream notify; don't block cookie clearing on it.
  if (base) {
    const jar = await cookies();
    const token = jar.get(COOKIE_NAME)?.value;
    if (token) {
      // Fire-and-forget — we don't await the response. The admin-api
      // endpoint is a no-op today, but keeping the call here means
      // adding a denylist later doesn't require a portal change.
      fetch(`${base}/sso/logout`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        cache: "no-store",
      }).catch(() => undefined);
    }
  }

  const resp = NextResponse.json({ ok: true });
  resp.cookies.delete(COOKIE_NAME);
  return resp;
}

// ----- Method handlers ------------------------------------------------------
export async function POST(
  req: NextRequest,
  ctx: { params: Promise<{ slug?: string[] }> },
): Promise<NextResponse> {
  const { slug } = await ctx.params;
  const action = pickAction(slug, "POST");

  if (action === "login") return handleLogin(req);
  if (action === "logout") return handleLogout();
  return NextResponse.json({ error: "Not found." }, { status: 404 });
}

export async function GET(
  _req: NextRequest,
  ctx: { params: Promise<{ slug?: string[] }> },
): Promise<NextResponse> {
  const { slug } = await ctx.params;
  const action = pickAction(slug, "GET");

  if (action === "me") return handleMe();
  return NextResponse.json({ error: "Not found." }, { status: 404 });
}
