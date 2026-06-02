"use client";

import { Suspense, useState, type FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { ShieldCheck, ArrowRight } from "lucide-react";
import { motion, useReducedMotion } from "framer-motion";

import { Button } from "@/components/ui/button";
import { AuthLayout, AuthCard } from "@/components/auth/AuthLayout";
import { loginByIdentity } from "@/lib/auth-client";

/**
 * Canonical citizen login — REAL, shippable, identity-lookup path.
 *
 * Single field (NIK 16 / NPWP 15, auto-detected by length), single button,
 * single error region. No password: SIAP exposes no password-verify endpoint
 * to the portal, so we look the identity up via admin-api /sso/login (which
 * sets the httpOnly cookie server-side). See lib/auth-flow.ts for why the
 * passwordless/magic-link flows are scaffold-only.
 *
 * This component is mounted by BOTH /login and /auth/login (the latter
 * re-exports it so the new auth shell is cohesive) — single source of truth,
 * no duplicated logic.
 *
 * Next 16 requires useSearchParams() consumers inside <Suspense>.
 */
export function LoginForm() {
  return (
    <Suspense fallback={<LoginFormFallback />}>
      <LoginFormInner />
    </Suspense>
  );
}

function LoginFormInner() {
  const reduce = useReducedMotion();
  const router = useRouter();
  const search = useSearchParams();
  const next = search.get("next") || "/dashboard";

  const [identityNumber, setIdentityNumber] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const cleanedDigits = identityNumber.replace(/\D/g, "");
  const canSubmit = cleanedDigits.length === 15 || cleanedDigits.length === 16;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setPending(true);

    const result = await loginByIdentity(identityNumber);

    if (!result.ok) {
      setError(result.message);
      setPending(false);
      return;
    }

    // Cookie is set server-side. Hard navigation so Server Components on the
    // destination re-render with the fresh cookie.
    router.replace(next);
    router.refresh();
  }

  return (
    <AuthLayout
      eyebrow="Masuk Akun"
      title="Login dengan akun SIAP Jateng Anda"
      lede="Lihat status izin, SK yang terbit, dan riwayat permohonan Anda di BIMA."
    >
      <motion.div
        initial={reduce ? false : { opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: reduce ? 0 : 0.2, ease: "easeOut" }}
      >
        <AuthCard>
          <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-5">
            <div className="flex flex-col gap-2">
              <label
                htmlFor="identity-number"
                className="text-xs font-medium uppercase tracking-wide text-ink-2"
              >
                NIK atau NPWP
              </label>
              <input
                id="identity-number"
                name="identity_number"
                type="text"
                inputMode="numeric"
                autoComplete="off"
                autoFocus
                required
                minLength={15}
                maxLength={20}
                placeholder="16 digit NIK atau 15 digit NPWP"
                value={identityNumber}
                onChange={(e) => setIdentityNumber(e.target.value)}
                aria-invalid={error ? true : undefined}
                aria-describedby={error ? "identity-error" : "identity-hint"}
                aria-busy={pending || undefined}
                className="w-full rounded-pill border border-border bg-surface-2 px-5 py-3 font-mono text-base tracking-wide text-ink outline-none transition-shadow duration-150 placeholder:font-sans placeholder:text-ink-3 focus:border-accent-ink focus:ring-2 focus:ring-accent-ink/40"
              />
              <p id="identity-hint" className="text-xs text-ink-3">
                NIK untuk akun pribadi (16 digit), NPWP untuk akun badan usaha
                (15 digit).
              </p>
            </div>

            {error && (
              <p
                id="identity-error"
                role="alert"
                className="rounded-card border border-error/40 bg-error/8 px-4 py-3 text-sm text-ink"
              >
                {error}
              </p>
            )}

            <Button
              type="submit"
              variant="primary"
              size="lg"
              disabled={pending || !canSubmit}
            >
              <span>{pending ? "Memverifikasi…" : "Lanjutkan"}</span>
              {!pending && <ArrowRight aria-hidden="true" />}
            </Button>
          </form>
        </AuthCard>
      </motion.div>

      <div className="mt-6 flex items-start gap-3 rounded-card border border-border bg-surface-2 p-4 text-xs text-ink-2 sm:text-sm">
        <ShieldCheck
          className="mt-0.5 size-4 shrink-0 text-success"
          aria-hidden="true"
        />
        <p>
          Belum punya akun SIAP Jateng? Daftar dulu di{" "}
          <a
            href="https://perizinan.jatengprov.go.id"
            target="_blank"
            rel="noopener noreferrer"
            className="text-accent-ink underline-offset-2 hover:underline"
          >
            perizinan.jatengprov.go.id
          </a>
          . Setelah punya akun, NIK atau NPWP Anda otomatis bisa dipakai login
          di sini.
        </p>
      </div>

      <p className="mt-6 text-center text-sm text-ink-3">
        Belum punya akun?{" "}
        <Link
          href="/auth/signup"
          className="font-medium text-accent-ink underline-offset-4 hover:underline"
        >
          Buat akun
        </Link>
      </p>
    </AuthLayout>
  );
}

/** Pre-mount skeleton matching the form shell so there's no layout flash. */
export function LoginFormFallback() {
  return (
    <main className="flex min-h-screen flex-col px-4 py-10 sm:px-6 sm:py-16 lg:px-8">
      <div className="mx-auto flex w-full max-w-md flex-1 flex-col">
        <div className="h-4 w-40 animate-pulse rounded-pill bg-surface-2" aria-hidden="true" />
        <div className="mt-8 h-10 w-3/4 animate-pulse rounded-card bg-surface-2" aria-hidden="true" />
        <div className="mt-8 h-56 animate-pulse rounded-card bg-surface-2" aria-hidden="true" />
        <span role="status" aria-live="polite" className="sr-only">
          Memuat formulir masuk…
        </span>
      </div>
    </main>
  );
}
