import { NextRequest, NextResponse } from "next/server";

// Server-side proxy: Next.js portal → ai-engine /webhook/chat
// Validates the Sanctum token against the backend /auth/me before forwarding,
// then uses the server-verified user_id — never trusts the client-supplied value.
//
// AUTH (security/gate-webhook-chat): ai-engine /webhook/chat now requires
// either a citizen-SSO JWT bearer or an X-Internal-Key. This proxy already
// validates the Sanctum session against the legacy backend, so it is in
// trusted-server position — we pass the shared INTERNAL_API_KEY along with
// the verified `user_id`. The browser never holds either secret.
//
// Note: this `frontend/` directory is the deprecated legacy portal; the live
// portal at `portal/` does its citizen auth via admin-api SSO and would call
// ai-engine with the citizen JWT instead. Kept here so the legacy path,
// while it remains in the tree, fails closed rather than wide-open.

const AI_ENGINE_URL =
  process.env.AI_ENGINE_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://116.254.113.81";

const BACKEND_URL =
  process.env.BACKEND_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://116.254.113.81";

// Server-only — must NEVER be exposed to the browser bundle. Next.js keeps
// non-NEXT_PUBLIC_ env vars server-side automatically, so reading from a
// Route Handler (this file) is safe.
const INTERNAL_API_KEY =
  process.env.BIMA_INTERNAL_API_KEY ?? process.env.INTERNAL_API_KEY ?? "";

export async function POST(req: NextRequest) {
  const authHeader = req.headers.get("Authorization");
  if (!authHeader?.startsWith("Bearer ")) {
    return NextResponse.json({ message: "Unauthorized" }, { status: 401 });
  }

  // Validate token against the backend and get the real user id.
  let verifiedUserId: number;
  try {
    const meRes = await fetch(`${BACKEND_URL}/api/auth/me`, {
      headers: {
        Authorization: authHeader,
        Accept: "application/json",
      },
      cache: "no-store",
    });

    if (!meRes.ok) {
      return NextResponse.json({ message: "Unauthorized" }, { status: 401 });
    }

    const meData = (await meRes.json()) as {
      data?: { user?: { id?: number } };
    };
    const uid = meData?.data?.user?.id;
    if (!uid || typeof uid !== "number") {
      return NextResponse.json({ message: "Unauthorized" }, { status: 401 });
    }
    verifiedUserId = uid;
  } catch {
    return NextResponse.json(
      { message: "Tidak dapat memverifikasi sesi. Coba lagi." },
      { status: 503 },
    );
  }

  let body: { message?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ message: "Invalid JSON body" }, { status: 400 });
  }

  const message = typeof body.message === "string" ? body.message.trim() : "";
  if (!message) {
    return NextResponse.json(
      { message: "message is required" },
      { status: 400 },
    );
  }

  if (message.length > 2000) {
    return NextResponse.json(
      { message: "Pesan terlalu panjang (maks 2000 karakter)." },
      { status: 422 },
    );
  }

  // Without the shared secret we cannot authenticate to ai-engine. Fail closed
  // — surfacing 503 here is the same posture admin-api takes when an upstream
  // is misconfigured (better than silently leaking an unauthenticated call).
  if (!INTERNAL_API_KEY) {
    console.error("[ai/chat] BIMA_INTERNAL_API_KEY not configured");
    return NextResponse.json(
      { message: "Asisten AI tidak dikonfigurasi." },
      { status: 503 },
    );
  }

  try {
    const upstream = await fetch(`${AI_ENGINE_URL}/webhook/chat`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Internal-Key": INTERNAL_API_KEY,
      },
      // user_id uses verified server-side id — never client-supplied
      body: JSON.stringify({ user_id: `web-${verifiedUserId}`, message }),
      cache: "no-store",
    });

    const data = await upstream.json();

    if (!upstream.ok) {
      return NextResponse.json(
        { message: data?.message ?? "AI engine error" },
        { status: upstream.status },
      );
    }

    return NextResponse.json(data);
  } catch (err) {
    console.error("[ai/chat] upstream fetch failed:", err);
    return NextResponse.json(
      { message: "Tidak dapat menghubungi asisten AI. Coba lagi." },
      { status: 503 },
    );
  }
}
