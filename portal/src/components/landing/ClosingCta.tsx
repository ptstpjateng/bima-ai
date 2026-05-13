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
    <section className="px-6 py-20 sm:py-24">
      <motion.div
        initial={{ opacity: 0, y: 4 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, margin: "-80px" }}
        transition={{ duration: 0.2, ease: "easeOut" }}
        className="mx-auto flex max-w-4xl flex-col items-center gap-8 rounded-card bg-brand-navy/10 px-8 py-14 text-center sm:px-12"
      >
        <h2 className="font-display text-2xl font-semibold tracking-tight text-text-primary sm:text-3xl">
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
