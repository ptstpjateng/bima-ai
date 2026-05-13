"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { ChevronLeft, ChevronRight, Inbox, Search, X } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { RiskBadge, SektorBadge } from "@/components/admin/badges";
import { apiFetch, ApiError } from "@/lib/api";
import { useDebounced } from "@/lib/hooks";
import { cn } from "@/lib/utils";
import { truncate } from "@/lib/format";
import type { KbliSummary, Paginated } from "@/lib/types";

const PAGE_SIZE = 20;

// Static filter chips — values match what BIMA's RAG dataset uses (see
// Progress Log #5: ETL run produced sektor + tingkat_risiko per row).
const SEKTOR_OPTIONS = [
  "Pertanian",
  "Industri",
  "Konstruksi",
  "Perdagangan",
  "Jasa",
  "Transportasi",
  "Pariwisata",
];

const RISIKO_OPTIONS = [
  "Rendah",
  "Menengah Rendah",
  "Menengah Tinggi",
  "Tinggi",
];

export default function KbliSearchPage() {
  const [searchInput, setSearchInput] = useState("");
  const [sektor, setSektor] = useState<string[]>([]);
  const [risiko, setRisiko] = useState<string[]>([]);
  const [page, setPage] = useState(1);

  const search = useDebounced(searchInput, 300);

  // Derive-on-render reset: see ai-interactions/page.tsx for the same pattern
  // (avoids the react-hooks/set-state-in-effect lint).
  const filterKey = `${search}|${sektor.join(",")}|${risiko.join(",")}`;
  const [prevFilterKey, setPrevFilterKey] = useState(filterKey);
  let pageEffective = page;
  if (filterKey !== prevFilterKey) {
    setPrevFilterKey(filterKey);
    setPage(1);
    pageEffective = 1;
  }

  const params = {
    q: search || undefined,
    // Send each as a comma-joined string; backend can accept either repeated
    // or comma-joined depending on FastAPI signature.
    sektor: sektor.length ? sektor.join(",") : undefined,
    tingkat_risiko: risiko.length ? risiko.join(",") : undefined,
    page: pageEffective,
    limit: PAGE_SIZE,
  };

  const { data, isLoading, isError, isFetching } = useQuery({
    queryKey: ["kbli", params],
    queryFn: () =>
      apiFetch<Paginated<KbliSummary>>("/kbli", { params }).catch((err) => {
        if (err instanceof ApiError && err.status === 404) {
          return { items: [], total: 0, page, limit: PAGE_SIZE };
        }
        throw err;
      }),
    placeholderData: (prev) => prev,
  });

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  function toggle(arr: string[], setter: (v: string[]) => void, v: string) {
    setter(arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);
  }

  return (
    <div className="space-y-6 max-w-7xl">
      <header className="space-y-1">
        <h1 className="font-display text-3xl font-semibold tracking-tight text-text-primary">
          KBLI
        </h1>
        <p className="text-sm text-text-secondary">
          Pencarian kode klasifikasi baku lapangan usaha Indonesia (5 digit) dan
          jenis perizinan berusaha (PB-UMKU) yang menyertainya.
        </p>
      </header>

      <Card className="bg-surface-card border-0 rounded-card p-4 sm:p-6 space-y-4">
        <div className="relative">
          <Search
            className="absolute left-3 top-1/2 -translate-y-1/2 size-4 text-text-muted pointer-events-none"
            aria-hidden
          />
          <Input
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            placeholder="Cari kode KBLI atau nama usaha…"
            className="pl-9 h-11 text-base bg-surface-base/60"
            aria-label="Cari KBLI"
          />
          {searchInput && (
            <button
              type="button"
              onClick={() => setSearchInput("")}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-text-muted hover:text-text-primary p-1"
              aria-label="Hapus pencarian"
            >
              <X className="size-4" aria-hidden />
            </button>
          )}
        </div>

        <div className="space-y-2">
          <div className="text-[11px] uppercase tracking-wider text-text-muted">
            Sektor
          </div>
          <div className="flex flex-wrap gap-1.5">
            {SEKTOR_OPTIONS.map((s) => (
              <Chip
                key={s}
                label={s}
                active={sektor.includes(s)}
                onClick={() => toggle(sektor, setSektor, s)}
              />
            ))}
            {sektor.length > 0 && (
              <button
                type="button"
                onClick={() => setSektor([])}
                className="text-[11px] text-text-muted hover:text-text-primary px-2 py-1"
              >
                Reset
              </button>
            )}
          </div>
        </div>

        <div className="space-y-2">
          <div className="text-[11px] uppercase tracking-wider text-text-muted">
            Tingkat risiko
          </div>
          <div className="flex flex-wrap gap-1.5">
            {RISIKO_OPTIONS.map((r) => (
              <Chip
                key={r}
                label={r}
                active={risiko.includes(r)}
                onClick={() => toggle(risiko, setRisiko, r)}
              />
            ))}
            {risiko.length > 0 && (
              <button
                type="button"
                onClick={() => setRisiko([])}
                className="text-[11px] text-text-muted hover:text-text-primary px-2 py-1"
              >
                Reset
              </button>
            )}
          </div>
        </div>
      </Card>

      <Card className="bg-surface-card border-0 rounded-card overflow-hidden">
        <div className="px-4 py-3 flex items-center justify-between text-xs text-text-muted">
          <span>
            {data
              ? `${data.total.toLocaleString("id-ID")} kode ditemukan`
              : isLoading
                ? "Memuat…"
                : "—"}
          </span>
          {isFetching && !isLoading && <span>Menyegarkan…</span>}
        </div>

        {isLoading ? (
          <div className="px-4 pb-4 space-y-2">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton
                key={i}
                className="h-12 w-full bg-surface-card-hover"
              />
            ))}
          </div>
        ) : isError ? (
          <EmptyRow
            title="Gagal memuat data KBLI"
            hint="Coba muat ulang halaman atau periksa admin-api."
          />
        ) : !data || data.items.length === 0 ? (
          <EmptyRow
            title="Tidak ada kode KBLI yang cocok"
            hint="Coba kata kunci lain atau hapus filter sektor/risiko."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="border-b border-white/5 hover:bg-transparent text-text-muted">
                <TableHead className="text-text-muted">Kode</TableHead>
                <TableHead className="text-text-muted">Judul Usaha</TableHead>
                <TableHead className="text-text-muted">Sektor</TableHead>
                <TableHead className="text-text-muted">Risiko</TableHead>
                <TableHead className="text-text-muted">Skala</TableHead>
                <TableHead className="text-text-muted">Perizinan</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.items.map((it, i) => (
                <motion.tr
                  key={String(it.id) + it.kode_kbli}
                  initial={{ opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{
                    duration: 0.2,
                    ease: "easeOut",
                    delay: Math.min(i, 12) * 0.04,
                  }}
                  className="border-b border-white/5 hover:bg-surface-card-hover/60 transition-colors"
                >
                  <TableCell>
                    <Link
                      href={`/kbli/${encodeURIComponent(it.kode_kbli)}`}
                      className="font-mono text-brand-amber hover:text-brand-amber-hover transition-colors"
                    >
                      {it.kode_kbli}
                    </Link>
                  </TableCell>
                  <TableCell className="max-w-md">
                    <Link
                      href={`/kbli/${encodeURIComponent(it.kode_kbli)}`}
                      className="text-text-primary hover:text-brand-amber transition-colors line-clamp-2 block"
                    >
                      {it.judul_kbli}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <SektorBadge sektor={it.sektor} />
                  </TableCell>
                  <TableCell>
                    <RiskBadge risk={it.tingkat_risiko} />
                  </TableCell>
                  <TableCell className="text-sm text-text-secondary">
                    {it.skala_usaha ?? (
                      <span className="text-text-muted">—</span>
                    )}
                  </TableCell>
                  <TableCell className="max-w-[260px]">
                    {it.perizinan_berusaha ? (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span className="text-sm text-text-secondary block truncate">
                            {truncate(it.perizinan_berusaha, 60)}
                          </span>
                        </TooltipTrigger>
                        <TooltipContent className="max-w-md whitespace-pre-wrap">
                          {it.perizinan_berusaha}
                        </TooltipContent>
                      </Tooltip>
                    ) : (
                      <span className="text-text-muted">—</span>
                    )}
                  </TableCell>
                </motion.tr>
              ))}
            </TableBody>
          </Table>
        )}

        {data && data.items.length > 0 && (
          <div className="px-4 py-3 flex items-center justify-between text-xs text-text-muted">
            <span>
              Halaman {data.page} dari {totalPages.toLocaleString("id-ID")}
            </span>
            <div className="flex items-center gap-2">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                className="h-7 text-text-secondary hover:text-text-primary"
              >
                <ChevronLeft className="size-3.5" aria-hidden />
                Prev
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                className="h-7 text-text-secondary hover:text-text-primary"
              >
                Next
                <ChevronRight className="size-3.5" aria-hidden />
              </Button>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}

function Chip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "rounded-pill px-3 py-1 text-xs transition-colors",
        active
          ? "bg-brand-navy text-white"
          : "bg-surface-base/60 text-text-secondary hover:bg-surface-card-hover hover:text-text-primary"
      )}
    >
      {label}
    </button>
  );
}

function EmptyRow({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-16">
      <Inbox className="size-8 text-text-muted mb-3" aria-hidden />
      <p className="text-sm text-text-secondary">{title}</p>
      {hint && <p className="text-xs text-text-muted mt-1">{hint}</p>}
    </div>
  );
}
