import { NextRequest, NextResponse } from "next/server";

/**
 * Edge middleware — gates protected pages by cookie presence.
 *
 * What it does:
 *   - On request to /me (or any future protected route in PROTECTED_PATHS),
 *     check for the SSO cookie. If absent, redirect to /login with ?next=
 *     so the user lands back where they came from after authenticating.
 *
 * What it does NOT do:
 *   - Verify the JWT signature. We can't — SECRET_KEY lives in admin-api,
 *     not the portal. The presence check is a cheap edge gate; the actual
 *     authorization check happens inside the page (getServerUser hits
 *     admin-api which DOES verify).
 *   - Block API routes. /api/sso/me handles its own 401 path.
 *
 * Performance note: middleware runs on every matched request. Keep
 * PROTECTED_PATHS narrow.
 */

const COOKIE_NAME = "bima_pemohon_token";

// Exact prefix match. If we add /dokumen, /tagihan, etc. they go here.
const PROTECTED_PREFIXES = ["/me"];

export function middleware(req: NextRequest) {
  const { pathname, search } = req.nextUrl;

  const isProtected = PROTECTED_PREFIXES.some((p) => pathname.startsWith(p));
  if (!isProtected) {
    return NextResponse.next();
  }

  const hasCookie = Boolean(req.cookies.get(COOKIE_NAME)?.value);
  if (hasCookie) {
    return NextResponse.next();
  }

  const url = req.nextUrl.clone();
  url.pathname = "/login";
  // Preserve the original path + query so post-login lands back here.
  url.search = `?next=${encodeURIComponent(pathname + search)}`;
  return NextResponse.redirect(url);
}

/**
 * Matcher: only run on /me/* — keeps the middleware off the main landing
 * page and static assets. Add prefixes here when adding new protected routes.
 */
export const config = {
  matcher: ["/me/:path*"],
};
