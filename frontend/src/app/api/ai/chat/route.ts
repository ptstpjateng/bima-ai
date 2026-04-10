import { NextRequest, NextResponse } from "next/server";

// Server-side proxy: Next.js portal → ai-engine /webhook/chat
// Keeps the ai-engine URL server-only and handles auth validation.

const AI_ENGINE_URL =
  process.env.AI_ENGINE_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://116.254.113.81";

export async function POST(req: NextRequest) {
  // Require a valid Authorization header (Sanctum token from the portal)
  const authHeader = req.headers.get("Authorization");
  if (!authHeader?.startsWith("Bearer ")) {
    return NextResponse.json({ message: "Unauthorized" }, { status: 401 });
  }

  let body: { message?: unknown; user_id?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ message: "Invalid JSON body" }, { status: 400 });
  }

  const message = typeof body.message === "string" ? body.message.trim() : "";
  const userId = typeof body.user_id === "string" ? body.user_id : "";

  if (!message || !userId) {
    return NextResponse.json(
      { message: "message and user_id are required" },
      { status: 400 },
    );
  }

  try {
    const upstream = await fetch(`${AI_ENGINE_URL}/webhook/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, message }),
      // Next.js fetch cache: no-store so every chat message is fresh
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
