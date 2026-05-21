"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  ArrowRight,
  Briefcase,
  CloudOff,
  Inbox as InboxIcon,
  RefreshCcw,
  Search,
  UserRound,
  X,
} from "lucide-react";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { CaseStatusBadge } from "@/components/case/case-status-badge";
import { SektorBadge } from "@/components/admin/badges";
import { SopProgress, SopDaysPill } from "@/components/inbox/sop-progress";
import { apiFetch, ApiError } from "@/lib/api";
import { useDebounced } from "@/lib/hooks";
import type { InboxCase, InboxResponse } from "@/lib/inbox-types";

/**
 * Officer inbox — interactive client island.
 *
 * Fetches the urgency-ranked queue from `GET /inbox` via the same-origin
 * proxy. The list arrives already sorted (server-side ranker), so this
 * component never re-sorts — it just renders. An optional desk filter narrows
 * the queue; it is debounced so typing doesn't hammer the API.
 *
 * Every row leads with the SOP-as-motivator surface: a calm progress bar +
 * encouraging copy. SOP is a motivator, not a whip — see `SopProgress`.
 */
export function InboxView() {
  const [deskInput, setDeskInput] = useState("");
  const desk = useDebounced(deskInput.trim(), 300);

  const params: Record<string, string | undefined> = {};
  if (desk) params.desk = desk;

  const { data, isLoading, isError, error, isFetching, refetch } =
    useQuery<InboxResponse>({
      queryKey: ["officer-inbox", desk || "all"],
      queryFn: () => apiFetch<InboxResponse>("/inbox", { params }),
      retry: false,
      // The queue shifts slowly (days-open changes by the day); a 60s stale
      // window keeps it snappy without going stale on the officer.
      staleTime: 60_000,
    });

  const cases = data?.cases ?? [];

  return (
    <div className="space-y-6 max-w-6xl">
      {/* Header */}
      <header className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1">
          <h1 className="font-display text-3xl font-semibold tracking-tight text-text-primary">
            Kotak Masuk
          </h1>
          <p className="text-sm text-text-secondary">
            Semua berkas aktif Anda, diurutkan dari yang paling mendesak. Tenang
            saja — SOP di sini adalah pengingat, bukan tekanan.
          </p>
        </div>
        <button
          type="button"
          onClick={() => refetch()}
          className="inline-flex shrink-0 items-center gap-1.5 self-start text-xs text-text-secondary transition-colors hover:text-text-primary"
          aria-label="Segarkan kotak masuk"
        >
          <RefreshCcw
            className={`size-3.5 ${isFetching ? "animate-spin" : ""}`}
            aria-hidden
          />
          <span>Segarkan</span>
        </button>
      </header>

      {/* Desk filter */}
      <Card className="bg-surface-card border-0 rounded-card p-4 sm:p-5">
        <div className="relative">
          <Search
            className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-text-muted"
            aria-hidden
          />
          <Input
            value={deskInput}
            onChange={(e) => setDeskInput(e.target.value)}
            placeholder="Saring berdasarkan meja / desk…"
            className="h-11 bg-surface-base/60 pl-9 text-base"
            aria-label="Saring berdasarkan meja"
          />
          {deskInput && (
            <button
              type="button"
              onClick={() => setDeskInput("")}
              className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-text-muted hover:text-text-primary"
              aria-label="Hapus saringan meja"
            >
              <X className="size-4" aria-hidden />
            </button>
          )}
        </div>
        <p className="mt-2 text-[11px] text-text-muted">
          Kosongkan untuk melihat seluruh berkas aktif lintas meja.
        </p>
      </Card>

      {/* List */}
      {isLoading ? (
        <InboxSkeleton />
      ) : isError ? (
        <InboxError error={error} onRetry={() => refetch()} />
      ) : cases.length === 0 ? (
        <EmptyInbox hasFilter={Boolean(desk)} />
      ) : (
        <section aria-label="Daftar berkas" className="space-y-3">
          <div className="flex items-baseline justify-between gap-3 px-1">
            <span className="text-xs text-text-muted">
              {cases.length.toLocaleString("id-ID")} berkas aktif
              {desk ? ` · meja “${desk}”` : ""}
            </span>
            {isFetching && !isLoading && (
              <span className="text-xs text-text-muted">Menyegarkan…</span>
            )}
          </div>

          <div className="grid grid-cols-1 gap-3">
            {cases.map((c, i) => (
              <InboxRow key={c.ticket} item={c} index={i} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Row                                                                         */
/* -------------------------------------------------------------------------- */

function InboxRow({ item, index }: { item: InboxCase; index: number }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        duration: 0.22,
        ease: "easeOut",
        delay: Math.min(index, 12) * 0.035,
      }}
    >
      <Link
        href={`/case/${encodeURIComponent(item.ticket)}`}
        className="group block rounded-card bg-surface-card p-4 transition-colors hover:bg-surface-card-hover sm:p-5"
        aria-label={`Buka berkas ${item.ticket}`}
      >
        <div className="flex flex-col gap-4 md:flex-row md:items-stretch md:gap-6">
          {/* Left: ticket + license + meta */}
          <div className="min-w-0 flex-1 space-y-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-lg tracking-tight text-brand-amber">
                {item.ticket}
              </span>
              <CaseStatusBadge status={item.status ?? "—"} />
            </div>

            <h2 className="line-clamp-2 font-display text-base font-medium leading-snug text-text-primary">
              {item.license_name ?? "Perizinan tanpa nama"}
            </h2>

            <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-text-secondary">
              <span className="inline-flex items-center gap-1.5">
                <UserRound className="size-3.5 text-text-muted" aria-hidden />
                {item.applicant_name ?? "—"}
              </span>
              <span className="inline-flex items-center gap-1.5">
                <Briefcase className="size-3.5 text-text-muted" aria-hidden />
                {item.current_desk ?? "Meja tidak diketahui"}
              </span>
              {item.sector_name && <SektorBadge sektor={item.sector_name} />}
            </div>
          </div>

          {/* Right: SOP-as-motivator surface */}
          <div className="flex flex-col justify-between gap-2.5 md:w-64 md:shrink-0">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[10px] uppercase tracking-widest text-text-muted">
                Progres SOP
              </span>
              <SopDaysPill
                daysOpen={item.days_open}
                sopDays={item.sop_days}
                sopState={item.sop_state}
              />
            </div>

            <SopProgress
              daysOpen={item.days_open}
              sopDays={item.sop_days}
              sopState={item.sop_state}
            />

            <span className="inline-flex items-center gap-1 self-end text-[11px] text-text-muted transition-colors group-hover:text-brand-amber">
              Tinjau berkas
              <ArrowRight
                className="size-3 transition-transform group-hover:translate-x-0.5"
                aria-hidden
              />
            </span>
          </div>
        </div>
      </Link>
    </motion.div>
  );
}

/* -------------------------------------------------------------------------- */
/* States                                                                      */
/* -------------------------------------------------------------------------- */

function InboxSkeleton() {
  return (
    <div className="space-y-3" aria-busy="true">
      {Array.from({ length: 5 }).map((_, i) => (
        <Skeleton
          key={i}
          className="h-32 w-full rounded-card bg-surface-card sm:h-28"
        />
      ))}
    </div>
  );
}

function EmptyInbox({ hasFilter }: { hasFilter: boolean }) {
  return (
    <Card className="bg-surface-card border-0 rounded-card p-10 text-center sm:p-12">
      <div className="mx-auto mb-4 inline-flex rounded-full bg-brand-emerald/12 p-3">
        <InboxIcon className="size-7 text-brand-emerald" aria-hidden />
      </div>
      <h2 className="font-display text-xl font-medium text-text-primary">
        {hasFilter ? "Tidak ada berkas di meja itu" : "Kotak masuk bersih"}
      </h2>
      <p className="mx-auto mt-1.5 max-w-md text-sm text-text-secondary">
        {hasFilter
          ? "Coba ubah atau hapus saringan meja untuk melihat berkas lainnya."
          : "Tidak ada berkas aktif yang menunggu saat ini. Kerja bagus — nikmati momennya."}
      </p>
    </Card>
  );
}

function InboxError({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry: () => void;
}) {
  const status = error instanceof ApiError ? error.status : 0;
  const offline = status === 502 || status === 503 || status === 504;
  return (
    <Card className="bg-surface-card border-0 rounded-card p-10 text-center sm:p-12">
      <div className="mx-auto mb-4 inline-flex rounded-full bg-brand-rose/15 p-3">
        <CloudOff className="size-6 text-brand-rose" aria-hidden />
      </div>
      <h2 className="font-display text-xl font-medium text-text-primary">
        {offline
          ? "Integrasi SIAP sedang tidak tersedia"
          : "Tidak bisa memuat kotak masuk"}
      </h2>
      <p className="mx-auto mt-1.5 mb-6 max-w-md text-sm text-text-secondary">
        {offline
          ? "Layanan data SIAP tidak merespons. Coba beberapa saat lagi."
          : error instanceof Error
            ? error.message
            : "Terjadi kesalahan tak terduga."}
      </p>
      <Button
        type="button"
        onClick={onRetry}
        className="bg-brand-navy text-white hover:bg-brand-navy-hover"
      >
        <RefreshCcw className="mr-2 size-4" aria-hidden />
        Coba lagi
      </Button>
    </Card>
  );
}
