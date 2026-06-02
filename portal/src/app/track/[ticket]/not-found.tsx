import Link from "next/link";
import { ArrowLeft } from "lucide-react";

/**
 * Branded 404 for /track/<ticket> when the [ticket] segment fails the
 * digit-shape regex in page.tsx (non-digits, too-short / too-long).
 *
 * Without this file, calling `notFound()` from the page falls through to the
 * framework default 404 — off-brand and English. Kept shape-consistent with
 * the in-page NotFoundCard. Migrated to the Bima token system.
 */
export default function TrackNotFound() {
  return (
    <main className="min-h-screen px-4 py-10 sm:px-6 sm:py-12 lg:px-8">
      <div className="mx-auto flex w-full max-w-2xl flex-col gap-6">
        <Link
          href="/"
          className="inline-flex items-center gap-2 text-sm text-ink-2 transition-colors hover:text-ink"
        >
          <ArrowLeft className="size-4" aria-hidden="true" />
          <span>Kembali ke beranda BIMA</span>
        </Link>

        <header className="mt-2">
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-brand-green-text">
            Lacak Permohonan Izin
          </p>
          <h1 className="mt-3 font-display text-2xl font-bold leading-tight tracking-tight text-ink sm:text-3xl md:text-4xl">
            Format tiket tidak dikenali
          </h1>
        </header>

        <section className="rounded-card border border-border bg-surface p-6 sm:p-8">
          <p className="text-ink-2">
            Nomor tiket BIMA biasanya{" "}
            <span className="text-ink">9 digit angka</span> yang tercetak pada
            tanda terima saat Anda mendaftar permohonan. Coba buka tautan dari
            WhatsApp BIMA atau salin nomor tiket persis seperti yang tertera.
          </p>
          <p className="mt-4 text-sm text-ink-2">
            Belum punya tiket? Tanyakan langsung ke BIMA via WhatsApp dengan
            mengetik <span className="font-mono text-ink">status izin</span>{" "}
            diikuti nomor permohonan Anda.
          </p>
        </section>
      </div>
    </main>
  );
}
