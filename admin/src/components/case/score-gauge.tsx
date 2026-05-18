"use client";

import { motion } from "framer-motion";
import { cn } from "@/lib/utils";
import {
  VALIDATION_STATUS_LABEL,
  type ValidationStatus,
} from "@/lib/case-types";

/**
 * Hand-rolled SVG radial score gauge — no chart library per brief.
 *
 * Color-coded against the Brand palette tokens:
 *   ≥ 90  → brand-emerald  (#10B981, "ready")
 *   70–89 → brand-amber    (#F59E0B, "minor")
 *   < 70  → brand-rose     (#F43F5E, "major")
 *
 * Track is a faint navy-tinted ring so the foreground arc reads against
 * surface-card. Animation is a single 600ms ease-out sweep — calm, per Brand
 * §"Animation guidelines".
 */
const SIZE = 200;
const STROKE = 18;
const RADIUS = (SIZE - STROKE) / 2;
const CIRC = 2 * Math.PI * RADIUS;

export type GaugeTone = "emerald" | "amber" | "rose" | "muted";

function toneFor(percent: number, status: ValidationStatus): GaugeTone {
  if (status === "unverified") return "muted";
  if (percent >= 90) return "emerald";
  if (percent >= 70) return "amber";
  return "rose";
}

const TONE_TEXT: Record<GaugeTone, string> = {
  emerald: "text-brand-emerald",
  amber: "text-brand-amber",
  rose: "text-brand-rose",
  muted: "text-text-muted",
};

// SVG `stroke` accepts `currentColor`, so we just inherit from the wrapping
// span's text color — keeps the gauge a single token-driven element.
export function ScoreGauge({
  percent,
  status,
  className,
}: {
  percent: number;
  status: ValidationStatus;
  className?: string;
}) {
  const clamped = Math.max(0, Math.min(100, Math.round(percent)));
  const tone = toneFor(clamped, status);
  const offset = CIRC - (clamped / 100) * CIRC;

  return (
    <div
      className={cn(
        "relative inline-flex items-center justify-center",
        className
      )}
      style={{ width: SIZE, height: SIZE }}
      role="img"
      aria-label={`Skor ${clamped} persen — ${VALIDATION_STATUS_LABEL[status]}`}
    >
      <svg
        width={SIZE}
        height={SIZE}
        viewBox={`0 0 ${SIZE} ${SIZE}`}
        className={cn("-rotate-90", TONE_TEXT[tone])}
        aria-hidden
      >
        {/* Track — quiet navy-tinted ring; no hard border, per "No-Line Rule". */}
        <circle
          cx={SIZE / 2}
          cy={SIZE / 2}
          r={RADIUS}
          fill="none"
          stroke="currentColor"
          strokeOpacity={0.15}
          strokeWidth={STROKE}
          strokeLinecap="round"
        />
        {/* Foreground arc. */}
        <motion.circle
          cx={SIZE / 2}
          cy={SIZE / 2}
          r={RADIUS}
          fill="none"
          stroke="currentColor"
          strokeWidth={STROKE}
          strokeLinecap="round"
          strokeDasharray={CIRC}
          initial={{ strokeDashoffset: CIRC }}
          animate={{ strokeDashoffset: offset }}
          transition={{ duration: 0.6, ease: "easeOut" }}
        />
      </svg>

      <div className="absolute inset-0 flex flex-col items-center justify-center text-center">
        <motion.span
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, delay: 0.2, ease: "easeOut" }}
          className={cn(
            "font-display text-5xl font-semibold tabular-nums leading-none",
            TONE_TEXT[tone]
          )}
        >
          {clamped}
          <span className="text-2xl text-text-secondary font-medium">%</span>
        </motion.span>
        <span className="mt-1 text-[10px] uppercase tracking-widest text-text-muted">
          BIMA Score
        </span>
      </div>
    </div>
  );
}

export function gaugeTone(percent: number, status: ValidationStatus): GaugeTone {
  return toneFor(percent, status);
}
