"use client";

import { Eye, EyeOff, Loader2, Lock, Mail, Shield } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useAuth } from "@/context/AuthContext";
import { login } from "@/lib/api";

export default function LoginPage() {
  const { signIn, isAuthenticated } = useAuth();
  const router = useRouter();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPass, setShowPass] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (isAuthenticated) {
    router.replace("/dashboard");
    return null;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setIsLoading(true);
    setError(null);
    try {
      const { token, user } = await login(email, password);
      signIn(token, user);
      router.replace("/dashboard");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Terjadi kesalahan. Coba lagi.");
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-gradient-to-br from-brand-950 via-brand-900 to-brand-800 px-4">
      {/* Background decoration */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -left-32 -top-32 h-96 w-96 rounded-full bg-brand-700/30 blur-3xl" />
        <div className="absolute -bottom-32 -right-32 h-96 w-96 rounded-full bg-brand-500/20 blur-3xl" />
      </div>

      <div className="relative w-full max-w-sm animate-fade-in">
        {/* Logo */}
        <div className="mb-8 text-center">
          <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-white/10 backdrop-blur-sm">
            <Shield className="h-8 w-8 text-white" />
          </div>
          <h1 className="text-2xl font-bold text-white">BIMA-AI</h1>
          <p className="mt-1 text-sm text-brand-200">
            Portal Perizinan UMKM — DPMPTSP
          </p>
        </div>

        {/* Card */}
        <div className="rounded-2xl bg-white p-6 shadow-2xl">
          <h2 className="mb-1 text-lg font-bold text-gray-900">Masuk ke Akun</h2>
          <p className="mb-6 text-sm text-gray-500">
            Gunakan email dan kata sandi yang diberikan oleh DPMPTSP
          </p>

          <form onSubmit={handleSubmit} className="space-y-4">
            {/* Email */}
            <div>
              <label
                htmlFor="email"
                className="mb-1.5 block text-sm font-medium text-gray-700"
              >
                Email
              </label>
              <div className="relative">
                <Mail className="absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
                <input
                  id="email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  autoComplete="email"
                  placeholder="contoh@email.com"
                  className="w-full rounded-xl border border-gray-200 py-2.5 pl-10 pr-4 text-sm outline-none transition placeholder:text-gray-400 focus:border-brand-400 focus:ring-2 focus:ring-brand-100"
                />
              </div>
            </div>

            {/* Password */}
            <div>
              <label
                htmlFor="password"
                className="mb-1.5 block text-sm font-medium text-gray-700"
              >
                Kata Sandi
              </label>
              <div className="relative">
                <Lock className="absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
                <input
                  id="password"
                  type={showPass ? "text" : "password"}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  autoComplete="current-password"
                  placeholder="Kata sandi Anda"
                  className="w-full rounded-xl border border-gray-200 py-2.5 pl-10 pr-11 text-sm outline-none transition placeholder:text-gray-400 focus:border-brand-400 focus:ring-2 focus:ring-brand-100"
                />
                <button
                  type="button"
                  onClick={() => setShowPass((v) => !v)}
                  className="absolute right-3.5 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                >
                  {showPass ? (
                    <EyeOff className="h-4 w-4" />
                  ) : (
                    <Eye className="h-4 w-4" />
                  )}
                </button>
              </div>
            </div>

            {/* Error */}
            {error && (
              <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
                {error}
              </div>
            )}

            {/* Submit */}
            <button
              type="submit"
              disabled={isLoading}
              className="flex w-full items-center justify-center gap-2 rounded-xl bg-brand-700 py-3 text-sm font-semibold text-white transition hover:bg-brand-800 disabled:opacity-70"
            >
              {isLoading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : null}
              {isLoading ? "Memproses..." : "Masuk"}
            </button>
          </form>
        </div>

        {/* WhatsApp hint */}
        <div className="mt-4 rounded-2xl bg-white/10 px-4 py-3 text-center backdrop-blur-sm">
          <p className="text-xs text-brand-100">
            Baru bergabung? Mulai dari WhatsApp dengan menghubungi asisten{" "}
            <span className="font-semibold text-white">BIMA-AI</span> dan Anda akan
            mendapat link masuk otomatis.
          </p>
        </div>
      </div>
    </div>
  );
}
