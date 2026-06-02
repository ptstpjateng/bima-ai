"use client";

import { motion, useReducedMotion } from "framer-motion";

/**
 * LifecycleExplainer — a small Lottie/Remotion-style SVG that *describes* the
 * 3-phase license lifecycle: Pengajuan → Terbit → Perpanjangan. A single
 * continuous stroke draws itself (echoing BrandFlow) and three labelled nodes
 * light up along it. Decorative-but-described (role=img + aria-label).
 *
 * Reduced-motion: renders fully drawn and static — no animation, no layout
 * shift (the global kill-switch also flattens any residual transition).
 */
const PHASES = [
  { x: 26, label: "Pengajuan" },
  { x: 130, label: "Terbit" },
  { x: 234, label: "Perpanjangan" },
];

export function LifecycleExplainer({ className }: { className?: string }) {
  const reduce = useReducedMotion();

  // Gentle horizontal path threading the three phase nodes.
  const d = "M26 36 C 78 12, 104 60, 130 36 C 182 12, 208 60, 234 36";

  return (
    <svg
      viewBox="0 0 260 72"
      fill="none"
      className={className}
      role="img"
      aria-label="Alur izin: Pengajuan, lalu Terbit, lalu Perpanjangan"
    >
      <motion.path
        d={d}
        className="stroke-primary/50"
        strokeWidth={3}
        strokeLinecap="round"
        strokeDasharray="1 0"
        initial={reduce ? { pathLength: 1 } : { pathLength: 0 }}
        animate={{ pathLength: 1 }}
        transition={reduce ? { duration: 0 } : { duration: 1.4, ease: [0.22, 1, 0.36, 1] }}
      />
      {PHASES.map((p, i) => (
        <g key={p.label}>
          <motion.circle
            cx={p.x}
            cy={36}
            r={7}
            className="fill-brand-green"
            initial={reduce ? { opacity: 1, scale: 1 } : { opacity: 0, scale: 0.5 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={
              reduce ? { duration: 0 } : { duration: 0.4, delay: 0.5 + i * 0.35, ease: "easeOut" }
            }
            style={{ transformOrigin: `${p.x}px 36px` }}
          />
          <text
            x={p.x}
            y={62}
            textAnchor="middle"
            className="fill-ink-2 text-[9px] font-medium"
          >
            {p.label}
          </text>
        </g>
      ))}
    </svg>
  );
}
