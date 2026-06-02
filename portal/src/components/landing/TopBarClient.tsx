"use client";

import { MessageCircle, UserRound } from "lucide-react";
import { motion } from "framer-motion";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { ThemeToggle } from "@/components/ThemeToggle";
import { WHATSAPP_URL } from "@/lib/constants";
import type { SsoUser } from "@/lib/auth-client";

type TopBarClientProps = {
  /** Server-rendered initial auth state. Null = anonymous. */
  initialUser: SsoUser | null;
};

/**
 * Transparent top bar — brand wordmark left, auth-aware actions right.
 *
 * Two modes:
 *   - Anonymous: WhatsApp CTA + "Masuk" link
 *   - Authenticated: user pill (first name) linking to /me, plus the
 *     same WhatsApp CTA so signed-in users can still jump to chat
 *
 * Why pass user via prop instead of re-fetching client-side? Two reasons:
 *   1. Avoids the "Login button blip" — Server Component pre-renders the
 *      correct state.
 *   2. Cuts a client-side fetch from every page load.
 */
export function TopBarClient({ initialUser }: TopBarClientProps) {
  const firstName =
    initialUser?.name?.split(" ")[0] ?? initialUser?.name ?? null;

  return (
    <motion.header
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
      className="sticky top-0 z-50 w-full border-b border-border/60 bg-bg/80 backdrop-blur-md"
    >
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4 sm:px-6 lg:px-8">
        <Link
          href="/"
          className="font-display text-xl font-semibold tracking-tight text-ink"
        >
          BIMA
        </Link>

        <div className="flex items-center gap-1 sm:gap-2">
          <ThemeToggle />

          {initialUser ? (
            <Button asChild variant="ghost" size="sm" className="hidden sm:inline-flex">
              <Link href="/me" aria-label={`Lihat akun ${firstName}`}>
                <UserRound aria-hidden="true" />
                <span className="max-w-[10ch] truncate">{firstName}</span>
              </Link>
            </Button>
          ) : (
            <Button asChild variant="ghost" size="sm" className="hidden sm:inline-flex">
              <Link href="/login" aria-label="Masuk dengan NIK">
                <UserRound aria-hidden="true" />
                <span>Masuk</span>
              </Link>
            </Button>
          )}

          <Button asChild variant="amber" size="sm">
            <a
              href={WHATSAPP_URL}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Mulai percakapan dengan BIMA di WhatsApp"
            >
              <MessageCircle aria-hidden="true" />
              <span className="hidden sm:inline">Mulai di WhatsApp</span>
              <span className="sm:hidden">WhatsApp</span>
            </a>
          </Button>
        </div>
      </div>
    </motion.header>
  );
}
