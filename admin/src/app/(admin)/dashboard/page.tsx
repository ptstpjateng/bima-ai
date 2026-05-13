"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Users,
  MessageCircle,
  FileText,
  Database,
  Inbox,
  RefreshCcw,
} from "lucide-react";
import { motion } from "framer-motion";
import { Card } from "@/components/ui/card";
import { StatTile } from "@/components/dashboard/stat-tile";
import { RecentInteractions } from "@/components/dashboard/recent-interactions";
import { apiFetch, ApiError } from "@/lib/api";
import { formatDateTime, formatNumber } from "@/lib/format";
import type { DashboardStats } from "@/lib/types";

/**
 * Sprint B.2 dashboard — wires the four stat tiles to /dashboard/stats with
 * a 30s poll, count-up animation per Brand spec, and a real "Latest AI
 * Interactions" panel.
 *
 * Recent Ingestions is left as an empty-state card — Sprint C / Ingestion
 * page covers that surface.
 */
export default function DashboardPage() {
  const { data, isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ["dashboard-stats"],
    queryFn: () =>
      apiFetch<DashboardStats>("/dashboard/stats").catch((err) => {
        // Backend agent's PR may not have landed → don't break the page.
        if (err instanceof ApiError && err.status === 404) {
          return null as unknown as DashboardStats;
        }
        throw err;
      }),
    refetchInterval: 30_000,
  });

  const stats: Array<{
    label: string;
    value: number;
    icon: typeof Users;
    hint?: string;
  }> = [
    {
      label: "Total UMKM Users",
      value: data?.umkm_users ?? 0,
      icon: Users,
    },
    {
      label: "WhatsApp Messages Today",
      value: data?.whatsapp_messages_today ?? 0,
      icon: MessageCircle,
    },
    {
      label: "Active KBLI Codes",
      value: data?.active_kbli_codes ?? 0,
      icon: FileText,
      hint:
        data?.chroma_chunks != null
          ? `${formatNumber(data.chroma_chunks)} chunks indexed`
          : undefined,
    },
    {
      label: "Pending Ingestions",
      value: data?.pending_ingestions ?? 0,
      icon: Database,
    },
  ];

  return (
    <div className="space-y-8 max-w-7xl">
      <header className="flex items-end justify-between gap-4">
        <div className="space-y-1">
          <h1 className="font-display text-3xl font-semibold tracking-tight text-text-primary">
            Dashboard
          </h1>
          <p className="text-sm text-text-secondary">
            Overview of BIMA-AI operations for DPMPTSP Jawa Tengah.
          </p>
        </div>
        <div className="text-right">
          <button
            type="button"
            onClick={() => refetch()}
            className="inline-flex items-center gap-1.5 text-xs text-text-secondary hover:text-text-primary transition-colors"
            aria-label="Refresh stats"
          >
            <RefreshCcw
              className={`size-3 ${isFetching ? "animate-spin" : ""}`}
              aria-hidden
            />
            <span>Refresh</span>
          </button>
          {data?.last_updated && (
            <p className="text-[11px] text-text-muted mt-1">
              Diperbarui · {formatDateTime(data.last_updated)}
            </p>
          )}
        </div>
      </header>

      {isError && (
        <motion.div
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.2 }}
          className="rounded-card bg-brand-rose/10 text-brand-rose px-4 py-3 text-sm"
          role="alert"
        >
          Tidak bisa memuat statistik dashboard. Periksa koneksi admin-api.
        </motion.div>
      )}

      <section
        aria-label="Key metrics"
        className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4"
      >
        {stats.map((s) => (
          <StatTile key={s.label} {...s} loading={isLoading} />
        ))}
      </section>

      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <RecentInteractions />
        <EmptyPanel
          title="Recent Ingestions"
          description="Sprint C (Ingestion runtime) wires this panel."
        />
      </section>
    </div>
  );
}

function EmptyPanel({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <Card className="bg-surface-card border-0 rounded-card p-6">
      <h2 className="font-display text-lg font-medium text-text-primary mb-4">
        {title}
      </h2>
      <div className="flex flex-col items-center justify-center text-center py-10 rounded-lg bg-surface-base/40">
        <Inbox className="size-8 text-text-muted mb-3" aria-hidden />
        <p className="text-sm text-text-secondary">{description}</p>
      </div>
    </Card>
  );
}
