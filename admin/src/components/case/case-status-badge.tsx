"use client";

import { cn } from "@/lib/utils";

/**
 * Free-form SIAP status string → brand-toned pill.
 *
 * SIAP returns Indonesian status text (e.g. "Menunggu verifikasi", "Disetujui",
 * "Ditolak", "Diproses"). We bucket on keyword and fall back to a neutral pill
 * so an unknown status never crashes the render.
 */
export function CaseStatusBadge({ status }: { status: string }) {
  const v = status?.toLowerCase() ?? "";
  let cls = "bg-surface-card-hover text-text-secondary";

  if (v.includes("setuju") || v.includes("selesai") || v.includes("terbit")) {
    cls = "bg-brand-emerald/15 text-brand-emerald";
  } else if (v.includes("tolak") || v.includes("gagal")) {
    cls = "bg-brand-rose/15 text-brand-rose";
  } else if (v.includes("tunggu") || v.includes("proses") || v.includes("verif")) {
    cls = "bg-brand-amber/15 text-brand-amber";
  }

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-pill px-2.5 py-0.5 text-xs font-medium",
        cls
      )}
    >
      {status || "—"}
    </span>
  );
}

export function DemoBadge({ fixture }: { fixture: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-pill bg-brand-amber/15 text-brand-amber px-2.5 py-0.5 text-[11px] font-medium uppercase tracking-wider">
      <span
        className="size-1.5 rounded-full bg-brand-amber animate-pulse"
        aria-hidden
      />
      Demo · {fixture}
    </span>
  );
}
