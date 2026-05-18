"use client";

import { MessageCircle } from "lucide-react";
import { motion } from "framer-motion";

import { Button } from "@/components/ui/button";
import { WHATSAPP_URL } from "@/lib/constants";

/**
 * Full-width band with a subtle navy tint, centered prompt + WhatsApp button.
 * The "ready to start" beat right before the footer.
 */
export function ClosingCta() {
  return (
    <section className="px-4 py-16 sm:px-6 sm:py-20 md:py-24 lg:px-8">
      <motion.div
        initial={{ opacity: 0, y: 4 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, margin: "-80px" }}
        transition={{ duration: 0.2, ease: "easeOut" }}
        className="mx-auto flex max-w-4xl flex-col items-center gap-6 rounded-card bg-brand-navy/10 px-6 py-12 text-center sm:gap-8 sm:px-12 sm:py-14"
      >
        <h2 className="font-display text-xl font-semibold tracking-tight text-text-primary sm:text-2xl md:text-3xl">
          Siap mulai? Chat BIMA sekarang.
        </h2>
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
      </motion.div>
    </section>
  );
}
