import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * Admin route guard.
 *
 * Any non-login route requires an `admin_token` cookie set by the
 * /api/auth/set-token route handler after a successful login.
 * Unauthenticated visitors are bounced to /login.
 *
 * Phase 2 will add token *validation* against admin-api `/auth/me` (today the
 * middleware only checks presence — a forged cookie would still pass here but
 * would fail at the API boundary on first authenticated request).
 */
export function proxy(req: NextRequest) {
  const token = req.cookies.get("admin_token")?.value;
  const { pathname } = req.nextUrl;

  const isAuthRoute = pathname.startsWith("/login");
  const isPublicApi = pathname.startsWith("/api/auth");

  // Authenticated user hitting /login → straight to dashboard.
  if (token && isAuthRoute) {
    return NextResponse.redirect(new URL("/dashboard", req.url));
  }

  // Unauthenticated user hitting anything except /login or auth APIs → /login.
  if (!token && !isAuthRoute && !isPublicApi) {
    const loginUrl = new URL("/login", req.url);
    // Preserve where the user wanted to go so we can bounce them back post-login.
    if (pathname !== "/") {
      loginUrl.searchParams.set("next", pathname);
    }
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!api/auth|_next/static|_next/image|favicon.ico).*)"],
};
