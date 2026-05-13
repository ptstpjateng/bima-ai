"use client";

/**
 * Sumber Data — Sprint C ingestion-upload UI.
 *
 * The admin uploads an XLSX (KBLI or PB UMKU), data-pipeline ETLs it, and
 * this page shows live status (pending → processing → done/failed) plus the
 * ChromaDB chunk count once the run finishes.
 *
 * Architecture brief: BIMA admin upload flow.
 * Volume: `ingestion_uploads` shared admin-api (rw) → data-pipeline (ro).
 * Polling: 3s while any row is processing; stop otherwise.
 *
 * No new npm deps — drag-and-drop is a vanilla HTML5 dropzone, status pulse
 * is framer-motion (already in the bundle).
 */

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Database,
  FileSpreadsheet,
  Inbox,
  Loader2,
  RefreshCcw,
  Upload,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ApiError, apiFetch } from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatDateTime, formatNumber, relativeTime } from "@/lib/format";
import type {
  IngestionKind,
  IngestionSource,
  IngestionStatus,
  Paginated,
} from "@/lib/types";

type UploadKind = Extract<IngestionKind, "excel_kbli" | "excel_pb_umku">;

const PAGE_SIZE = 20;
const MAX_BYTES = 50 * 1024 * 1024;

const KIND_LABEL: Record<IngestionKind, string> = {
  excel_kbli: "KBLI",
  excel_pb_umku: "PB UMKU",
  pdf: "PDF",
  url: "URL",
  text: "Teks",
  docx: "DOCX",
};

// XLSX-ish content types accepted by both backend and this client. Falls
// back to extension check if the browser sends application/octet-stream.
const ACCEPTED_TYPES = new Set([
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.ms-excel",
  "application/octet-stream",
]);

function isLikelyXlsx(file: File): boolean {
  if (ACCEPTED_TYPES.has(file.type)) return true;
  const lower = file.name.toLowerCase();
  return lower.endsWith(".xlsx") || lower.endsWith(".xls");
}

export default function IngestionPage() {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);

  const { data, isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ["ingestion-sources", page],
    queryFn: () =>
      apiFetch<Paginated<IngestionSource>>("/ingestion", {
        params: { page, limit: PAGE_SIZE },
      }).catch((err) => {
        if (err instanceof ApiError && err.status === 404) {
          return { items: [], total: 0, page, limit: PAGE_SIZE };
        }
        throw err;
      }),
    placeholderData: (prev) => prev,
    // Auto-poll only when at least one row is in flight. Tanstack v5 returns
    // the latest data to the callback so we can decide per-tick.
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? [];
      const anyProcessing = items.some(
        (it: IngestionSource) =>
          it.status === "processing" || it.status === "pending"
      );
      return anyProcessing ? 3_000 : false;
    },
  });

  // Disable the per-row "Proses ulang" button while *any* row is processing
  // — the data-pipeline serializes ETL runs and will return 409 anyway, so
  // we may as well stop the user firing pointless requests.
  const anyProcessing = (data?.items ?? []).some(
    (it) => it.status === "processing"
  );

  const rerunMutation = useMutation({
    mutationFn: async (id: number) =>
      apiFetch<IngestionSource>(`/ingestion/${id}/rerun`, { method: "POST" }),
    onSuccess: (row) => {
      toast.success(`"${row.display_name}" diproses ulang.`);
      queryClient.invalidateQueries({ queryKey: ["ingestion-sources"] });
    },
    onError: (err: unknown) => {
      const msg =
        err instanceof ApiError
          ? err.status === 409
            ? "Pipeline ETL sedang berjalan. Coba lagi sebentar."
            : `Gagal memproses ulang (${err.status}).`
          : "Gagal memproses ulang.";
      toast.error(msg);
    },
  });

  return (
    <div className="space-y-6 max-w-7xl">
      <header className="flex items-end justify-between gap-4">
        <div className="space-y-1">
          <h1 className="font-display text-3xl font-semibold tracking-tight text-text-primary">
            Sumber Data
          </h1>
          <p className="text-sm text-text-secondary">
            Unggah berkas KBLI atau PB UMKU untuk memperkaya pengetahuan BIMA.
          </p>
        </div>
        <button
          type="button"
          onClick={() => refetch()}
          className="inline-flex items-center gap-1.5 text-xs text-text-secondary hover:text-text-primary transition-colors"
          aria-label="Muat ulang daftar"
        >
          <RefreshCcw
            className={cn("size-3", isFetching && "animate-spin")}
            aria-hidden
          />
          <span>Muat ulang</span>
        </button>
      </header>

      <UploadCard
        onUploaded={() => {
          setPage(1);
          queryClient.invalidateQueries({ queryKey: ["ingestion-sources"] });
        }}
      />

      <Card className="bg-surface-card border-0 rounded-card overflow-hidden">
        <div className="px-4 py-3 flex items-center justify-between text-xs text-text-muted">
          <span>
            {data
              ? `${formatNumber(data.total)} sumber data terdaftar`
              : isLoading
                ? "Memuat…"
                : "—"}
          </span>
          {isFetching && !isLoading && <span>Menyegarkan…</span>}
        </div>

        {isLoading ? (
          <div className="px-4 pb-4 space-y-2">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton
                key={i}
                className="h-12 w-full bg-surface-card-hover"
              />
            ))}
          </div>
        ) : isError ? (
          <EmptyRow
            title="Gagal memuat daftar sumber data"
            hint="Coba muat ulang halaman atau periksa admin-api."
          />
        ) : !data || data.items.length === 0 ? (
          <EmptyRow
            title="Belum ada sumber data."
            hint="Unggah berkas pertama Anda di atas."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="border-b border-white/5 hover:bg-transparent text-text-muted">
                <TableHead className="text-text-muted">Nama</TableHead>
                <TableHead className="text-text-muted">Jenis</TableHead>
                <TableHead className="text-text-muted">Status</TableHead>
                <TableHead className="text-text-muted text-right">
                  Chunks
                </TableHead>
                <TableHead className="text-text-muted">
                  Terakhir diproses
                </TableHead>
                <TableHead className="text-text-muted text-right">
                  Aksi
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.items.map((row) => (
                <SourceRow
                  key={row.id}
                  row={row}
                  rerunDisabled={
                    anyProcessing || rerunMutation.isPending
                  }
                  onRerun={() => rerunMutation.mutate(row.id)}
                />
              ))}
            </TableBody>
          </Table>
        )}
      </Card>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────────────
 * Upload card — drag/drop + form + submit
 * ────────────────────────────────────────────────────────────────────── */

function UploadCard({ onUploaded }: { onUploaded: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [kind, setKind] = useState<UploadKind>("excel_kbli");
  const [displayName, setDisplayName] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const uploadMutation = useMutation({
    mutationFn: async (vars: {
      file: File;
      kind: UploadKind;
      displayName: string;
    }) => {
      const form = new FormData();
      form.append("file", vars.file);
      form.append("kind", vars.kind);
      form.append("display_name", vars.displayName);
      return apiFetch<IngestionSource>("/ingestion/upload", {
        method: "POST",
        body: form,
      });
    },
    onSuccess: (row) => {
      toast.success(
        row.status === "processing"
          ? `"${row.display_name}" diunggah. Proses ETL berjalan…`
          : `"${row.display_name}" diunggah. Status: ${row.status}.`
      );
      setFile(null);
      setDisplayName("");
      if (inputRef.current) inputRef.current.value = "";
      onUploaded();
    },
    onError: (err: unknown) => {
      const msg =
        err instanceof ApiError
          ? `Gagal mengunggah (${err.status}). ${
              extractApiMessage(err.body) ?? ""
            }`.trim()
          : "Gagal mengunggah.";
      toast.error(msg);
    },
  });

  function handleFile(picked: File | null) {
    if (!picked) {
      setFile(null);
      return;
    }
    if (!isLikelyXlsx(picked)) {
      toast.error("Hanya berkas .xlsx atau .xls yang didukung.");
      return;
    }
    if (picked.size > MAX_BYTES) {
      toast.error(
        `Berkas terlalu besar (${(picked.size / (1024 * 1024)).toFixed(
          1
        )} MB). Batas: 50 MB.`
      );
      return;
    }
    setFile(picked);
    if (!displayName) {
      // Default display name from the filename minus extension; admin can edit.
      setDisplayName(picked.name.replace(/\.[^.]+$/, ""));
    }
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!file) {
      toast.error("Pilih berkas terlebih dahulu.");
      return;
    }
    const trimmed = displayName.trim();
    if (!trimmed) {
      toast.error("Nama tampilan tidak boleh kosong.");
      return;
    }
    uploadMutation.mutate({ file, kind, displayName: trimmed });
  }

  return (
    <Card className="bg-surface-card border-0 rounded-card p-4 sm:p-6">
      <form onSubmit={onSubmit} className="space-y-4">
        <div
          onDragEnter={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            const dropped = e.dataTransfer.files?.[0] ?? null;
            handleFile(dropped);
          }}
          onClick={() => inputRef.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              inputRef.current?.click();
            }
          }}
          className={cn(
            "rounded-lg border border-dashed px-6 py-10 text-center cursor-pointer transition-colors",
            dragOver
              ? "border-brand-amber bg-brand-amber/5"
              : "border-white/10 bg-surface-base/40 hover:bg-surface-base/60"
          )}
          aria-label="Seret berkas atau klik untuk memilih"
        >
          <input
            ref={inputRef}
            type="file"
            accept=".xlsx,.xls,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel"
            className="hidden"
            onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
          />
          {file ? (
            <div className="flex flex-col items-center gap-2 text-text-primary">
              <FileSpreadsheet
                className="size-8 text-brand-amber"
                aria-hidden
              />
              <div className="text-sm font-medium">{file.name}</div>
              <div className="text-xs text-text-muted">
                {(file.size / 1024).toLocaleString("id-ID", {
                  maximumFractionDigits: 0,
                })}{" "}
                KB · Klik untuk mengganti
              </div>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-2">
              <Upload className="size-8 text-text-muted" aria-hidden />
              <div className="text-sm text-text-primary">
                Seret berkas{" "}
                <span className="font-mono text-brand-amber">.xlsx</span> ke
                sini atau klik untuk memilih
              </div>
              <div className="text-xs text-text-muted">
                Maks. 50 MB · Hanya KBLI dan PB UMKU
              </div>
            </div>
          )}
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-2">
            <Label className="text-xs uppercase tracking-wider text-text-muted">
              Jenis sumber
            </Label>
            <div className="flex gap-2" role="radiogroup" aria-label="Jenis sumber">
              <KindOption
                value="excel_kbli"
                label="KBLI"
                selected={kind === "excel_kbli"}
                onSelect={() => setKind("excel_kbli")}
              />
              <KindOption
                value="excel_pb_umku"
                label="PB UMKU"
                selected={kind === "excel_pb_umku"}
                onSelect={() => setKind("excel_pb_umku")}
              />
            </div>
          </div>

          <div className="space-y-2">
            <Label
              htmlFor="display-name"
              className="text-xs uppercase tracking-wider text-text-muted"
            >
              Nama tampilan
            </Label>
            <Input
              id="display-name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="Contoh: KBLI 2025 — revisi sektor"
              maxLength={255}
              className="bg-surface-base/60"
            />
          </div>
        </div>

        <div className="flex justify-end">
          <Button
            type="submit"
            disabled={uploadMutation.isPending}
            className="bg-brand-amber text-surface-base hover:bg-brand-amber-hover"
          >
            {uploadMutation.isPending ? (
              <>
                <Loader2 className="size-4 animate-spin" aria-hidden />
                Mengunggah…
              </>
            ) : (
              <>
                <Upload className="size-4" aria-hidden />
                Unggah & proses
              </>
            )}
          </Button>
        </div>
      </form>
    </Card>
  );
}

function KindOption({
  value,
  label,
  selected,
  onSelect,
}: {
  value: UploadKind;
  label: string;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      onClick={onSelect}
      data-value={value}
      className={cn(
        "flex-1 rounded-pill px-4 py-2 text-sm transition-colors border",
        selected
          ? "bg-brand-navy text-white border-brand-navy"
          : "bg-surface-base/60 text-text-secondary border-white/10 hover:text-text-primary hover:bg-surface-card-hover"
      )}
    >
      {label}
    </button>
  );
}

/* ─────────────────────────────────────────────────────────────────────────
 * Source row + status badge
 * ────────────────────────────────────────────────────────────────────── */

function SourceRow({
  row,
  rerunDisabled,
  onRerun,
}: {
  row: IngestionSource;
  rerunDisabled: boolean;
  onRerun: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const hasError = row.status === "failed" && !!row.error;

  return (
    <>
      <TableRow className="border-b border-white/5 hover:bg-surface-card-hover/60 transition-colors">
        <TableCell className="text-text-primary">{row.display_name}</TableCell>
        <TableCell>
          <Badge
            variant="outline"
            className="border-white/10 bg-surface-base/40 text-text-secondary"
          >
            {KIND_LABEL[row.kind] ?? row.kind}
          </Badge>
        </TableCell>
        <TableCell>
          <StatusBadge status={row.status} />
        </TableCell>
        <TableCell className="text-right text-sm text-text-secondary tabular-nums">
          {formatNumber(row.chunks_indexed)}
        </TableCell>
        <TableCell className="text-sm text-text-secondary">
          {row.last_run_at ? (
            <span title={formatDateTime(row.last_run_at)}>
              {relativeTime(row.last_run_at)}
            </span>
          ) : (
            <span className="text-text-muted">—</span>
          )}
        </TableCell>
        <TableCell className="text-right">
          <div className="inline-flex items-center gap-1">
            {hasError && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setExpanded((v) => !v)}
                className="text-text-secondary hover:text-text-primary"
                aria-expanded={expanded}
              >
                {expanded ? (
                  <ChevronUp className="size-3.5" aria-hidden />
                ) : (
                  <ChevronDown className="size-3.5" aria-hidden />
                )}
                Lihat error
              </Button>
            )}
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={onRerun}
              disabled={rerunDisabled || !row.object_key}
              title={
                !row.object_key
                  ? "Tidak ada berkas yang tersimpan untuk diproses ulang."
                  : undefined
              }
            >
              <RefreshCcw className="size-3.5" aria-hidden />
              Proses ulang
            </Button>
          </div>
        </TableCell>
      </TableRow>
      <AnimatePresence>
        {expanded && hasError && (
          <motion.tr
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          >
            <TableCell colSpan={6} className="bg-surface-base/40 px-4 py-3">
              <div className="flex items-start gap-2 text-xs text-brand-rose">
                <AlertCircle className="size-3.5 mt-0.5" aria-hidden />
                <pre className="whitespace-pre-wrap font-mono text-[11px] text-text-secondary leading-snug">
                  {row.error}
                </pre>
              </div>
            </TableCell>
          </motion.tr>
        )}
      </AnimatePresence>
    </>
  );
}

function StatusBadge({ status }: { status: IngestionStatus }) {
  if (status === "processing") {
    return (
      <motion.span
        // Pulse only on the live state — done/failed/pending stay still.
        animate={{ opacity: [0.65, 1, 0.65] }}
        transition={{ duration: 1.6, repeat: Infinity, ease: "easeInOut" }}
        className="inline-flex items-center gap-1.5 rounded-pill bg-brand-amber/15 px-2.5 py-0.5 text-xs font-medium text-brand-amber"
      >
        <Loader2 className="size-3 animate-spin" aria-hidden />
        Diproses
      </motion.span>
    );
  }
  if (status === "done") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-pill bg-emerald-500/15 px-2.5 py-0.5 text-xs font-medium text-emerald-300">
        <CheckCircle2 className="size-3" aria-hidden />
        Selesai
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-pill bg-rose-500/15 px-2.5 py-0.5 text-xs font-medium text-rose-300">
        <AlertCircle className="size-3" aria-hidden />
        Gagal
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-pill bg-surface-base/60 px-2.5 py-0.5 text-xs font-medium text-text-muted">
      <Database className="size-3" aria-hidden />
      Menunggu
    </span>
  );
}

/* ─────────────────────────────────────────────────────────────────────────
 * Helpers
 * ────────────────────────────────────────────────────────────────────── */

function EmptyRow({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-16">
      <Inbox className="size-8 text-text-muted mb-3" aria-hidden />
      <p className="text-sm text-text-secondary">{title}</p>
      {hint && <p className="text-xs text-text-muted mt-1">{hint}</p>}
    </div>
  );
}

function extractApiMessage(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  const obj = body as Record<string, unknown>;
  if (typeof obj.detail === "string") return obj.detail;
  if (typeof obj.message === "string") return obj.message;
  return null;
}
