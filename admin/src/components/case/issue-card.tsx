"use client";

import { motion } from "framer-motion";
import {
  AlertTriangle,
  AlertOctagon,
  AlertCircle,
  Info,
  FileText,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { IssueSeverity, ValidationIssue } from "@/lib/case-types";

/**
 * Severity-aware issue card. Color tokens trace 1:1 to Brand.md:
 *   critical / high → brand-rose
 *   medium          → brand-amber
 *   low             → brand-navy
 *
 * Related-docs are rendered as chips on `surface-card-hover` — same chip
 * pattern as the SektorBadge to keep visual rhythm consistent across pages.
 */

type SeverityConfig = {
  icon: typeof AlertTriangle;
  label: string;
  ringClass: string;
  iconClass: string;
  badgeClass: string;
};

const SEVERITY: Record<IssueSeverity, SeverityConfig> = {
  critical: {
    icon: AlertOctagon,
    label: "Kritis",
    ringClass: "ring-brand-rose/30",
    iconClass: "text-brand-rose",
    badgeClass: "bg-brand-rose/15 text-brand-rose",
  },
  high: {
    icon: AlertTriangle,
    label: "Tinggi",
    ringClass: "ring-brand-rose/20",
    iconClass: "text-brand-rose",
    badgeClass: "bg-brand-rose/15 text-brand-rose",
  },
  medium: {
    icon: AlertCircle,
    label: "Sedang",
    ringClass: "ring-brand-amber/20",
    iconClass: "text-brand-amber",
    badgeClass: "bg-brand-amber/15 text-brand-amber",
  },
  low: {
    icon: Info,
    label: "Rendah",
    ringClass: "ring-brand-navy/20",
    iconClass: "text-brand-navy-hover",
    badgeClass: "bg-brand-navy/20 text-text-primary",
  },
};

export function IssueCard({
  issue,
  index,
}: {
  issue: ValidationIssue;
  index: number;
}) {
  const cfg = SEVERITY[issue.severity] ?? SEVERITY.low;
  const Icon = cfg.icon;

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        duration: 0.25,
        ease: "easeOut",
        delay: Math.min(index, 8) * 0.04,
      }}
      className={cn(
        "rounded-card bg-surface-card p-5 sm:p-6 ring-1 transition-colors",
        cfg.ringClass
      )}
    >
      <div className="flex items-start gap-3">
        <div
          className={cn(
            "shrink-0 rounded-full bg-surface-base/60 p-2",
            cfg.iconClass
          )}
          aria-hidden
        >
          <Icon className="size-4" />
        </div>

        <div className="flex-1 min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={cn(
                "inline-flex items-center rounded-pill px-2 py-0.5 text-[11px] font-medium leading-none uppercase tracking-wider",
                cfg.badgeClass
              )}
            >
              {cfg.label}
            </span>
            <span className="font-mono text-xs text-text-secondary truncate">
              {issue.field}
            </span>
          </div>

          <p className="text-sm text-text-primary leading-relaxed">
            {issue.message}
          </p>

          {issue.related_docs?.length > 0 && (
            <div
              className="flex flex-wrap gap-1.5 pt-1"
              aria-label="Berkas terkait"
            >
              {issue.related_docs.map((doc) => (
                <span
                  key={doc}
                  className="inline-flex items-center gap-1 rounded-pill bg-surface-card-hover px-2 py-0.5 text-[11px] text-text-secondary"
                >
                  <FileText
                    className="size-3 text-text-muted"
                    aria-hidden
                  />
                  <span className="truncate max-w-[200px]">{doc}</span>
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </motion.div>
  );
}
