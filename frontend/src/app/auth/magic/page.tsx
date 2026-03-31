"use client";

import { AlertCircle, CheckCircle2, Loader2, Shield } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { useAuth } from "@/context/AuthContext";
import { redeemMagicLink } from "@/lib/api";

function MagicAuthContent() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const { signIn } = useAuth();
  const token = searchParams.get("token");
  const hasRun = useRef(false);

  const [status, setStatus] = useState<"loading" | "success" | "error">("loading");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    if (hasRun.current) return;
    hasRun.current = true;

    if (!token) {
      setStatus("error");
      setErrorMsg("Link tidak valid. Tidak ada token yang ditemukan.");
      return;
    }

    redeemMagicLink(token)
      .then(({ token: apiToken, user }) => {
        signIn(apiToken, user);
        setStatus("success");
        setTimeout(() => router.replace("/dashboard"), 1500);
      })
      .catch((err) => {
        setStatus("error");
        setErrorMsg(
          err instanceof Error
            ? err.message
            : "Link tidak valid atau sudah kadaluarsa.",
        );
      });
  }, [token, signIn, router]);

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-gradient-to-br from-brand-950 via-brand-900 to-brand-800 px-4">
      <div className="w-full max-w-sm text-center">
        {/* Logo */}
        <div className="mb-8">
          <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-white/10">
            <Shield className="h-8 w-8 text-white" />
          </div>
          <h1 className="text-2xl font-bold text-white">BIMA-AI</h1>
        </div>

        {/* Status card */}
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
                Sedang memproses link masuk Anda
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
                Link Tidak Valid
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
          Link masuk otomatis dari WhatsApp BIMA-AI
        </p>
      </div>
    </div>
  );
}

export default function MagicAuthPage() {
  return (
    <Suspense fallback={
      <div className="flex min-h-screen items-center justify-center bg-brand-900">
        <Loader2 className="h-8 w-8 animate-spin text-white" />
      </div>
    }>
      <MagicAuthContent />
    </Suspense>
  );
}
