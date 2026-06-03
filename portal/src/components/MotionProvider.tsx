"use client";

import { MotionConfig } from "framer-motion";

/**
 * Global Framer reduced-motion governor.
 *
 * The CSS `@media (prefers-reduced-motion)` kill-switch in globals.css only
 * governs CSS transitions/animations — it does NOT stop Framer Motion, which
 * drives opacity/transform via JS/WAAPI. `reducedMotion="user"` makes Framer
 * honor the OS setting for EVERY descendant motion component (landing,
 * dashboard, auth, and anything added later): transform/scale/layout
 * animations are skipped; harmless opacity fades remain. One global point
 * instead of per-component `useReducedMotion()` plumbing.
 */
export function MotionProvider({ children }: { children: React.ReactNode }) {
  return <MotionConfig reducedMotion="user">{children}</MotionConfig>;
}
