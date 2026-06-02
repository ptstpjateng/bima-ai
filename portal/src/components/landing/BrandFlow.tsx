"use client";

import { motion, useReducedMotion } from "framer-motion";

/**
 * BrandFlow — an animated SVG that *describes* the BIMA idea: a single
 * continuous stroke (the brand's "seamless workflow" motif from the logo)
 * draws itself, and a few integration nodes light up along it ("smart
 * integration"). Decorative, so aria-hidden.
 *
 * Motion is purposeful + restrained (one draw, gentle node pulse). Honors
 * prefers-reduced-motion: when set, the stroke renders fully drawn and
 * static — no animation, no layout shift.
 */
export function BrandFlow({ className }: { className?: string }) {
  const reduce = useReducedMotion();

  // Single organic continuous stroke — stacked counters echoing the "b" mark.
  const d =
    "M64 42 C150 24 184 96 124 124 C82 143 70 96 108 92 " +
    "C170 85 188 182 98 206 C56 218 44 168 74 152";

  // Integration nodes positioned along the stroke.
  const nodes = [
    { cx: 64, cy: 42 },
    { cx: 124, cy: 124 },
    { cx: 98, cy: 206 },
  ];

  return (
    <svg
      viewBox="0 0 240 248"
      fill="none"
      className={className}
      role="img"
      aria-label="BIMA — alur layanan yang mengalir mulus"
    >
      {/* soft halo */}
      <circle cx="120" cy="124" r="104" className="fill-primary/5" />

      <motion.path
        d={d}
        className="stroke-primary"
        strokeWidth={22}
        strokeLinecap="round"
        strokeLinejoin="round"
        initial={reduce ? { pathLength: 1, opacity: 1 } : { pathLength: 0, opacity: 0.2 }}
        animate={{ pathLength: 1, opacity: 1 }}
        transition={
          reduce
            ? { duration: 0 }
            : { duration: 1.6, ease: [0.22, 1, 0.36, 1] }
        }
      />

      {nodes.map((n, i) => (
        <motion.circle
          key={i}
          cx={n.cx}
          cy={n.cy}
          r={9}
          className="fill-accent"
          initial={reduce ? { opacity: 1, scale: 1 } : { opacity: 0, scale: 0.4 }}
          animate={
            reduce
              ? { opacity: 1, scale: 1 }
              : { opacity: [0, 1, 0.75], scale: [0.4, 1.15, 1] }
          }
          transition={
            reduce
              ? { duration: 0 }
              : { duration: 0.5, delay: 1.2 + i * 0.18, ease: "easeOut" }
          }
        />
      ))}
    </svg>
  );
}
