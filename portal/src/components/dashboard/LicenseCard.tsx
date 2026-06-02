import Link from "next/link";
import { ChevronRight, Hash } from "lucide-react";

import { StatusBadge } from "@/components/ui/StatusBadge";
import { RenewalCountdown } from "@/components/ui/RenewalCountdown";
import { formatDateID } from "@/lib/format";
import type { CitizenLicense } from "@/lib/citizen-dashboard-client";

/**
 * LicenseCard — one issued/processing license. Title, sector, KBLI code,
 * StatusBadge, RenewalCountdown, issued/expiry dates, and a "Lihat detail"
 * link to /dashboard/[id]. Server-safe; the stagger animation is applied by
 * the client <MotionGrid> wrapper around it.
 */
export function LicenseCard({ license }: { license: CitizenLicense }) {
  return (
    <Link
      href={`/dashboard/${license.id}`}
      className="group flex h-full flex-col rounded-card border border-border bg-surface p-5 transition-colors duration-200 hover:border-border-strong hover:bg-surface-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent-ink sm:p-6"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={license.status} />
          {license.status !== "proses" && (
            <RenewalCountdown expiresAt={license.expires_at} />
          )}
        </div>
        <ChevronRight
          className="size-5 shrink-0 text-ink-3 transition-transform duration-200 group-hover:translate-x-0.5 group-hover:text-ink-2"
          aria-hidden="true"
        />
      </div>

      <h3 className="mt-4 font-display text-lg font-semibold leading-snug text-ink">
        {license.license_name}
      </h3>
      <p className="mt-1 text-sm text-ink-2">{license.sector}</p>

      <dl className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <div className="inline-flex items-center gap-1.5 text-ink-2">
          <Hash className="size-3.5 text-ink-3" aria-hidden="true" />
          <dt className="sr-only">Kode KBLI</dt>
          <dd className="font-mono">{license.kbli_code}</dd>
        </div>
        <div>
          <dt className="text-xs uppercase tracking-wide text-ink-3">Terbit</dt>
          <dd className="text-ink">{formatDateID(license.issued_at)}</dd>
        </div>
        <div>
          <dt className="text-xs uppercase tracking-wide text-ink-3">
            Kedaluwarsa
          </dt>
          <dd className="text-ink">{formatDateID(license.expires_at)}</dd>
        </div>
      </dl>

      <span className="mt-4 inline-flex items-center gap-1 text-sm font-medium text-accent-ink">
        Lihat detail
        <ChevronRight className="size-4" aria-hidden="true" />
      </span>
    </Link>
  );
}
