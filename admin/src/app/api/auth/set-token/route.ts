import { NextResponse } from "next/server";

/**
 * Stores the admin-api JWT in an httpOnly cookie.
 *
 * The login page (`/login`) POSTs the user's email/password directly to
 * admin-api's `/auth/login`. On success the page calls THIS handler with
 * the returned JWT, which we then drop into a Set-Cookie that JavaScript
 * cannot read (mitigates XSS token exfiltration).
 *
 * Phase 2: refactor so the login POST itself comes through this route
 * handler (proxy pattern) — then the JWT is never exposed to client JS
 * even briefly. Phase 1 keeps the flow simple to unblock the shell.
 */
export async function POST(req: Request) {
  let body: { token?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid_json" }, { status: 400 });
  }

  const token = body?.token;
  if (typeof token !== "string" || token.length < 16 || token.length > 4096) {
    return NextResponse.json({ error: "invalid_token" }, { status: 400 });
  }

  const res = NextResponse.json({ ok: true });
  res.cookies.set("admin_token", token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    // 8h — admin sessions short by design; refresh-token flow lands Phase 2.
    maxAge: 60 * 60 * 8,
  });
  return res;
}

export async function DELETE() {
  const res = NextResponse.json({ ok: true });
  res.cookies.set("admin_token", "", {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: 0,
  });
  return res;
}
