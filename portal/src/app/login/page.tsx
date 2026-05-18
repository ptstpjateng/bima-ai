"use client";

import { useState, type FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { loginByNik } from "@/lib/auth-client";

/**
 * Citizen login page.
 *
 * Single field (NIK), single button, single error region. NIK-only because
 * SIAP doesn't expose a password verification endpoint we can call yet —
 * see `BIMA Vision.md` open items for the SMS-OTP follow-up.
 *
 * Client Component because we need form state + redirect-on-success. The
 * brand wrapper around it is the same dark surface used by the landing
 * page, so it feels native to the portal even though it's a leaf route.
 *
 * Honors a `?next=` query param so middleware redirects (e.g. from /me)
 * land back where they came from after login.
 */
export default function LoginPage() {
  const router = useRouter();
  const search = useSearchParams();
  const next = search.get("next") || "/me";

  const [nik, setNik] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setPending(true);

    const result = await loginByNik(nik);

    if (!result.ok) {
      setError(result.message);
      setPending(false);
      return;
    }

    // Successful login — the cookie is already set server-side. We do a
    // hard navigation so any Server Components on /me re-render with the
    // new cookie. router.push alone would keep the stale tree.
    router.replace(next);
    router.refresh();
  }

  return (
    <main className="flex min-h-screen flex-col px-4 py-10 sm:px-6 sm:py-12 lg:px-8">
      <div className="mx-auto flex w-full max-w-md flex-1 flex-col">
        <Link
          href="/"
          className="inline-flex items-center gap-2 text-sm text-text-secondary transition hover:text-brand-amber"
        >
          <ArrowLeft className="size-4" aria-hidden="true" />
          <span>Kembali ke beranda</span>
        </Link>

        <header className="mt-8">
          <p className="text-xs font-medium uppercase tracking-[0.18em] text-brand-amber">
            Masuk Akun
          </p>
          <h1 className="mt-3 font-display text-2xl font-semibold leading-tight tracking-tight text-text-primary sm:text-3xl md:text-4xl">
            Login dengan akun SIAP Jateng Anda
          </h1>
          <p className="mt-3 text-sm text-text-secondary sm:text-base">
            Lihat status izin Anda dan riwayat permohonan langsung di BIMA.
          </p>
        </header>

        <form
          onSubmit={handleSubmit}
          noValidate
          className="mt-8 flex flex-col gap-5 rounded-card bg-surface-card p-6 sm:p-8"
        >
          <div className="flex flex-col gap-2">
            <label
              htmlFor="nik"
              className="text-xs font-medium uppercase tracking-wide text-text-secondary"
            >
              Nomor Induk Kependudukan (NIK)
            </label>
            <input
              id="nik"
              name="nik"
              type="text"
              inputMode="numeric"
              autoComplete="off"
              autoFocus
              required
              minLength={16}
              maxLength={19}
              placeholder="Masukkan 16 digit NIK"
              value={nik}
              onChange={(e) => setNik(e.target.value)}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? "nik-error" : undefined}
              className="w-full rounded-pill bg-surface-base px-5 py-3 font-mono text-base tracking-wide text-text-primary outline-none ring-1 ring-white/10 transition placeholder:font-sans placeholder:text-text-muted focus:ring-2 focus:ring-brand-navy"
            />
          </div>

          {error && (
            <p
              id="nik-error"
              role="alert"
              className="rounded-card bg-brand-rose/10 px-4 py-3 text-sm text-text-primary"
            >
              {error}
            </p>
          )}

          <Button
            type="submit"
            variant="primary"
            size="lg"
            disabled={pending || nik.replace(/\D/g, "").length !== 16}
          >
            {pending ? "Memverifikasi…" : "Lanjutkan"}
          </Button>
        </form>

        <div className="mt-6 flex items-start gap-3 rounded-card bg-surface-card/60 p-4 text-xs text-text-secondary sm:text-sm">
          <ShieldCheck
            className="mt-0.5 size-4 shrink-0 text-brand-emerald"
            aria-hidden="true"
          />
          <p>
            Belum punya akun SIAP Jateng? Daftar dulu di{" "}
            <a
              href="https://perizinan.jatengprov.go.id"
              target="_blank"
              rel="noopener noreferrer"
              className="text-brand-amber underline-offset-2 hover:underline"
            >
              perizinan.jatengprov.go.id
            </a>
            . Setelah punya akun, NIK Anda otomatis bisa dipakai login di sini.
          </p>
        </div>
      </div>
    </main>
  );
}
