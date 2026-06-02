import type { Metadata } from "next";
import { redirect, notFound } from "next/navigation";
import { DownloadCloud, Building2, ShieldAlert } from "lucide-react";

import { getServerUser } from "@/lib/auth-server";
import {
  getCitizenLicense,
  getCitizenRenewals,
} from "@/lib/citizen-dashboard-client";
import { DashboardShell } from "@/components/dashboard/DashboardShell";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { RenewalPanel } from "@/components/dashboard/RenewalPanel";
import { LicenseTimeline } from "@/components/dashboard/LicenseTimeline";
import { LifecycleExplainer } from "@/components/dashboard/LifecycleExplainer";
import { Button } from "@/components/ui/button";
import { formatDateID } from "@/lib/format";

/**
 * /dashboard/[licenseId] — license detail. Header + status, definition grid,
 * SK download, renewal panel, and the lifecycle timeline. MOCK data via the
 * typed stub (banner in DashboardShell makes that explicit).
 */
export const metadata: Metadata = {
  title: "Detail Izin · BIMA",
  robots: { index: false },
};

export default async function LicenseDetailPage({
  params,
}: {
  params: Promise<{ licenseId: string }>;
}) {
  const user = await getServerUser();
  const { licenseId } = await params;
  if (!user) redirect(`/login?next=/dashboard/${licenseId}`);

  const [license, renewals] = await Promise.all([
    getCitizenLicense(licenseId),
    getCitizenRenewals(),
  ]);

  if (!license) notFound();

  const renewal = renewals.find((r) => r.license_id === license.id) ?? null;
  const canDownload = Boolean(license.sk_document_url);

  return (
    <DashboardShell
      eyebrow="Detail Izin"
      title={license.license_name}
      backHref="/dashboard"
      backLabel="Dashboard"
    >
      {/* Header meta */}
      <section className="rounded-card border border-border bg-surface p-6 sm:p-8">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={license.status} />
        </div>
        <p className="mt-3 text-sm text-ink-2">{license.sector}</p>

        <dl className="mt-6 grid gap-x-6 gap-y-5 sm:grid-cols-2">
          <DefField label="Kode KBLI" value={license.kbli_code} mono />
          <DefField label="Tingkat risiko" value={license.risk_level} />
          <DefField label="Tanggal terbit" value={formatDateID(license.issued_at)} />
          <DefField label="Kedaluwarsa" value={formatDateID(license.expires_at)} />
          <div className="sm:col-span-2">
            <dt className="text-xs uppercase tracking-wide text-ink-3">
              Kewenangan
            </dt>
            <dd className="mt-1 inline-flex items-center gap-2 text-ink">
              <Building2 className="size-4 text-ink-3" aria-hidden="true" />
              {license.authority}
            </dd>
          </div>
        </dl>

        {/* SK download */}
        <div className="mt-8 border-t border-border pt-6">
          {canDownload ? (
            <Button asChild variant="primary" size="lg">
              <a
                href={license.sk_document_url!}
                download
                aria-label={`Unduh SK untuk ${license.license_name} (PDF)`}
              >
                <DownloadCloud aria-hidden="true" />
                <span>Unduh SK (PDF)</span>
              </a>
            </Button>
          ) : (
            <div>
              <Button variant="primary" size="lg" disabled>
                <DownloadCloud aria-hidden="true" />
                <span>Unduh SK (PDF)</span>
              </Button>
              <p className="mt-2 inline-flex items-center gap-1.5 text-sm text-ink-3">
                <ShieldAlert className="size-4" aria-hidden="true" />
                SK belum tersedia untuk diunduh.
              </p>
            </div>
          )}
        </div>
      </section>

      {/* Renewal + lifecycle */}
      <div className="grid gap-6 lg:grid-cols-2">
        <RenewalPanel renewal={renewal} expiresAt={license.expires_at} />

        <section
          aria-labelledby="lifecycle-heading"
          className="rounded-card border border-border bg-surface p-5 sm:p-6"
        >
          <h2
            id="lifecycle-heading"
            className="font-display text-base font-semibold text-ink"
          >
            Alur izin
          </h2>
          <LifecycleExplainer className="mt-4 h-auto w-full max-w-xs" />
          <div className="mt-6">
            <LicenseTimeline events={license.timeline} />
          </div>
        </section>
      </div>
    </DashboardShell>
  );
}

function DefField({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-ink-3">{label}</dt>
      <dd className={`mt-1 text-ink ${mono ? "font-mono" : ""}`}>{value}</dd>
    </div>
  );
}
