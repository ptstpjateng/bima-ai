import { RefreshCw, MessageCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { RenewalCountdown } from "@/components/ui/RenewalCountdown";
import type { CitizenRenewal } from "@/lib/citizen-dashboard-client";

/**
 * RenewalPanel — countdown + "Ajukan perpanjangan" action for a license.
 * When the renewal window isn't open yet, the action is disabled and a
 * helper explains when it opens (information, not a dead end).
 */
export function RenewalPanel({
  renewal,
  expiresAt,
}: {
  /** The matching renewal record, or null if none applies. */
  renewal: CitizenRenewal | null;
  expiresAt: string | null;
}) {
  return (
    <section
      aria-labelledby="renewal-heading"
      className="rounded-card border border-border bg-surface p-5 sm:p-6"
    >
      <div className="flex items-center gap-3">
        <span className="inline-flex size-9 items-center justify-center rounded-pill bg-surface-2">
          <RefreshCw className="size-4 text-primary" aria-hidden="true" />
        </span>
        <h2
          id="renewal-heading"
          className="font-display text-base font-semibold text-ink"
        >
          Perpanjangan
        </h2>
      </div>

      <div className="mt-4">
        <RenewalCountdown expiresAt={expiresAt} />
      </div>

      {renewal?.eligible ? (
        <>
          <p className="mt-4 text-sm text-ink-2">
            Jendela perpanjangan sudah terbuka. Ajukan lewat WhatsApp — BIMA
            akan memandu dokumen yang diperlukan.
          </p>
          <Button asChild variant="amber" size="md" className="mt-4">
            <a
              href={renewal.action_url}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Ajukan perpanjangan lewat WhatsApp BIMA"
            >
              <MessageCircle aria-hidden="true" />
              <span>Ajukan perpanjangan</span>
            </a>
          </Button>
        </>
      ) : (
        <>
          <p className="mt-4 text-sm text-ink-2">
            {renewal
              ? `Perpanjangan dibuka ${renewal.window_days} hari sebelum kedaluwarsa.`
              : "Belum ada jadwal perpanjangan untuk izin ini."}
          </p>
          <Button variant="amber" size="md" className="mt-4" disabled>
            <MessageCircle aria-hidden="true" />
            <span>Ajukan perpanjangan</span>
          </Button>
        </>
      )}
    </section>
  );
}
