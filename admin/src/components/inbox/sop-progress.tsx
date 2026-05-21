"use client";

import { cn } from "@/lib/utils";
import type { SopState } from "@/lib/inbox-types";

/**
 * SOP-as-motivator surface (Wave 4, Vision req #15).
 *
 * The SOP / SLA — `days_open` against the licence's `sop_days` (jangka
 * waktu) — is shown as a *motivator, not a whip*. A calm progress bar plus
 * encouraging Indonesian copy nudges the officer to keep things moving while
 * feeling supported, never policed:
 *
 *   on_track    → emerald  · "3 dari 14 hari kerja — masih on track"
 *   approaching → amber    · "11 dari 14 hari — ayo selesaikan, hampir tuntas"
 *   over        → amber    · "16 dari 14 hari — sedikit lewat, mari tuntaskan"
 *                            (a steady amber, deliberately NOT an alarming red)
 *   unknown     → neutral  · SOP days unresolved — show elapsed days plainly
 *
 * Why no red even when over SOP: a red "you failed" pill shames the officer
 * and makes the SOP a stick. The brief is explicit — the goal is to help the
 * officer move faster while feeling backed up. Amber says "let's wrap this
 * up", red says "you're in trouble". We always say the former.
 */

type SopTone = {
  /** Tailwind bg for the filled portion of the bar. */
  bar: string;
  /** Tailwind text colour for the caption. */
  text: string;
  /** Tailwind bg for the track (un-filled portion). */
  track: string;
};

const TONE: Record<SopState, SopTone> = {
  on_track: {
    bar: "bg-brand-emerald",
    text: "text-brand-emerald",
    track: "bg-brand-emerald/12",
  },
  approaching: {
    bar: "bg-brand-amber",
    text: "text-brand-amber",
    track: "bg-brand-amber/12",
  },
  // Deliberately amber, not rose — SOP is a motivator, not a whip.
  over: {
    bar: "bg-brand-amber",
    text: "text-brand-amber",
    track: "bg-brand-amber/12",
  },
  unknown: {
    bar: "bg-text-muted",
    text: "text-text-muted",
    track: "bg-surface-card-hover",
  },
};

/**
 * Encouraging caption — frames progress positively in every state.
 * `daysOpen` / `sopDays` come straight from the inbox row.
 */
function caption(
  state: SopState,
  daysOpen: number,
  sopDays: number | null
): string {
  if (state === "unknown" || sopDays == null) {
    return daysOpen === 0
      ? "Baru masuk hari ini — selamat memulai"
      : `Berjalan ${daysOpen} hari — tetap semangat`;
  }
  const used = `${daysOpen} dari ${sopDays} hari kerja`;
  if (state === "on_track") {
    return daysOpen <= 1
      ? `${used} — waktu masih lapang`
      : `${used} — masih on track, kerja bagus`;
  }
  if (state === "approaching") {
    return `${used} — hampir tuntas, ayo diselesaikan`;
  }
  // over — supportive, never shaming
  const over = daysOpen - sopDays;
  return `${used} — lewat ${over} hari, mari kita tuntaskan`;
}

export function SopProgress({
  daysOpen,
  sopDays,
  sopState,
  className,
}: {
  daysOpen: number;
  sopDays: number | null;
  sopState: SopState;
  className?: string;
}) {
  const tone = TONE[sopState];

  // Fill ratio for the bar. Capped at 100% so an over-SOP case shows a full —
  // not overflowing — bar; the caption carries the "how far over" detail.
  const ratio =
    sopDays && sopDays > 0 ? Math.min(daysOpen / sopDays, 1) : daysOpen > 0 ? 1 : 0;
  const pct = Math.round(ratio * 100);

  const label = caption(sopState, daysOpen, sopDays);

  return (
    <div className={cn("space-y-1.5", className)}>
      <div
        className={cn("h-1.5 w-full overflow-hidden rounded-pill", tone.track)}
        role="progressbar"
        aria-valuenow={daysOpen}
        aria-valuemin={0}
        aria-valuemax={sopDays ?? undefined}
        aria-label={label}
      >
        <div
          className={cn(
            "h-full rounded-pill transition-[width] duration-500 ease-out",
            tone.bar
          )}
          style={{ width: `${Math.max(pct, daysOpen > 0 ? 6 : 0)}%` }}
        />
      </div>
      <p className={cn("text-[11px] leading-snug", tone.text)}>{label}</p>
    </div>
  );
}

/**
 * Compact SOP pill — the at-a-glance "days / SOP" chip for a table row.
 * Pairs with `SopProgress`; same calm tone discipline.
 */
export function SopDaysPill({
  daysOpen,
  sopDays,
  sopState,
}: {
  daysOpen: number;
  sopDays: number | null;
  sopState: SopState;
}) {
  const tone = TONE[sopState];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-pill px-2 py-0.5 text-[11px] font-medium tabular-nums",
        tone.track,
        tone.text
      )}
    >
      <span className="font-mono">{daysOpen}</span>
      <span className="opacity-60">/</span>
      <span className="font-mono">{sopDays ?? "—"}</span>
      <span className="opacity-70">hari</span>
    </span>
  );
}
