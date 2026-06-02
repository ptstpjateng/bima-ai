"use client";

import { MessageCircle, ArrowDown } from "lucide-react";
import { motion } from "framer-motion";

import { Button } from "@/components/ui/button";
import { BrandFlow } from "@/components/landing/BrandFlow";
import { WHATSAPP_URL } from "@/lib/constants";

/**
 * Hero — two-column on desktop (message + animated brand flow), single column
 * on mobile. Restrained motion, Bima palette, WCAG-AA contrast. No background
 * ornament beyond the brand mark's own soft halo.
 */
export function Hero() {
  return (
    <section className="relative px-4 py-16 sm:px-6 sm:py-20 lg:px-8">
      <div className="mx-auto grid w-full max-w-6xl items-center gap-12 lg:grid-cols-[1.15fr_0.85fr]">
        <motion.div
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.25, ease: "easeOut" }}
          className="flex flex-col items-start"
        >
          <p className="inline-flex items-center gap-2 rounded-pill bg-surface-2 px-3 py-1 text-xs font-semibold uppercase tracking-[0.14em] text-brand-green">
            <span className="size-1.5 rounded-pill bg-brand-green" aria-hidden="true" />
            DPMPTSP Jawa Tengah · Asisten AI Perizinan
          </p>

          <h1 className="mt-6 font-display text-4xl font-bold leading-[1.04] tracking-tight text-ink sm:text-5xl md:text-6xl">
            Perizinan UMKM.
            <br />
            <span className="text-primary">Tanpa bingung.</span>
          </h1>

          <p className="mt-6 max-w-prose text-base leading-relaxed text-ink-2 sm:text-lg">
            BIMA adalah asisten AI yang membantu pelaku UMKM Jawa Tengah
            memahami KBLI, menyiapkan persyaratan, dan menyelesaikan alur OSS RBA
            — langsung dari WhatsApp. Gratis, 24 jam, dalam Bahasa Indonesia.
          </p>

          <div className="mt-10 flex flex-col items-start gap-4 sm:flex-row sm:items-center">
            <Button asChild variant="amber" size="lg">
              <a
                href={WHATSAPP_URL}
                target="_blank"
                rel="noopener noreferrer"
                aria-label="Chat dengan BIMA di WhatsApp"
              >
                <MessageCircle aria-hidden="true" />
                <span>Chat dengan BIMA</span>
              </a>
            </Button>

            <a
              href="#fitur"
              className="inline-flex items-center gap-1.5 text-sm font-medium text-ink-3 underline-offset-4 transition-colors hover:text-ink hover:underline"
            >
              <ArrowDown aria-hidden="true" className="size-4" />
              Pelajari fitur
            </a>
          </div>

          <p className="mt-12 text-xs text-ink-3">
            Layanan resmi · DPMPTSP Provinsi Jawa Tengah
          </p>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, scale: 0.96 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.35, ease: "easeOut", delay: 0.1 }}
          className="relative mx-auto hidden w-full max-w-sm lg:block"
        >
          <BrandFlow className="h-auto w-full" />
        </motion.div>
      </div>
    </section>
  );
}
