"use client";

import { Filter, PlusCircle } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { AppLayout } from "@/components/layout/AppLayout";
import { PermitCard } from "@/components/permits/PermitCard";
import { PermitCardSkeleton } from "@/components/shared/LoadingSkeleton";
import { EmptyState } from "@/components/shared/EmptyState";
import { usePermits } from "@/hooks/usePermits";
import type { PermitStatus } from "@/types";
import { STATUS_LABELS } from "@/lib/utils";

const FILTER_OPTIONS: Array<{ value: PermitStatus | "all"; label: string }> = [
  { value: "all", label: "Semua" },
  { value: "submitted", label: STATUS_LABELS["submitted"] },
  { value: "under_review", label: STATUS_LABELS["under_review"] },
  { value: "additional_docs_required", label: STATUS_LABELS["additional_docs_required"] },
  { value: "approved", label: STATUS_LABELS["approved"] },
  { value: "rejected", label: STATUS_LABELS["rejected"] },
];

export default function PermitsPage() {
  const { permits, isLoading } = usePermits();
  const [filter, setFilter] = useState<PermitStatus | "all">("all");

  const filtered =
    filter === "all"
      ? permits
      : permits.filter((p) => p.status === filter);

  return (
    <AppLayout>
      <div className="space-y-5">
        {/* Header */}
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-xl font-bold text-gray-900">Perizinan Saya</h1>
            <p className="text-sm text-gray-500">
              {isLoading
                ? "Memuat..."
                : `${permits.length} permohonan ditemukan`}
            </p>
          </div>
          <Link
            href="/permits/apply"
            className="inline-flex flex-shrink-0 items-center gap-2 rounded-xl bg-brand-700 px-4 py-2.5 text-sm font-semibold text-white hover:bg-brand-800"
          >
            <PlusCircle className="h-4 w-4" />
            <span className="hidden sm:inline">Ajukan Baru</span>
          </Link>
        </div>

        {/* Filter pills */}
        <div className="flex items-center gap-1 overflow-x-auto pb-1">
          <Filter className="mr-1 h-4 w-4 flex-shrink-0 text-gray-400" />
          {FILTER_OPTIONS.map(({ value, label }) => {
            const count =
              value === "all"
                ? permits.length
                : permits.filter((p) => p.status === value).length;
            return (
              <button
                key={value}
                onClick={() => setFilter(value)}
                className={`inline-flex flex-shrink-0 items-center gap-1.5 rounded-full border px-3.5 py-1.5 text-xs font-medium transition-colors ${
                  filter === value
                    ? "border-brand-400 bg-brand-50 text-brand-700"
                    : "border-gray-200 bg-white text-gray-600 hover:border-gray-300"
                }`}
              >
                {label}
                {count > 0 && (
                  <span
                    className={`rounded-full px-1.5 py-0.5 text-[10px] font-bold ${
                      filter === value
                        ? "bg-brand-100 text-brand-700"
                        : "bg-gray-100 text-gray-500"
                    }`}
                  >
                    {count}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        {/* Content */}
        {isLoading ? (
          <div className="space-y-4">
            {[1, 2, 3].map((i) => (
              <PermitCardSkeleton key={i} />
            ))}
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState
            title={
              filter === "all"
                ? "Belum ada permohonan"
                : `Tidak ada permohonan dengan status "${STATUS_LABELS[filter as PermitStatus]}"`
            }
            description={
              filter === "all"
                ? "Mulai ajukan izin usaha pertama Anda. Asisten BIMA-AI akan membantu prosesnya."
                : "Coba filter lainnya atau ajukan permohonan baru."
            }
            action={
              filter === "all" ? (
                <Link
                  href="/permits/apply"
                  className="inline-flex items-center gap-2 rounded-xl bg-brand-700 px-5 py-2.5 text-sm font-semibold text-white hover:bg-brand-800"
                >
                  <PlusCircle className="h-4 w-4" />
                  Ajukan Izin Baru
                </Link>
              ) : undefined
            }
          />
        ) : (
          <div className="space-y-4">
            {filtered.map((permit) => (
              <PermitCard key={permit.id} permit={permit} />
            ))}
          </div>
        )}
      </div>
    </AppLayout>
  );
}
