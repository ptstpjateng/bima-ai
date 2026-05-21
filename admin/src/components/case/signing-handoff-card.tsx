"use client";

import { motion } from "framer-motion";
import { ExternalLink, PenLine, ShieldCheck } from "lucide-react";
import { buildSiapSigningUrl } from "@/lib/case-types";

/**
 * Signing handoff card — Wave 4, Vision req #13.
 *
 * The Head of DPMPTSP reviews a case with the BIMA signature assistant, then
 * signs it in SIAP Jateng. BIMA does NOT perform the digital signature —
 * TTE/BSRE is owned entirely by SIAP. This card is the clean handoff: a
 * single, unmistakable call-to-action that deep-links into SIAP's "Tanda
 * Tangan Berkas" page, pre-filtered to this exact ticket.
 *
 * Brand: Midnight Government — amber primary accent for the decisive action,
 * surface-card panel, no hard borders. The link opens in a new tab so the
 * BIMA review context is not lost.
 */
export function SigningHandoffCard({ ticket }: { ticket: string }) {
  const signingUrl = buildSiapSigningUrl(ticket);

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: "easeOut" }}
      className="rounded-card bg-surface-card ring-1 ring-brand-amber/25 p-5 sm:p-6"
    >
      <div className="flex items-start gap-3">
        <div
          className="shrink-0 rounded-full bg-brand-amber/15 p-2.5 text-brand-amber"
          aria-hidden
        >
          <ShieldCheck className="size-5" />
        </div>

        <div className="flex-1 min-w-0 space-y-3">
          <div className="space-y-1">
            <h3 className="font-display text-base font-semibold text-text-primary leading-snug">
              Siap ditandatangani
            </h3>
            <p className="text-sm text-text-secondary leading-relaxed">
              Setelah meninjau rantai persetujuan bersama BIMA, lakukan tanda
              tangan elektronik di SIAP Jateng. Penandatanganan (TTE/BSRE)
              memakai passphrase BSrE Anda — BIMA tidak menandatangani
              dokumen.
            </p>
          </div>

          <a
            href={signingUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-2 rounded-card bg-brand-amber px-4 py-2.5 text-sm font-medium text-surface-base transition-colors hover:bg-brand-amber-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-amber/50"
          >
            <PenLine className="size-4" aria-hidden />
            Tanda tangani di SIAP Jateng
            <ExternalLink className="size-3.5 opacity-70" aria-hidden />
          </a>

          <p className="text-[11px] text-text-muted leading-relaxed">
            Tautan membuka SIAP Jateng di tab baru, tersaring ke tiket{" "}
            <span className="font-mono text-text-secondary">{ticket}</span>.
          </p>
        </div>
      </div>
    </motion.div>
  );
}
