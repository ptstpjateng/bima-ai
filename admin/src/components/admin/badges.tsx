"use client";

import { cn } from "@/lib/utils";
import type { Channel, MessageType } from "@/lib/types";

/**
 * Brand-aligned badges for AI interaction rows.
 *
 * All colors map back to BIMA-Vault/Brand.md tokens — no Tailwind defaults
 * (no random gray-500, no shadcn-default destructive that doesn't trace
 * back to brand-rose, etc.).
 *
 * Channel palette:
 *   whatsapp → emerald (matches WhatsApp's brand lineage; brand-emerald)
 *   telegram → navy (brand-navy — official, trusted)
 *   web      → amber (brand-amber — human, approachable)
 *   other    → muted neutral on surface-card-hover
 *
 * Message type palette:
 *   user_message → brand-navy on a tinted surface
 *   ai_response  → brand-amber on a tinted surface
 *   system_event → text-muted, no fill
 */

function basePill(extra: string) {
  return cn(
    "inline-flex items-center gap-1 rounded-pill px-2 py-0.5 text-[11px] font-medium leading-none whitespace-nowrap",
    extra
  );
}

export function ChannelBadge({ channel }: { channel: Channel }) {
  const value = channel?.toLowerCase() ?? "";
  const variants: Record<string, string> = {
    whatsapp: "bg-brand-emerald/15 text-brand-emerald",
    telegram: "bg-brand-navy/20 text-text-primary",
    web: "bg-brand-amber/15 text-brand-amber",
  };
  const cls = variants[value] ?? "bg-surface-card-hover text-text-secondary";
  return <span className={basePill(cls)}>{channel ?? "—"}</span>;
}

export function MessageTypeBadge({ type }: { type: MessageType }) {
  const value = type?.toLowerCase() ?? "";
  const variants: Record<string, string> = {
    user_message: "bg-brand-navy/20 text-text-primary",
    ai_response: "bg-brand-amber/15 text-brand-amber",
    system_event: "bg-surface-card-hover text-text-muted",
  };
  const cls = variants[value] ?? "bg-surface-card-hover text-text-secondary";
  // Friendlier labels.
  const label =
    value === "user_message"
      ? "user"
      : value === "ai_response"
        ? "ai"
        : value === "system_event"
          ? "system"
          : type ?? "—";
  return <span className={basePill(cls)}>{label}</span>;
}

export function ResponseTimeBadge({ ms }: { ms: number | null | undefined }) {
  if (ms == null) {
    return <span className="text-xs text-text-muted">—</span>;
  }
  const cls =
    ms < 2000
      ? "text-brand-emerald"
      : ms < 5000
        ? "text-brand-amber"
        : "text-brand-rose";
  return (
    <span className={cn("font-mono text-xs tabular-nums", cls)}>
      {ms.toLocaleString("id-ID")} ms
    </span>
  );
}

export function RiskBadge({ risk }: { risk: string | null | undefined }) {
  if (!risk) {
    return <span className="text-xs text-text-muted">—</span>;
  }
  const v = risk.toLowerCase();
  let cls = "bg-surface-card-hover text-text-secondary";
  if (v.includes("rendah") && !v.includes("menengah")) {
    cls = "bg-brand-emerald/15 text-brand-emerald";
  } else if (v.includes("menengah")) {
    cls = "bg-brand-amber/15 text-brand-amber";
  } else if (v.includes("tinggi")) {
    cls = "bg-brand-rose/15 text-brand-rose";
  }
  return <span className={basePill(cls)}>{risk}</span>;
}

export function SektorBadge({ sektor }: { sektor: string | null | undefined }) {
  if (!sektor) return <span className="text-xs text-text-muted">—</span>;
  return (
    <span className={basePill("bg-surface-card-hover text-text-secondary")}>
      {sektor}
    </span>
  );
}
