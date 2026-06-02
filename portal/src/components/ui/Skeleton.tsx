import { cn } from "@/lib/utils";

/**
 * Skeleton — a pulsing placeholder block. aria-hidden so AT doesn't announce
 * the geometry; pair a screen with a polite "Memuat…" live region instead.
 * The pulse is killed by the global prefers-reduced-motion rule.
 */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className={cn("animate-pulse rounded-card bg-surface-2", className)}
    />
  );
}

/** A polite live region announcing a loading state to screen readers. */
export function LoadingAnnounce({ label = "Memuat…" }: { label?: string }) {
  return (
    <span role="status" aria-live="polite" className="sr-only">
      {label}
    </span>
  );
}
