import { Skeleton } from "@/components/ui/Skeleton";

/**
 * Route-segment loading for /dashboard. Mirrors the real layout: header,
 * three stat cards, a two-column license grid. The async stub resolves
 * quickly; this keeps the transition smooth and intentional.
 */
export default function DashboardLoading() {
  return (
    <main className="min-h-screen px-4 py-8 sm:px-6 sm:py-10 lg:px-8">
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-6">
        <div className="flex items-center justify-between">
          <Skeleton className="h-4 w-24 rounded-pill" />
          <Skeleton className="h-9 w-24 rounded-pill" />
        </div>

        <div className="space-y-2">
          <Skeleton className="h-3 w-32 rounded-pill" />
          <Skeleton className="h-8 w-48" />
        </div>

        <div className="grid gap-4 sm:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-32" />
          ))}
        </div>

        <Skeleton className="h-5 w-28" />
        <div className="grid gap-4 md:grid-cols-2">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-48" />
          ))}
        </div>

        <span role="status" aria-live="polite" className="sr-only">
          Memuat dashboard izin…
        </span>
      </div>
    </main>
  );
}
