import { Check, Circle } from "lucide-react";

import { formatDateID } from "@/lib/format";
import type { LicenseTimelineEvent } from "@/lib/citizen-dashboard-client";

/**
 * LicenseTimeline — a vertical lifecycle of a license (Pengajuan → Terbit →
 * Perpanjangan). Completed steps get a filled check; the current/pending step
 * gets a hollow ring. Semantic <ol>; color is paired with an icon + label.
 */
export function LicenseTimeline({
  events,
}: {
  events: LicenseTimelineEvent[];
}) {
  return (
    <ol className="relative space-y-6">
      {events.map((ev, i) => {
        const isLast = i === events.length - 1;
        return (
          <li key={`${ev.date}-${ev.label}`} className="relative flex gap-4">
            {/* connector line */}
            {!isLast && (
              <span
                aria-hidden="true"
                className="absolute left-[11px] top-7 h-[calc(100%+0.5rem)] w-px bg-border"
              />
            )}
            <span
              className={
                ev.done
                  ? "z-10 inline-flex size-6 shrink-0 items-center justify-center rounded-pill bg-brand-green text-white"
                  : "z-10 inline-flex size-6 shrink-0 items-center justify-center rounded-pill border border-border-strong bg-surface text-ink-3"
              }
            >
              {ev.done ? (
                <Check className="size-3.5" aria-hidden="true" />
              ) : (
                <Circle className="size-2.5 fill-current" aria-hidden="true" />
              )}
            </span>
            <div className="min-w-0 pb-1">
              <p className="font-medium text-ink">{ev.label}</p>
              <p className="mt-0.5 text-xs text-ink-3">
                {formatDateID(ev.date)}
                {!ev.done && " · belum selesai"}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
