"use client";

import { Users, MessageCircle, FileText, Inbox, Database } from "lucide-react";
import { Card } from "@/components/ui/card";
import { StatTile } from "@/components/dashboard/stat-tile";

/**
 * Phase 1 dashboard — placeholders only. Phase 2 wires the four stat tiles
 * to admin-api endpoints and the two panels to real list queries.
 */
export default function DashboardPage() {
  const stats: Array<{
    label: string;
    value: number;
    icon: typeof Users;
  }> = [
    { label: "Total UMKM Users", value: 0, icon: Users },
    { label: "WhatsApp Messages Today", value: 0, icon: MessageCircle },
    { label: "Active KBLI Codes", value: 0, icon: FileText },
    { label: "Pending Ingestions", value: 0, icon: Database },
  ];

  return (
    <div className="space-y-8 max-w-7xl">
      <header className="space-y-1">
        <h1 className="font-display text-3xl font-semibold tracking-tight text-text-primary">
          Dashboard
        </h1>
        <p className="text-sm text-text-secondary">
          Overview of BIMA-AI operations for DPMPTSP Jawa Tengah.
        </p>
      </header>

      <section
        aria-label="Key metrics"
        className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4"
      >
        {stats.map((s) => (
          <StatTile key={s.label} {...s} />
        ))}
      </section>

      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <EmptyPanel
          title="Latest AI Interactions"
          description="No data yet — Phase 2 will stream the AI thread viewer."
        />
        <EmptyPanel
          title="Recent Ingestions"
          description="No data yet — Phase 2 will list ingestion sources."
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
