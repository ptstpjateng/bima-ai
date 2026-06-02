import {
  CheckCircle2,
  Clock,
  XCircle,
  Loader2,
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * StatusBadge — a small pill that encodes a license/application status with
 * BOTH color AND an icon + text label, so it never relies on color alone
 * (WCAG 1.4.1). Shared by /track and /dashboard.
 *
 * Contrast strategy: the badge label is always `text-ink` on a faint
 * color-mixed tint of the status token, with a same-token border + icon.
 * This keeps the AA contrast promise in BOTH light and dark themes —
 * colored *text* on a colored tint can dip below 4.5:1, but ink-on-tint does
 * not. The status token carries meaning through the icon + border + tint.
 */

type Tone = "success" | "warning" | "error" | "info" | "neutral";

type ToneStyle = {
  icon: LucideIcon;
  /** Tailwind classes: tinted bg + token border + token-colored icon. */
  wrap: string;
  iconClass: string;
  spin?: boolean;
};

const TONES: Record<Tone, ToneStyle> = {
  success: {
    icon: CheckCircle2,
    wrap: "border-success/40 bg-success/10",
    iconClass: "text-success",
  },
  warning: {
    icon: Clock,
    wrap: "border-warning/40 bg-warning/10",
    iconClass: "text-warning",
  },
  error: {
    icon: XCircle,
    wrap: "border-error/40 bg-error/10",
    iconClass: "text-error",
  },
  info: {
    icon: Loader2,
    wrap: "border-info/40 bg-info/10",
    iconClass: "text-info",
    spin: false,
  },
  neutral: {
    icon: Clock,
    wrap: "border-border bg-surface-2",
    iconClass: "text-ink-3",
  },
};

/** Maps a raw status string (from SIAP/track or the dashboard) to a tone + label. */
export function statusTone(raw: string): { tone: Tone; label: string } {
  const s = raw.toLowerCase().trim();
  if (
    /(selesai|terbit|approved|aktif|disetujui|done)/.test(s) &&
    !/akan/.test(s)
  ) {
    return { tone: "success", label: titleCase(raw) };
  }
  if (/(akan_kedaluwarsa|akan kedaluwarsa|segera|expiring)/.test(s)) {
    return { tone: "warning", label: "Akan kedaluwarsa" };
  }
  if (/(kedaluwarsa|expired)/.test(s)) {
    return { tone: "error", label: "Kedaluwarsa" };
  }
  if (/(tolak|ditolak|rejected|gagal)/.test(s)) {
    return { tone: "error", label: titleCase(raw) };
  }
  if (/(proses|diproses|berjalan|in.?progress|menunggu|review|verifikasi)/.test(s)) {
    return { tone: "info", label: titleCase(raw) };
  }
  return { tone: "neutral", label: titleCase(raw) };
}

function titleCase(s: string): string {
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function StatusBadge({
  status,
  className,
}: {
  /** Raw status string; mapped to a tone + Indonesian label. */
  status: string;
  className?: string;
}) {
  const { tone, label } = statusTone(status);
  const style = TONES[tone];
  const Icon = style.icon;

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-pill border px-2.5 py-1 text-xs font-medium text-ink",
        style.wrap,
        className,
      )}
    >
      <Icon
        className={cn("size-3.5 shrink-0", style.iconClass)}
        aria-hidden="true"
      />
      <span>{label}</span>
    </span>
  );
}
