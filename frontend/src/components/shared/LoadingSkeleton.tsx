import { cn } from "@/lib/utils";

interface SkeletonProps {
  className?: string;
}

export function Skeleton({ className }: SkeletonProps) {
  return (
    <div
      className={cn(
        "animate-pulse rounded-md bg-gray-200",
        className,
      )}
    />
  );
}

export function CardSkeleton() {
  return (
    <div className="rounded-2xl border border-gray-100 bg-white p-6 shadow-sm">
      <Skeleton className="mb-3 h-4 w-2/3" />
      <Skeleton className="mb-2 h-8 w-1/3" />
      <Skeleton className="h-3 w-1/2" />
    </div>
  );
}

export function PermitCardSkeleton() {
  return (
    <div className="rounded-2xl border border-gray-100 bg-white p-6 shadow-sm">
      <div className="mb-4 flex items-start justify-between">
        <div className="flex-1">
          <Skeleton className="mb-2 h-5 w-3/4" />
          <Skeleton className="h-3 w-1/2" />
        </div>
        <Skeleton className="ml-4 h-6 w-24 rounded-full" />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-full" />
      </div>
    </div>
  );
}

export function ChatSkeleton() {
  return (
    <div className="space-y-3">
      {[1, 2, 3].map((i) => (
        <div
          key={i}
          className={cn(
            "flex gap-2",
            i % 2 === 0 ? "flex-row-reverse" : "flex-row",
          )}
        >
          <Skeleton className="h-8 w-8 flex-shrink-0 rounded-full" />
          <Skeleton
            className={cn(
              "h-12 rounded-2xl",
              i % 2 === 0 ? "w-2/3" : "w-3/4",
            )}
          />
        </div>
      ))}
    </div>
  );
}

export function PageLoader() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50">
      <div className="text-center">
        <div className="mx-auto mb-4 h-12 w-12 animate-spin rounded-full border-4 border-brand-200 border-t-brand-600" />
        <p className="text-sm text-gray-500">Memuat...</p>
      </div>
    </div>
  );
}
