"use client";

import { MessageCircle } from "lucide-react";
import { motion } from "framer-motion";

import { Button } from "@/components/ui/button";
import { WHATSAPP_URL } from "@/lib/constants";

/**
 * Transparent top bar — brand wordmark left, single WhatsApp CTA right.
 * No nav links: there is nowhere else to navigate.
 */
export function TopBar() {
  return (
    <motion.header
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
      className="sticky top-0 z-50 w-full bg-surface-base/80 backdrop-blur-md"
    >
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-6">
        <span className="font-display text-xl font-semibold tracking-tight text-text-primary">
          BIMA
        </span>
        <Button asChild variant="amber" size="sm">
          <a
            href={WHATSAPP_URL}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Mulai percakapan dengan BIMA di WhatsApp"
          >
            <MessageCircle aria-hidden="true" />
            <span>Mulai di WhatsApp</span>
          </a>
        </Button>
      </div>
    </motion.header>
  );
}
