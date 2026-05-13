"use client";

import { MessageCircle } from "lucide-react";
import { motion } from "framer-motion";

import { Button } from "@/components/ui/button";
import { WHATSAPP_URL } from "@/lib/constants";

/**
 * Full-viewport hero: eyebrow, display headline, sub-paragraph, primary CTA,
 * secondary text link, trust line. No background ornament — restraint per
 * Brand.md ("no particle backgrounds, no gradient blobs").
 */
export function Hero() {
  return (
    <section className="relative flex min-h-[calc(100svh-4rem)] items-center px-6 py-20">
      <motion.div
        initial={{ opacity: 0, y: 4 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.2, ease: "easeOut" }}
        className="mx-auto flex w-full max-w-3xl flex-col items-start"
      >
        <p className="text-xs font-medium uppercase tracking-[0.18em] text-brand-amber">
          DPMPTSP Jawa Tengah · Asisten AI Perizinan UMKM
        </p>

        <h1 className="mt-6 font-display text-4xl font-semibold leading-[1.05] tracking-tight text-text-primary sm:text-5xl md:text-6xl">
          Perizinan UMKM.
          <br />
          <span className="text-brand-navy-hover">Tanpa Bingung.</span>
        </h1>

        <p className="mt-6 max-w-prose text-base leading-relaxed text-text-secondary sm:text-lg">
          BIMA adalah asisten AI yang membantu pelaku UMKM Jawa Tengah memahami
          KBLI, persyaratan PB UMKU, dan alur OSS RBA — langsung dari WhatsApp.
          Gratis, 24 jam, dalam Bahasa Indonesia.
        </p>

        <div className="mt-10 flex flex-col items-start gap-4">
          <Button asChild variant="primary" size="lg">
            <a
              href={WHATSAPP_URL}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Chat dengan BIMA di WhatsApp"
            >
              <MessageCircle aria-hidden="true" />
              <span>Chat dengan BIMA di WhatsApp</span>
            </a>
          </Button>

          <a
            href="#fitur"
            className="text-sm text-text-muted underline-offset-4 transition-colors hover:text-text-secondary hover:underline"
          >
            Atau pelajari fitur di bawah ↓
          </a>
        </div>

        <p className="mt-12 text-xs text-text-muted">
          Resmi · DPMPTSP Provinsi Jawa Tengah
        </p>
      </motion.div>
    </section>
  );
}
