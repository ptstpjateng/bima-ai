import { MessageCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { BrandFlow } from "@/components/landing/BrandFlow";
import { WHATSAPP_URL } from "@/lib/constants";

/**
 * EmptyState — shown when the citizen has no licenses yet. Intentional, calm,
 * and on-brand (small BrandFlow motif), with a clear WhatsApp CTA to start an
 * application. Not "AI slop" filler.
 */
export function EmptyState({
  title = "Belum ada izin",
  body = "Akun Anda belum memiliki izin yang ter-link. Ajukan izin lewat OSS RBA — BIMA akan memandu langkah demi langkah di WhatsApp.",
}: {
  title?: string;
  body?: string;
}) {
  return (
    <section className="flex flex-col items-center rounded-card border border-border bg-surface px-6 py-12 text-center sm:py-16">
      <BrandFlow className="h-28 w-28" />
      <h2 className="mt-6 font-display text-xl font-semibold text-ink">
        {title}
      </h2>
      <p className="mt-2 max-w-sm text-sm text-ink-2">{body}</p>
      <Button asChild variant="amber" size="lg" className="mt-6">
        <a
          href={WHATSAPP_URL}
          target="_blank"
          rel="noopener noreferrer"
          aria-label="Mulai ajukan izin lewat WhatsApp BIMA"
        >
          <MessageCircle aria-hidden="true" />
          <span>Mulai di WhatsApp</span>
        </a>
      </Button>
    </section>
  );
}

/**
 * ErrorCard — a calm "couldn't load" surface (warning tone, never a white
 * screen). The dashboard stub never throws, but this is the fallback if a
 * future real fetch fails.
 */
export function ErrorCard({
  message = "Tidak bisa memuat data izin saat ini.",
}: {
  message?: string;
}) {
  return (
    <section className="rounded-card border border-warning/30 bg-warning/6 p-6 sm:p-8">
      <h2 className="font-display text-lg font-semibold text-ink">
        Gagal memuat
      </h2>
      <p className="mt-2 text-sm text-ink-2">
        {message} Coba muat ulang halaman dalam beberapa saat.
      </p>
    </section>
  );
}
