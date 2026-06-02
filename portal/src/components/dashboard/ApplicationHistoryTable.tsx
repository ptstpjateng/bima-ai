"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { ArrowUpDown, ChevronRight, Filter } from "lucide-react";
import { ToggleGroup } from "radix-ui";

import { StatusBadge } from "@/components/ui/StatusBadge";
import { formatDateID } from "@/lib/format";
import { cn } from "@/lib/utils";
import type {
  CitizenApplication,
  ApplicationStatus,
} from "@/lib/citizen-dashboard-client";

/**
 * ApplicationHistoryTable — status-filterable, date-sortable list of every
 * application. A real semantic <table> on ≥md (with <th scope> + sr-only
 * caption); a stacked card list on mobile that preserves label/value pairs.
 *
 * Filter chips use radix ToggleGroup (roving tabindex, keyboard-operable).
 * Sort toggles the submitted-date order. All meaning is text + icon, never
 * color alone.
 */
type Filter = "all" | ApplicationStatus;

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "Semua" },
  { value: "proses", label: "Berjalan" },
  { value: "selesai", label: "Selesai" },
  { value: "ditolak", label: "Ditolak" },
];

export function ApplicationHistoryTable({
  applications,
}: {
  applications: CitizenApplication[];
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [sortDesc, setSortDesc] = useState(true);

  const rows = useMemo(() => {
    const filtered =
      filter === "all"
        ? applications
        : applications.filter((a) => a.status === filter);
    return [...filtered].sort((a, b) => {
      const cmp =
        new Date(a.submitted_at).getTime() - new Date(b.submitted_at).getTime();
      return sortDesc ? -cmp : cmp;
    });
  }, [applications, filter, sortDesc]);

  return (
    <div>
      {/* Filter chips */}
      <div className="flex items-center gap-3">
        <span className="inline-flex items-center gap-1.5 text-sm text-ink-2">
          <Filter className="size-4 text-ink-3" aria-hidden="true" />
          Filter
        </span>
        <ToggleGroup.Root
          type="single"
          value={filter}
          onValueChange={(v) => v && setFilter(v as Filter)}
          aria-label="Filter status permohonan"
          className="flex flex-wrap gap-2"
        >
          {FILTERS.map((f) => (
            <ToggleGroup.Item
              key={f.value}
              value={f.value}
              className={cn(
                "cursor-pointer rounded-pill border px-3 py-1.5 text-sm font-medium transition-colors duration-150 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent-ink",
                "border-border bg-surface text-ink-2 hover:bg-surface-hover",
                "data-[state=on]:border-brand-green data-[state=on]:bg-brand-green/10 data-[state=on]:text-ink",
              )}
            >
              {f.label}
            </ToggleGroup.Item>
          ))}
        </ToggleGroup.Root>
      </div>

      {rows.length === 0 ? (
        <p className="mt-6 rounded-card border border-border bg-surface p-6 text-sm text-ink-2">
          Tidak ada permohonan dengan status ini.
        </p>
      ) : (
        <>
          {/* Desktop table */}
          <div className="mt-5 hidden overflow-hidden rounded-card border border-border bg-surface md:block">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">
                Riwayat permohonan izin, dapat difilter berdasarkan status dan
                diurutkan berdasarkan tanggal.
              </caption>
              <thead>
                <tr className="border-b border-border text-xs uppercase tracking-wide text-ink-3">
                  <th scope="col" className="px-5 py-3 font-medium">
                    Permohonan
                  </th>
                  <th scope="col" className="px-5 py-3 font-medium">
                    Tiket
                  </th>
                  <th scope="col" className="px-5 py-3 font-medium">
                    Status
                  </th>
                  <th scope="col" className="px-5 py-3 font-medium">
                    <button
                      type="button"
                      onClick={() => setSortDesc((s) => !s)}
                      className="inline-flex cursor-pointer items-center gap-1 uppercase tracking-wide transition-colors hover:text-ink"
                      aria-label={`Urutkan tanggal ${sortDesc ? "terlama dulu" : "terbaru dulu"}`}
                    >
                      Tanggal
                      <ArrowUpDown className="size-3.5" aria-hidden="true" />
                    </button>
                  </th>
                  <th scope="col" className="px-5 py-3">
                    <span className="sr-only">Aksi</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {rows.map((app) => (
                  <tr key={app.id} className="transition-colors hover:bg-surface-hover">
                    <th scope="row" className="px-5 py-4 font-medium text-ink">
                      {app.license_name}
                      <span className="block text-xs font-normal text-ink-3">
                        {app.sector}
                      </span>
                    </th>
                    <td className="px-5 py-4 font-mono text-xs text-ink-2">
                      {app.ticket}
                    </td>
                    <td className="px-5 py-4">
                      <StatusBadge status={app.status} />
                    </td>
                    <td className="px-5 py-4 text-ink-2">
                      {formatDateID(app.submitted_at)}
                    </td>
                    <td className="px-5 py-4 text-right">
                      <Link
                        href={`/track/${app.ticket}`}
                        className="inline-flex items-center gap-1 text-sm font-medium text-accent-ink underline-offset-4 hover:underline"
                      >
                        Lacak
                        <ChevronRight className="size-4" aria-hidden="true" />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Mobile cards */}
          <ul className="mt-5 space-y-3 md:hidden">
            {rows.map((app) => (
              <li
                key={app.id}
                className="rounded-card border border-border bg-surface p-4"
              >
                <div className="flex items-start justify-between gap-3">
                  <p className="font-medium text-ink">{app.license_name}</p>
                  <StatusBadge status={app.status} />
                </div>
                <dl className="mt-3 grid grid-cols-2 gap-2 text-sm">
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-ink-3">
                      Tiket
                    </dt>
                    <dd className="font-mono text-ink-2">{app.ticket}</dd>
                  </div>
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-ink-3">
                      Tanggal
                    </dt>
                    <dd className="text-ink-2">
                      {formatDateID(app.submitted_at)}
                    </dd>
                  </div>
                </dl>
                <Link
                  href={`/track/${app.ticket}`}
                  className="mt-3 inline-flex items-center gap-1 text-sm font-medium text-accent-ink underline-offset-4 hover:underline"
                >
                  Lacak permohonan
                  <ChevronRight className="size-4" aria-hidden="true" />
                </Link>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
