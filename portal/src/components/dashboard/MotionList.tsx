"use client";

import { motion, useReducedMotion } from "framer-motion";

/**
 * MotionList — a thin client wrapper that stagger-fades its children into
 * view once (opacity 0→1, y 8→0, ~200ms, capped delay). Used to animate the
 * license/application card grids. Reduced-motion renders everything static
 * (no opacity/transform animation) so there's no layout shift.
 *
 * Children are wrapped per-item, so callers just map their data into
 * <MotionList.Item> children.
 */
export function MotionList({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <div className={className}>{children}</div>;
}

export function MotionItem({
  index,
  children,
}: {
  index: number;
  children: React.ReactNode;
}) {
  const reduce = useReducedMotion();
  // Cap the stagger so a long list doesn't crawl in.
  const delay = Math.min(index, 6) * 0.04;

  return (
    <motion.div
      initial={reduce ? false : { opacity: 0, y: 8 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-40px" }}
      transition={{ duration: reduce ? 0 : 0.2, ease: "easeOut", delay }}
      className="h-full"
    >
      {children}
    </motion.div>
  );
}
