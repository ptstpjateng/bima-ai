"use client";

import { CalendarDays, GitBranch, Workflow } from "lucide-react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import type { ArchFlow, ArchSnapshot } from "@/lib/architecture";

type SidebarProps = {
  snapshots: ArchSnapshot[];
  snapshot: ArchSnapshot;
  onSnapshotChange: (id: string) => void;
  selectedFlowId: string | null;
  onFlowSelect: (id: string) => void;
};

/**
 * Left rail of the Architecture page — snapshot picker on top, then the
 * snapshot summary, then a clickable list of flows. Selected flow is
 * highlighted in amber.
 */
export function ArchitectureSidebar({
  snapshots,
  snapshot,
  onSnapshotChange,
  selectedFlowId,
  onFlowSelect,
}: SidebarProps) {
  return (
    <aside className="flex flex-col gap-4 w-72 shrink-0" aria-label="Architecture controls">
      {/* Snapshot picker */}
      <div className="rounded-card bg-surface-card p-4 space-y-2">
        <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.14em] text-text-muted">
          <GitBranch className="size-3" aria-hidden />
          Snapshot
        </div>
        <Select value={snapshot.id} onValueChange={onSnapshotChange}>
          <SelectTrigger className="w-full bg-surface-base/60 border-white/10 text-text-primary">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {snapshots.map((s) => (
              <SelectItem key={s.id} value={s.id}>
                {s.label} · {s.date}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <div className="flex items-center gap-1.5 text-[11px] text-text-muted pt-1">
          <CalendarDays className="size-3" aria-hidden />
          {snapshot.date}
        </div>
        <p className="text-[12px] leading-relaxed text-text-secondary pt-1">
          {snapshot.summary}
        </p>
      </div>

      {/* Flow list */}
      <div className="rounded-card bg-surface-card p-4 space-y-3">
        <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.14em] text-text-muted">
          <Workflow className="size-3" aria-hidden />
          Alur ({snapshot.flows.length})
        </div>
        <ul className="space-y-1" role="list">
          {snapshot.flows.map((flow) => (
            <li key={flow.id}>
              <FlowButton
                flow={flow}
                active={flow.id === selectedFlowId}
                onSelect={() => onFlowSelect(flow.id)}
              />
            </li>
          ))}
        </ul>
        {selectedFlowId === null && (
          <p className="text-[11px] text-text-muted pt-1 leading-relaxed">
            Pilih sebuah alur untuk menyorot komponen yang terlibat dan
            menelusuri langkah demi langkah.
          </p>
        )}
      </div>
    </aside>
  );
}

function FlowButton({
  flow,
  active,
  onSelect,
}: {
  flow: ArchFlow;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "w-full text-left rounded-lg px-3 py-2 transition-colors group",
        active
          ? "bg-brand-amber/10 ring-1 ring-brand-amber/40"
          : "hover:bg-surface-card-hover/70"
      )}
      aria-pressed={active}
    >
      <div
        className={cn(
          "text-[13px] font-medium",
          active ? "text-brand-amber" : "text-text-primary"
        )}
      >
        {flow.label}
      </div>
      <div
        className={cn(
          "text-[11px] mt-0.5 leading-snug",
          active ? "text-text-secondary" : "text-text-muted"
        )}
      >
        {flow.summary}
      </div>
      <div className="mt-1 text-[10px] text-text-muted">
        {flow.steps.length} langkah
      </div>
    </button>
  );
}
