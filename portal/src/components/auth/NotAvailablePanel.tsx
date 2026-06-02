import Link from "next/link";
import { Info, ArrowRight, ExternalLink } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * NotAvailablePanel — the HONEST "belum tersedia" surface.
 *
 * This is the crux of the "never a fake login" rule. When a scaffolded auth
 * flow (magic-link / reset) is invoked, the stub resolves to
 * `not_implemented` and the page renders THIS panel instead of any success
 * or logged-in state.
 *
 * - role="status" + aria-live="polite": it's informational, NOT an error
 *   (the user did nothing wrong) and NOT a success. AT users hear the
 *   "belum tersedia" message plus the working alternative.
 * - info token (not error red, not success green).
 * - Always offers a real, never-disabled fallback to the working NIK/NPWP
 *   login and to SIAP's own portal.
 */
export function NotAvailablePanel({
  message,
  /** Where the working login lives (the canonical real path). */
  loginHref = "/login",
}: {
  message: string;
  loginHref?: string;
}) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="mt-8 rounded-card border border-info/40 bg-info/10 p-6 sm:p-8"
    >
      <div className="flex items-start gap-3">
        <span className="mt-0.5 inline-flex size-8 shrink-0 items-center justify-center rounded-pill bg-info/15">
          <Info className="size-4 text-info" aria-hidden="true" />
        </span>
        <div className="min-w-0">
          <h2 className="font-display text-base font-semibold text-ink">
            Belum tersedia
          </h2>
          <p className="mt-2 text-sm leading-relaxed text-ink-2">{message}</p>

          <div className="mt-5 flex flex-col gap-3 sm:flex-row sm:items-center">
            <Button asChild variant="primary" size="md">
              <Link href={loginHref}>
                <span>Masuk dengan NIK / NPWP</span>
                <ArrowRight aria-hidden="true" />
              </Link>
            </Button>
            <a
              href="https://perizinan.jatengprov.go.id"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-sm font-medium text-accent-ink underline-offset-4 hover:underline"
            >
              <ExternalLink className="size-4" aria-hidden="true" />
              <span>Buka SIAP Jateng</span>
            </a>
          </div>
        </div>
      </div>
    </div>
  );
}
