import { Skeleton } from "@/components/ui/Skeleton";

/**
 * Route-segment loading state for /track/[ticket]. The page itself is a
 * server fetch (cache: no-store), so this skeleton makes navigation from
 * WhatsApp feel instant while admin-api responds.
 */
export default function TrackLoading() {
  return (
    <main className="min-h-screen px-4 py-10 sm:px-6 sm:py-12 lg:px-8">
      <div className="mx-auto flex w-full max-w-2xl flex-col gap-6">
        <Skeleton className="h-4 w-44 rounded-pill" />
        <div className="mt-2 space-y-3">
          <Skeleton className="h-3 w-40 rounded-pill" />
          <Skeleton className="h-9 w-56" />
        </div>
        <Skeleton className="h-64 w-full" />
        <span role="status" aria-live="polite" className="sr-only">
          Memuat status permohonan…
        </span>
      </div>
    </main>
  );
}
