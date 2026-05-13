"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/sonner";
import { useState, type ReactNode } from "react";

/**
 * Top-level client providers for the admin app.
 *
 * - TanStack Query for data fetching (Phase 2 will add resource endpoints).
 * - TooltipProvider so shadcn Tooltip works without per-component wrapping.
 * - Sonner toaster mounted globally; `toast()` can be called from anywhere.
 *
 * Phase 2 will add an AuthProvider that exposes `user` to children based on
 * the httpOnly admin_token cookie validated against admin-api `/auth/me`.
 * For Phase 1 the middleware in `src/middleware.ts` already enforces the
 * cookie's presence — protected routes never render without it.
 */
export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // Calm-by-default: no thundering herds when a tab regains focus.
            refetchOnWindowFocus: false,
            // 30s stale window is enough for an internal admin tool — keeps
            // the list/detail navigation snappy without serving truly stale data.
            staleTime: 30_000,
            retry: 1,
          },
        },
      })
  );

  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={200}>
        {children}
        <Toaster position="bottom-right" richColors />
      </TooltipProvider>
      {process.env.NODE_ENV === "development" && (
        <ReactQueryDevtools initialIsOpen={false} />
      )}
    </QueryClientProvider>
  );
}
