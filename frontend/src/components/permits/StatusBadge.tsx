import type { PermitStatus } from "@/types";
import { STATUS_COLORS, STATUS_LABELS } from "@/lib/utils";
import { cn } from "@/lib/utils";

interface StatusBadgeProps {
  status: PermitStatus;
  className?: string;
}

export function StatusBadge({ status, className }: StatusBadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium",
        STATUS_COLORS[status],
        className,
      )}
    >
      {STATUS_LABELS[status]}
    </span>
  );
}
