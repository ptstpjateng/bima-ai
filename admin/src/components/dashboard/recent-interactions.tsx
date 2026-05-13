"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { ArrowUpRight, Inbox } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ChannelBadge, MessageTypeBadge } from "@/components/admin/badges";
import { apiFetch, ApiError } from "@/lib/api";
import { relativeTime, truncate } from "@/lib/format";
import type { AiInteractionSummary, Paginated } from "@/lib/types";

/**
 * "Latest AI Interactions" panel on the dashboard.
 *
 * Calls /ai-interactions?limit=5. Each item is a card linking to the full
 * thread view. Empty state for either an empty list OR a backend that's
 * not yet wired (404 from the proxy → still renders gracefully).
 */
export function RecentInteractions() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["ai-interactions", { limit: 5 }],
    queryFn: () =>
      apiFetch<Paginated<AiInteractionSummary>>("/ai-interactions", {
        params: { limit: 5 },
      }).catch((err) => {
        // Backend agent's PR may not have landed yet → swallow 404 and show
        // empty state. Re-throw anything else.
        if (err instanceof ApiError && err.status === 404) {
          return { items: [], total: 0, page: 1, limit: 5 };
        }
        throw err;
      }),
    refetchInterval: 30_000,
  });

  return (
    <Card className="bg-surface-card border-0 rounded-card p-6">
      <header className="flex items-center justify-between mb-4">
        <h2 className="font-display text-lg font-medium text-text-primary">
          Latest AI Interactions
        </h2>
        <Link
          href="/ai-interactions"
          className="text-xs text-brand-amber hover:text-brand-amber-hover inline-flex items-center gap-1 transition-colors"
        >
          Lihat semua
          <ArrowUpRight className="size-3" aria-hidden />
        </Link>
      </header>

      {isLoading ? (
        <ul className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <li key={i}>
              <Skeleton className="h-16 w-full bg-surface-card-hover" />
            </li>
          ))}
        </ul>
      ) : error ? (
        <EmptyState
          message="Gagal memuat interaksi terbaru."
          hint="Coba muat ulang halaman."
        />
      ) : !data || data.items.length === 0 ? (
        <EmptyState
          message="Belum ada interaksi tercatat."
          hint="Pesan dari WhatsApp, Telegram, dan portal akan muncul di sini."
        />
      ) : (
        <ul className="space-y-2">
          {data.items.map((it, i) => (
            <motion.li
              key={String(it.id)}
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.2, ease: "easeOut", delay: i * 0.04 }}
            >
              <Link
                href={`/ai-interactions/${encodeURIComponent(it.session_id)}`}
                className="block rounded-lg p-3 bg-surface-base/40 hover:bg-surface-card-hover transition-colors group"
              >
                <div className="flex items-center gap-2 mb-1">
                  <ChannelBadge channel={it.channel} />
                  <MessageTypeBadge type={it.message_type} />
                  {it.intent && (
                    <span className="text-[11px] text-text-muted truncate">
                      · {it.intent}
                    </span>
                  )}
                  <span className="ml-auto text-[11px] text-text-muted">
                    {relativeTime(it.created_at)}
                  </span>
                </div>
                <p className="text-sm text-text-secondary line-clamp-2 group-hover:text-text-primary transition-colors">
                  {truncate(it.content, 200) || "(empty)"}
                </p>
              </Link>
            </motion.li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function EmptyState({ message, hint }: { message: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-10 rounded-lg bg-surface-base/40">
      <Inbox className="size-8 text-text-muted mb-3" aria-hidden />
      <p className="text-sm text-text-secondary">{message}</p>
      {hint && <p className="text-xs text-text-muted mt-1">{hint}</p>}
    </div>
  );
}
