"use client";

import { ChevronLeft, ChevronRight, ArrowRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { ArchComponent, ArchFlow } from "@/lib/architecture";

type StepNavigatorProps = {
  flow: ArchFlow;
  currentIndex: number;
  components: ArchComponent[];
  onPrev: () => void;
  onNext: () => void;
};

/**
 * Step detail card — shows `from → to`, the step's label, and the
 * verbatim payload string from the JSON. The payload field is the
 * primary "teaching" surface — judges will read these.
 */
export function StepNavigator({
  flow,
  currentIndex,
  components,
  onPrev,
  onNext,
}: StepNavigatorProps) {
  const step = flow.steps[currentIndex];
  if (!step) return null;
  const fromLabel = labelFor(components, step.from);
  const toLabel = labelFor(components, step.to);
  const isSelfLoop = step.from === step.to;
  const atStart = currentIndex === 0;
  const atEnd = currentIndex === flow.steps.length - 1;

  return (
    <div className="rounded-card bg-surface-card p-4 space-y-3">
      <div className="flex items-center justify-between text-[10px] uppercase tracking-[0.14em] text-text-muted">
        <span>
          Langkah {currentIndex + 1} / {flow.steps.length}
        </span>
        <span className="text-brand-amber">{flow.label}</span>
      </div>

      <div className="space-y-2">
        <div className="flex items-center gap-2 text-sm font-medium text-text-primary">
          <span className="truncate">{fromLabel}</span>
          {!isSelfLoop && (
            <ArrowRight
              className="size-3.5 shrink-0 text-brand-amber"
              aria-hidden
            />
          )}
          {!isSelfLoop && <span className="truncate">{toLabel}</span>}
          {isSelfLoop && (
            <span className="text-[10px] rounded-pill bg-surface-base/60 px-1.5 py-0.5 text-text-muted">
              internal
            </span>
          )}
        </div>
        <div className="text-xs text-text-secondary leading-snug">
          {step.label}
        </div>
        <pre className="mt-2 whitespace-pre-wrap break-words rounded-md bg-surface-base/60 px-2.5 py-2 text-[11px] leading-relaxed text-text-secondary font-mono">
          {step.payload}
        </pre>
      </div>

      <div className="flex items-center gap-2 pt-1">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onPrev}
          disabled={atStart}
          className={cn(
            "flex-1 border-white/10 bg-surface-base/40",
            atStart && "opacity-40"
          )}
          aria-label="Langkah sebelumnya"
        >
          <ChevronLeft className="size-3.5" aria-hidden />
          Prev
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onNext}
          disabled={atEnd}
          className={cn(
            "flex-1 border-white/10 bg-surface-base/40",
            atEnd && "opacity-40"
          )}
          aria-label="Langkah berikutnya"
        >
          Next
          <ChevronRight className="size-3.5" aria-hidden />
        </Button>
      </div>
    </div>
  );
}

function labelFor(components: ArchComponent[], id: string): string {
  return components.find((c) => c.id === id)?.label ?? id;
}
