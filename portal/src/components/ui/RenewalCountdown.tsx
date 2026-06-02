import { CalendarClock, CalendarCheck2, CalendarX2 } from "lucide-react";

import { cn } from "@/lib/utils";
import { daysUntil } from "@/lib/format";

/**
 * RenewalCountdown — a pill expressing how long until a license expires,
 * with a token chosen by threshold:
 *   > 60 days   → success ("masih aman")
 *   14–60 days  → warning ("akan kedaluwarsa")
 *   < 14 days   → error
 *   expired     → error ("kedaluwarsa")
 *
 * Like StatusBadge, meaning is carried by icon + text, never color alone.
 * Label is text-ink on a token tint for AA-safe contrast in both themes.
 * The number does NOT animate — this is information, not spectacle.
 */
export function RenewalCountdown({
  expiresAt,
  className,
}: {
  expiresAt: string | null;
  className?: string;
}) {
  const days = daysUntil(expiresAt);

  if (days === null) {
    return (
      <span
        className={cn(
          "inline-flex items-center gap-1.5 rounded-pill border border-border bg-surface-2 px-2.5 py-1 text-xs font-medium text-ink-2",
          className,
        )}
      >
        <CalendarCheck2 className="size-3.5 text-ink-3" aria-hidden="true" />
        Tanpa masa berlaku
      </span>
    );
  }

  let tone: "success" | "warning" | "error";
  let Icon = CalendarClock;
  let label: string;

  if (days < 0) {
    tone = "error";
    Icon = CalendarX2;
    label = `Kedaluwarsa ${Math.abs(days)} hari lalu`;
  } else if (days < 14) {
    tone = "error";
    Icon = CalendarX2;
    label = days === 0 ? "Kedaluwarsa hari ini" : `Kedaluwarsa dalam ${days} hari`;
  } else if (days <= 60) {
    tone = "warning";
    Icon = CalendarClock;
    label = `Kedaluwarsa dalam ${days} hari`;
  } else {
    tone = "success";
    Icon = CalendarCheck2;
    label = `Berlaku ${days} hari lagi`;
  }

  const toneWrap = {
    success: "border-success/40 bg-success/10",
    warning: "border-warning/40 bg-warning/10",
    error: "border-error/40 bg-error/10",
  }[tone];
  const toneIcon = {
    success: "text-success",
    warning: "text-warning",
    error: "text-error",
  }[tone];

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-pill border px-2.5 py-1 text-xs font-medium text-ink",
        toneWrap,
        className,
      )}
    >
      <Icon className={cn("size-3.5 shrink-0", toneIcon)} aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}
