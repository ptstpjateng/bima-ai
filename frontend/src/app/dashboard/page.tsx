"use client";

import { AlertCircle, PlusCircle } from "lucide-react";
import Link from "next/link";
import { AppLayout } from "@/components/layout/AppLayout";
import { WelcomeCard } from "@/components/dashboard/WelcomeCard";
import { LicensingProgress } from "@/components/dashboard/LicensingProgress";
import { AiActivityFeed } from "@/components/dashboard/AiActivityFeed";
import { ChatWidget } from "@/components/dashboard/ChatWidget";
import { PermitCard } from "@/components/permits/PermitCard";
import {
  CardSkeleton,
  PermitCardSkeleton,
} from "@/components/shared/LoadingSkeleton";
import { EmptyState } from "@/components/shared/EmptyState";
import { useAuth } from "@/context/AuthContext";
import { usePermits } from "@/hooks/usePermits";
import { useAiLogs } from "@/hooks/useAiLogs";
import type { PermitApplication } from "@/types";

// LKPM applies to kecil/menengah scale permits approved > 90 days ago
function getLkpmAlert(permits: PermitApplication[]): PermitApplication | null {
  const now = Date.now();
  const ninetyDays = 90 * 24 * 60 * 60 * 1000;
  return (
    permits.find((p) => {
      if (p.status !== "approved") return false;
      if (!["kecil", "menengah"].includes(p.business_scale)) return false;
      const ref = p.approved_at ?? p.created_at;
      return now - new Date(ref).getTime() > ninetyDays;
    }) ?? null
  );
}

export default function DashboardPage() {
  const { user } = useAuth();
  const { permits, isLoading: permitsLoading } = usePermits();
  const { interactions, isLoading: logsLoading } = useAiLogs();

  const recentPermits = permits.slice(0, 3);
  const hasAiSession = interactions.length > 0;
  const lkpmAlert = getLkpmAlert(permits);

  return (
    <AppLayout>
      <div className="space-y-5">
        {/* LKPM reminder banner */}
        {lkpmAlert && (
          <div className="flex items-start gap-3 rounded-2xl border border-orange-200 bg-orange-50 p-4">
            <AlertCircle className="mt-0.5 h-5 w-5 flex-shrink-0 text-orange-500" />
            <div className="flex-1">
              <p className="text-sm font-semibold text-orange-800">
                Pengingat Laporan LKPM
              </p>
              <p className="mt-0.5 text-xs text-orange-700">
                Izin usaha{" "}
                <strong>{lkpmAlert.kbli_description}</strong> ({lkpmAlert.kbli_code})
                sudah aktif lebih dari 90 hari. Pastikan laporan kegiatan penanaman modal
                (LKPM) triwulan Anda sudah disampaikan ke OSS.
              </p>
            </div>
          </div>
        )}

        {/* Welcome banner */}
        {user ? (
          <WelcomeCard user={user} permits={permits} />
        ) : (
          <CardSkeleton />
        )}

        {/* Two-column on wider screens */}
        <div className="grid gap-5 lg:grid-cols-5">
          {/* Licensing progress (takes 2/5 width on lg) */}
          <div className="lg:col-span-2">
            <LicensingProgress
              permits={permits}
              hasAiSession={hasAiSession}
            />
          </div>

          {/* Recent permits (takes 3/5 width on lg) */}
          <div className="lg:col-span-3">
            <div className="rounded-2xl border border-gray-100 bg-white p-6 shadow-sm">
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-base font-bold text-gray-900">
                  Perizinan Terbaru
                </h2>
                <Link
                  href="/permits"
                  className="text-xs font-medium text-brand-600 hover:text-brand-700"
                >
                  Lihat semua →
                </Link>
              </div>

              {permitsLoading ? (
                <div className="space-y-3">
                  <PermitCardSkeleton />
                  <PermitCardSkeleton />
                </div>
              ) : recentPermits.length === 0 ? (
                <EmptyState
                  title="Belum ada perizinan"
                  description="Ajukan izin usaha pertama Anda sesuai panduan asisten BIMA-AI"
                  action={
                    <Link
                      href="/permits/apply"
                      className="inline-flex items-center gap-2 rounded-xl bg-brand-700 px-5 py-2.5 text-sm font-semibold text-white hover:bg-brand-800"
                    >
                      <PlusCircle className="h-4 w-4" />
                      Ajukan Izin Sekarang
                    </Link>
                  }
                />
              ) : (
                <div className="space-y-3">
                  {recentPermits.map((permit) => (
                    <PermitCard key={permit.id} permit={permit} />
                  ))}
                  {permits.length > 3 && (
                    <Link
                      href="/permits"
                      className="block rounded-xl border border-dashed border-gray-200 py-3 text-center text-sm text-gray-500 hover:border-brand-200 hover:text-brand-600 transition-colors"
                    >
                      +{permits.length - 3} perizinan lainnya
                    </Link>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>

        {/* BIMA-AI Chat Widget */}
        <ChatWidget />

        {/* AI Activity Feed */}
        <AiActivityFeed interactions={interactions} isLoading={logsLoading} />
      </div>
    </AppLayout>
  );
}
