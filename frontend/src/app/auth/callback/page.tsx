"use client";

import { AlertCircle, CheckCircle2, Loader2, Shield } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { useAuth } from "@/context/AuthContext";
import type { User } from "@/types";

function normalizeRole(role: string): User["role"] {
  if (role === "admin") return "admin";
  if (role === "dpmptsp_staff" || role === "staff") return "staff";
  return "msme";
}

function CallbackContent() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const { signIn } = useAuth();
  const hasRun = useRef(false);

  const [status, setStatus] = useState<"loading" | "success" | "error">("loading");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    if (hasRun.current) return;
    hasRun.current = true;

    const error = searchParams.get("error");
    if (error) {
      setStatus("error");
      setErrorMsg("Link tidak valid atau sudah kadaluarsa. Silakan minta link baru.");
      return;
    }

    const apiToken = searchParams.get("api_token");
    const userId = searchParams.get("user_id");
    const name = searchParams.get("name");
    const email = searchParams.get("email");
    const role = searchParams.get("role");

    if (!apiToken || !userId || !name || !email || !role) {
      setStatus("error");
      setErrorMsg("Parameter autentikasi tidak lengkap. Silakan coba lagi.");
      return;
    }

    const user: User = {
      id: parseInt(userId, 10),
      name,
      email,
      role: normalizeRole(role),
      phone: null,
      nik: null,
      npwp: null,
      business_name: null,
      business_legal_type: null,
      business_address: null,
      province: null,
      city: null,
      district: null,
      village: null,
      postal_code: null,
      email_verified_at: null,
      telegram_chat_id: null,
      telegram_username: null,
    };

    signIn(apiToken, user);
    setStatus("success");
    setTimeout(() => router.replace("/dashboard"), 1500);
  }, [searchParams, signIn, router]);

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-gradient-to-br from-brand-950 via-brand-900 to-brand-800 px-4">
      <div className="w-full max-w-sm text-center">
        <div className="mb-8">
          <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-white/10">
            <Shield className="h-8 w-8 text-white" />
          </div>
          <h1 className="text-2xl font-bold text-white">BIMA-AI</h1>
        </div>

        <div className="rounded-2xl bg-white p-8 shadow-2xl">
          {status === "loading" && (
            <div className="flex flex-col items-center">
              <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-brand-50">
                <Loader2 className="h-8 w-8 animate-spin text-brand-600" />
              </div>
              <h2 className="mb-2 text-lg font-bold text-gray-900">
                Memverifikasi...
              </h2>
              <p className="text-sm text-gray-500">
                Sedang memproses autentikasi Anda
              </p>
            </div>
          )}

          {status === "success" && (
            <div className="flex flex-col items-center">
              <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-green-50">
                <CheckCircle2 className="h-8 w-8 text-green-500" />
              </div>
              <h2 className="mb-2 text-lg font-bold text-gray-900">
                Berhasil Masuk!
              </h2>
              <p className="text-sm text-gray-500">
                Mengalihkan ke dasbor Anda...
              </p>
            </div>
          )}

          {status === "error" && (
            <div className="flex flex-col items-center">
              <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-red-50">
                <AlertCircle className="h-8 w-8 text-red-500" />
              </div>
              <h2 className="mb-2 text-lg font-bold text-gray-900">
                Autentikasi Gagal
              </h2>
              <p className="mb-6 text-sm text-gray-500">{errorMsg}</p>
              <button
                onClick={() => router.push("/login")}
                className="rounded-xl bg-brand-700 px-6 py-2.5 text-sm font-semibold text-white hover:bg-brand-800"
              >
                Ke Halaman Masuk
              </button>
            </div>
          )}
        </div>

        <p className="mt-4 text-xs text-brand-200">
          Link masuk otomatis dari Telegram BIMA-AI
        </p>
      </div>
    </div>
  );
}

export default function AuthCallbackPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center bg-brand-900">
          <Loader2 className="h-8 w-8 animate-spin text-white" />
        </div>
      }
    >
      <CallbackContent />
    </Suspense>
  );
}
