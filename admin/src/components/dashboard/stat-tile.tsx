"use client";

import { useEffect, useState } from "react";
import { motion, useMotionValue, useTransform, animate } from "framer-motion";
import type { LucideIcon } from "lucide-react";
import { Card } from "@/components/ui/card";

/**
 * Dashboard stat tile per Brand spec.
 *
 * - Icon top-left in brand-navy
 * - Big number in display font (3xl)
 * - Label below in text-secondary
 * - Card hover: scale 1.01 + soft navy shadow lift (150ms)
 * - Count-up: 0 → value over 800ms easeOut cubic
 */
export function StatTile({
  label,
  value,
  icon: Icon,
}: {
  label: string;
  value: number;
  icon: LucideIcon;
}) {
  const count = useMotionValue(0);
  const rounded = useTransform(count, (latest) => Math.round(latest).toLocaleString("id-ID"));
  const [display, setDisplay] = useState("0");

  useEffect(() => {
    const controls = animate(count, value, {
      duration: 0.8,
      ease: [0.16, 1, 0.3, 1], // ease-out cubic-ish
    });
    const unsub = rounded.on("change", (v) => setDisplay(v));
    return () => {
      controls.stop();
      unsub();
    };
  }, [count, rounded, value]);

  return (
    <motion.div
      whileHover={{ scale: 1.01 }}
      transition={{ duration: 0.15, ease: "easeOut" }}
    >
      <Card className="bg-surface-card border-0 rounded-card p-6 hover:shadow-[0_8px_24px_-8px_rgba(46,77,204,0.3)] transition-shadow">
        <div className="flex items-start justify-between">
          <div className="rounded-lg bg-brand-navy/15 p-2 text-brand-navy">
            <Icon className="size-5" aria-hidden />
          </div>
        </div>
        <div className="mt-6">
          <motion.div className="font-display text-3xl font-semibold text-text-primary tracking-tight">
            {display}
          </motion.div>
          <div className="mt-1 text-sm text-text-secondary">{label}</div>
        </div>
      </Card>
    </motion.div>
  );
}
