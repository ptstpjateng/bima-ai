import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * AuthLayout — the shared centered shell for every /auth/* surface (and the
 * canonical /login). Brand wordmark + back-link on top, an eyebrow + H1, an
 * optional slot (e.g. SoonChip / StepIndicator) and the content card.
 *
 * Server-safe (no client hooks). Mobile: full-bleed px-4. ≥sm: max-w-md card
 * centered with generous vertical rhythm. Single <h1> per page (the `title`).
 */
export function AuthLayout({
  eyebrow,
  title,
  lede,
  headerSlot,
  children,
  backHref = "/",
  backLabel = "Kembali ke beranda",
}: {
  eyebrow: string;
  title: string;
  lede?: string;
  /** Rendered between the eyebrow and the H1 (e.g. SoonChip). */
  headerSlot?: React.ReactNode;
  children: React.ReactNode;
  backHref?: string;
  backLabel?: string;
}) {
  return (
    <main className="flex min-h-screen flex-col px-4 py-10 sm:px-6 sm:py-16 lg:px-8">
      <div className="mx-auto flex w-full max-w-md flex-1 flex-col">
        <Link
          href={backHref}
          className="inline-flex items-center gap-2 text-sm text-ink-2 transition-colors hover:text-ink"
        >
          <ArrowLeft className="size-4" aria-hidden="true" />
          <span>{backLabel}</span>
        </Link>

        <header className="mt-8">
          <div className="flex flex-wrap items-center gap-3">
            <p className="text-xs font-semibold uppercase tracking-[0.18em] text-brand-green-text">
              {eyebrow}
            </p>
            {headerSlot}
          </div>
          <h1 className="mt-3 font-display text-2xl font-bold leading-tight tracking-tight text-ink sm:text-3xl">
            {title}
          </h1>
          {lede && <p className="mt-3 text-sm text-ink-2 sm:text-base">{lede}</p>}
        </header>

        {children}
      </div>
    </main>
  );
}

/** A card surface used inside AuthLayout for forms / panels. */
export function AuthCard({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "mt-8 rounded-card border border-border bg-surface p-6 sm:p-8",
        className,
      )}
    >
      {children}
    </div>
  );
}
