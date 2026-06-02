import { Check } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * StepIndicator — a slim multi-step progress row. Decorative dots/labels are
 * aria-hidden; the meaning is carried by a sr-only "Langkah N dari M" string
 * plus aria-current on the active step.
 */
export function StepIndicator({
  steps,
  current,
}: {
  steps: string[];
  /** 0-based index of the active step. */
  current: number;
}) {
  return (
    <nav aria-label="Langkah pendaftaran" className="mt-6">
      <p className="sr-only">
        Langkah {Math.min(current + 1, steps.length)} dari {steps.length}:{" "}
        {steps[current]}
      </p>
      <ol className="flex items-center gap-2" aria-hidden="true">
        {steps.map((label, i) => {
          const done = i < current;
          const active = i === current;
          return (
            <li key={label} className="flex items-center gap-2">
              <span
                className={cn(
                  "inline-flex size-6 items-center justify-center rounded-pill border text-xs font-semibold transition-colors duration-200",
                  done && "border-brand-green bg-brand-green text-white",
                  active && "border-brand-green bg-brand-green/10 text-brand-green-text",
                  !done && !active && "border-border bg-surface-2 text-ink-3",
                )}
              >
                {done ? <Check className="size-3.5" /> : i + 1}
              </span>
              <span
                className={cn(
                  "hidden text-xs sm:inline",
                  active ? "font-medium text-ink" : "text-ink-3",
                )}
              >
                {label}
              </span>
              {i < steps.length - 1 && (
                <span className="h-px w-4 bg-border sm:w-6" />
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
