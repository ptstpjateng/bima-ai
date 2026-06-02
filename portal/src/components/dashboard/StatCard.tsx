import { type LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * StatCard — a summary tile: big display-font number + label + icon chip.
 * Server-safe. `tone` tints the icon chip to match the metric's semantics
 * (e.g. expiring = warning) without relying on color alone for meaning (the
 * label carries it).
 */
export function StatCard({
  icon: Icon,
  value,
  label,
  tone = "neutral",
}: {
  icon: LucideIcon;
  value: number | string;
  label: string;
  tone?: "neutral" | "success" | "warning" | "info";
}) {
  const chip = {
    neutral: "bg-surface-2 text-primary",
    success: "bg-success/12 text-success",
    warning: "bg-warning/12 text-warning",
    info: "bg-info/12 text-info",
  }[tone];

  return (
    <div className="rounded-card border border-border bg-surface p-5 sm:p-6">
      <span
        className={cn(
          "inline-flex size-10 items-center justify-center rounded-pill",
          chip,
        )}
      >
        <Icon className="size-5" aria-hidden="true" />
      </span>
      <p className="mt-4 font-display text-3xl font-bold leading-none text-ink">
        {value}
      </p>
      <p className="mt-2 text-sm text-ink-2">{label}</p>
    </div>
  );
}
