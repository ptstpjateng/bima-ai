"use client";

import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { cn } from "@/lib/utils";
import type { ComponentKind } from "@/lib/architecture";

export type BimaNodeData = {
  label: string;
  subtitle?: string;
  kind: ComponentKind;
  /**
   * Set by Canvas to 0.3 when the node is *not* involved in the currently
   * selected flow. Default 1 = full opacity.
   */
  dimmed?: boolean;
};

/**
 * Maps the component `kind` to a 2px top "accent strip" colour. Borders
 * sit inside Tailwind utilities so the rest of the brand palette flows
 * through cleanly. The brief specified colours per kind — see
 * project brief 'Visual specs' section.
 */
const STRIP_BY_KIND: Record<ComponentKind, string> = {
  external: "bg-text-muted",
  edge: "bg-brand-amber",
  service: "bg-brand-navy",
  data: "bg-brand-emerald",
  frontend: "bg-brand-amber-hover",
  llm: "bg-brand-rose",
};

const LABEL_BY_KIND: Record<ComponentKind, string> = {
  external: "External",
  edge: "Edge",
  service: "Service",
  data: "Data store",
  frontend: "Frontend",
  llm: "LLM",
};

/**
 * Read-only node card used for every component on the architecture
 * canvas. ~180px wide, dark surface card, with a 2px top accent strip
 * coloured by component kind.
 */
function BimaNodeImpl({ data, selected }: NodeProps) {
  const d = data as unknown as BimaNodeData;
  const dimmed = d.dimmed === true;
  return (
    <div
      className={cn(
        "relative w-[180px] rounded-card overflow-hidden bg-surface-card",
        "shadow-[0_1px_0_0_rgba(255,255,255,0.04)_inset]",
        "transition-opacity duration-300",
        dimmed ? "opacity-30" : "opacity-100",
        selected && "ring-1 ring-brand-amber/60"
      )}
    >
      <div className={cn("h-[2px] w-full", STRIP_BY_KIND[d.kind])} aria-hidden />

      <div className="px-3 pt-2.5 pb-3">
        <div className="font-display text-[14px] font-medium leading-tight text-text-primary tracking-tight">
          {d.label}
        </div>
        {d.subtitle && (
          <div className="mt-1 text-[11px] leading-tight text-text-muted truncate">
            {d.subtitle}
          </div>
        )}
        <div className="mt-1.5 text-[9px] uppercase tracking-[0.12em] text-text-muted/70">
          {LABEL_BY_KIND[d.kind]}
        </div>
      </div>

      {/*
        Provide source + target handles on all four sides. React Flow picks
        the one that produces the cleanest edge for each connection. Hidden
        visually — read-only canvas, the user cannot drag from them.
      */}
      <Handle
        type="target"
        position={Position.Left}
        id="l"
        className="!h-2 !w-2 !bg-transparent !border-0 !opacity-0"
      />
      <Handle
        type="source"
        position={Position.Right}
        id="r"
        className="!h-2 !w-2 !bg-transparent !border-0 !opacity-0"
      />
      <Handle
        type="target"
        position={Position.Top}
        id="t"
        className="!h-2 !w-2 !bg-transparent !border-0 !opacity-0"
      />
      <Handle
        type="source"
        position={Position.Bottom}
        id="b"
        className="!h-2 !w-2 !bg-transparent !border-0 !opacity-0"
      />
    </div>
  );
}

export const BimaNode = memo(BimaNodeImpl);
