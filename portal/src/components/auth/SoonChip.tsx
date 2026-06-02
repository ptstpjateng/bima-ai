import { Sparkles } from "lucide-react";

/**
 * SoonChip — a small honest "Segera hadir" pill shown in the header of any
 * scaffold-only auth surface so it's never mistaken for a working flow.
 * Meaning is text-encoded (not color-only); uses the info token tint with
 * ink text for AA-safe contrast in both themes.
 */
export function SoonChip({ label = "Segera hadir" }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-pill border border-info/40 bg-info/10 px-2.5 py-1 text-xs font-medium text-ink">
      <Sparkles className="size-3.5 text-info" aria-hidden="true" />
      {label}
    </span>
  );
}
