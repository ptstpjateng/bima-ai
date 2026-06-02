import type { Metadata } from "next";
import { redirect } from "next/navigation";
import Link from "next/link";
import {
  FileText,
  AlertTriangle,
  RefreshCw,
  History,
  ChevronRight,
} from "lucide-react";

import { getServerUser } from "@/lib/auth-server";
import {
  getCitizenLicenses,
  getCitizenApplications,
  getCitizenRenewals,
  type CitizenLicense,
  type CitizenApplication,
} from "@/lib/citizen-dashboard-client";
import { DashboardShell } from "@/components/dashboard/DashboardShell";
import { StatCard } from "@/components/dashboard/StatCard";
import { LicenseCard } from "@/components/dashboard/LicenseCard";
import { EmptyState } from "@/components/dashboard/EmptyState";
import { MotionItem } from "@/components/dashboard/MotionList";
import { StatusBadge } from "@/components/ui/StatusBadge";

/**
 * /dashboard — NEW post-licensing citizen hub.
 *
 * Protected (middleware + page-level getServerUser). Reads from the typed
 * citizen-dashboard-client STUB (DASHBOARD_MOCK) — clearly labelled by the
 * persistent "Data contoh" banner in DashboardShell. When SIAP's
 * post-licensing read endpoint exists, the same typed client points at
 * admin-api with NO UI change.
 */
export const metadata: Metadata = {
  title: "Dashboard Izin · BIMA",
  description: "Ringkasan izin, status, dan riwayat permohonan Anda.",
  robots: { index: false },
};

export default async function DashboardPage() {
  const user = await getServerUser();
  if (!user) redirect("/login?next=/dashboard");

  const [licenses, applications, renewals] = await Promise.all([
    getCitizenLicenses(),
    getCitizenApplications(),
    getCitizenRenewals(),
  ]);

  const firstName = user.name.split(" ")[0] ?? user.name;
  const activeCount = licenses.filter((l) => l.status === "aktif").length;
  const expiringCount = licenses.filter(
    (l) => l.status === "akan_kedaluwarsa",
  ).length;
  const inProgressCount = applications.filter(
    (a) => a.status === "proses",
  ).length;

  return (
    <DashboardShell
      title={`Halo, ${firstName}`}
      lede="Pantau izin aktif, SK yang terbit, perpanjangan, dan riwayat permohonan Anda di satu tempat."
    >
      {/* Summary stats */}
      <section aria-label="Ringkasan" className="grid gap-4 sm:grid-cols-3">
        <StatCard
          icon={FileText}
          value={activeCount}
          label="Izin aktif"
          tone="success"
        />
        <StatCard
          icon={AlertTriangle}
          value={expiringCount}
          label="Akan kedaluwarsa"
          tone="warning"
        />
        <StatCard
          icon={RefreshCw}
          value={inProgressCount}
          label="Permohonan berjalan"
          tone="info"
        />
      </section>

      {/* Licenses */}
      <section aria-labelledby="licenses-heading" className="mt-2">
        <h2
          id="licenses-heading"
          className="font-display text-lg font-semibold text-ink"
        >
          Izin Saya
        </h2>
        {licenses.length === 0 ? (
          <div className="mt-4">
            <EmptyState />
          </div>
        ) : (
          <div className="mt-4 grid gap-4 md:grid-cols-2">
            {licenses.map((license: CitizenLicense, i) => (
              <MotionItem key={license.id} index={i}>
                <LicenseCard license={license} />
              </MotionItem>
            ))}
          </div>
        )}
      </section>

      {/* Renewals nudge */}
      {renewals.some((r) => r.eligible) && (
        <section
          aria-labelledby="renewals-heading"
          className="rounded-card border border-warning/30 bg-warning/6 p-5 sm:p-6"
        >
          <div className="flex items-start gap-3">
            <RefreshCw
              className="mt-0.5 size-5 shrink-0 text-warning"
              aria-hidden="true"
            />
            <div className="min-w-0">
              <h2
                id="renewals-heading"
                className="font-display text-base font-semibold text-ink"
              >
                Perpanjangan jatuh tempo
              </h2>
              <ul className="mt-2 space-y-1.5 text-sm text-ink-2">
                {renewals
                  .filter((r) => r.eligible)
                  .map((r) => (
                    <li key={r.id}>
                      <Link
                        href={`/dashboard/${r.license_id}`}
                        className="font-medium text-accent-ink underline-offset-4 hover:underline"
                      >
                        {r.license_name}
                      </Link>{" "}
                      — segera ajukan perpanjangan.
                    </li>
                  ))}
              </ul>
            </div>
          </div>
        </section>
      )}

      {/* Applications mini-list (links to live /track tickets) */}
      <section aria-labelledby="apps-heading" className="mt-2">
        <div className="flex items-center justify-between gap-4">
          <h2
            id="apps-heading"
            className="font-display text-lg font-semibold text-ink"
          >
            Permohonan
          </h2>
          <Link
            href="/dashboard/history"
            className="inline-flex items-center gap-1 text-sm font-medium text-accent-ink underline-offset-4 hover:underline"
          >
            <History className="size-4" aria-hidden="true" />
            Lihat riwayat
          </Link>
        </div>

        <ul className="mt-4 divide-y divide-border overflow-hidden rounded-card border border-border bg-surface">
          {applications.slice(0, 3).map((app: CitizenApplication) => (
            <li key={app.id}>
              <Link
                href={`/track/${app.ticket}`}
                className="flex items-center justify-between gap-4 px-5 py-4 transition-colors hover:bg-surface-hover focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent-ink"
              >
                <div className="min-w-0">
                  <p className="truncate font-medium text-ink">
                    {app.license_name}
                  </p>
                  <p className="mt-0.5 font-mono text-xs text-ink-3">
                    Tiket {app.ticket}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  <StatusBadge status={app.status} />
                  <ChevronRight
                    className="size-4 text-ink-3"
                    aria-hidden="true"
                  />
                </div>
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </DashboardShell>
  );
}
