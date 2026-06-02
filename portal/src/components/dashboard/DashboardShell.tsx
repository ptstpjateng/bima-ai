import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { LogoutButton } from "@/components/me/LogoutButton";
import { MockDataBanner } from "@/components/dashboard/MockDataBanner";
import { DASHBOARD_MOCK } from "@/lib/citizen-dashboard-client";

/**
 * DashboardShell — shared chrome for every /dashboard surface. Server
 * component (LogoutButton + MockDataBanner are the only client islands).
 *
 * max-w-5xl column, an eyebrow + H1 header with a back-link and LogoutButton,
 * and the persistent "Data contoh" banner (only while DASHBOARD_MOCK).
 */
export function DashboardShell({
  eyebrow = "Dashboard Izin",
  title,
  lede,
  backHref,
  backLabel,
  children,
}: {
  eyebrow?: string;
  title: string;
  lede?: string;
  /** When set, a back-link is shown instead of the home link. */
  backHref?: string;
  backLabel?: string;
  children: React.ReactNode;
}) {
  return (
    <main className="min-h-screen px-4 py-8 sm:px-6 sm:py-10 lg:px-8">
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-6">
        <div className="flex items-center justify-between gap-4">
          <Link
            href={backHref ?? "/"}
            className="inline-flex items-center gap-2 text-sm text-ink-2 transition-colors hover:text-ink"
          >
            <ArrowLeft className="size-4" aria-hidden="true" />
            <span>{backLabel ?? "Beranda"}</span>
          </Link>
          <LogoutButton />
        </div>

        {DASHBOARD_MOCK && <MockDataBanner />}

        <header>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-brand-green-text">
            {eyebrow}
          </p>
          <h1 className="mt-2 font-display text-2xl font-bold leading-tight tracking-tight text-ink sm:text-3xl">
            {title}
          </h1>
          {lede && <p className="mt-2 max-w-prose text-sm text-ink-2 sm:text-base">{lede}</p>}
        </header>

        {children}
      </div>
    </main>
  );
}
