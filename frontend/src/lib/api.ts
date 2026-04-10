import type {
  ApiResponse,
  AuthPayload,
  AiInteraction,
  KbliOption,
  PermitApplication,
  PermitApplyPayload,
  User,
} from "@/types";
import { getToken } from "./auth";

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "";

class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<ApiResponse<T>> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
    ...(options.headers as Record<string, string>),
  };

  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetch(`${BASE_URL}/api${path}`, {
    ...options,
    headers,
  });

  const body = await res.json();

  if (!res.ok) {
    throw new ApiError(
      res.status,
      body?.message ?? "Terjadi kesalahan",
      body?.error_code,
    );
  }

  return body as ApiResponse<T>;
}

// ── Auth ─────────────────────────────────────────────────────────────────────

export async function login(email: string, password: string): Promise<AuthPayload> {
  const res = await request<AuthPayload>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  return res.data;
}

export async function redeemMagicLink(token: string): Promise<AuthPayload> {
  const res = await request<AuthPayload>(`/auth/magic/${encodeURIComponent(token)}`);
  return res.data;
}

export async function logout(): Promise<void> {
  await request("/auth/logout", { method: "POST" });
}

export async function getMe(): Promise<User> {
  const res = await request<{ user: User }>("/auth/me");
  return res.data.user;
}

// ── Permits ───────────────────────────────────────────────────────────────────

export async function fetchPermits(userId: number): Promise<PermitApplication[]> {
  const res = await request<{ user_id: number; permits: PermitApplication[]; total: number }>(
    `/permits/${userId}`,
  );
  return res.data.permits;
}

export async function applyPermit(payload: PermitApplyPayload): Promise<{
  id: number;
  application_number: string;
  status: string;
  submitted_at: string;
}> {
  const res = await request<{
    id: number;
    application_number: string;
    status: string;
    submitted_at: string;
  }>("/permits/apply", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return res.data;
}

// ── AI Interactions ───────────────────────────────────────────────────────────

export async function fetchAiLogs(): Promise<AiInteraction[]> {
  const res = await request<{ interactions: AiInteraction[]; total: number }>("/ai-logs");
  return res.data.interactions;
}

// ── KBLI search ───────────────────────────────────────────────────────────────

export async function searchKbli(q: string): Promise<KbliOption[]> {
  const res = await request<KbliOption[]>(`/kbli?q=${encodeURIComponent(q)}`);
  // Backend returns { success, data: [...] } — data key holds the array
  // The generic request<T> returns body.data, which here is the array directly
  return Array.isArray(res.data) ? res.data : [];
}

// ── Profile ───────────────────────────────────────────────────────────────────

export interface ProfileUpdatePayload {
  name?: string;
  phone?: string;
  nik?: string;
  npwp?: string;
  business_name?: string;
  business_address?: string;
}

export async function updateProfile(payload: ProfileUpdatePayload): Promise<User> {
  const res = await request<{ user: User }>("/profile", {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
  return res.data.user;
}

export async function generateTelegramToken(): Promise<string> {
  const res = await request<{ token: string }>("/profile/telegram-token", {
    method: "POST",
  });
  return res.data.token;
}

// ── Web chat (portal → Next.js API route → ai-engine) ─────────────────────────

export async function webChat(message: string, userId: number): Promise<string> {
  const res = await fetch("/api/ai/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, user_id: `web-${userId}` }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { message?: string }).message ?? "Gagal menghubungi asisten AI");
  }
  const data = (await res.json()) as { response: string };
  return data.response;
}

// ── SWR fetcher helper ────────────────────────────────────────────────────────

export function swrFetcher<T>(path: string): () => Promise<T> {
  return () => request<T>(path).then((r) => r.data);
}

export { ApiError };
