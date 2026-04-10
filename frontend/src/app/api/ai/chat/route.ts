import { NextRequest, NextResponse } from "next/server";

// Server-side proxy: Next.js portal → ai-engine /webhook/chat
// Validates the Sanctum token against the backend /auth/me before forwarding,
// then uses the server-verified user_id — never trusts the client-supplied value.

const AI_ENGINE_URL =
  process.env.AI_ENGINE_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://116.254.113.81";

const BACKEND_URL =
  process.env.BACKEND_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://116.254.113.81";

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

  try {
    const upstream = await fetch(`${AI_ENGINE_URL}/webhook/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
