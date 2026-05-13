"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  ChevronLeft,
  ChevronRight,
  Inbox,
  MessageCircle,
  Search,
} from "lucide-react";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
import {
  ChannelBadge,
  MessageTypeBadge,
  ResponseTimeBadge,
} from "@/components/admin/badges";
import { apiFetch, ApiError } from "@/lib/api";
import { useDebounced } from "@/lib/hooks";
import { relativeTime, truncate } from "@/lib/format";
import type { AiInteractionSummary, Paginated } from "@/lib/types";

const PAGE_SIZE = 20;

export default function AiInteractionsPage() {
  const [channel, setChannel] = useState<string>("all");
  const [messageType, setMessageType] = useState<string>("all");
  const [todayOnly, setTodayOnly] = useState(false);
  const [searchInput, setSearchInput] = useState("");
  const [page, setPage] = useState(1);

  const search = useDebounced(searchInput, 300);

  // Derive-on-render: any time a filter changes, the previousFilterKey we
  // remember in state diverges from the current key — that's the cue to reset
  // page back to 1. Tracking via state (not effect) avoids the cascading-
  // render lint and keeps the URL/page in sync immediately.
  const filterKey = `${channel}|${messageType}|${todayOnly}|${search}`;
  const [prevFilterKey, setPrevFilterKey] = useState(filterKey);
  let pageEffective = page;
  if (filterKey !== prevFilterKey) {
    setPrevFilterKey(filterKey);
    setPage(1);
    pageEffective = 1;
  }

  const since = todayOnly
    ? (() => {
        const d = new Date();
        d.setHours(0, 0, 0, 0);
        return d.toISOString();
      })()
    : undefined;

  const params = {
    channel: channel === "all" ? undefined : channel,
    message_type: messageType === "all" ? undefined : messageType,
    since,
    q: search || undefined,
    page: pageEffective,
    limit: PAGE_SIZE,
  };

  const { data, isLoading, isError, isFetching } = useQuery({
    queryKey: ["ai-interactions", params],
    queryFn: () =>
      apiFetch<Paginated<AiInteractionSummary>>("/ai-interactions", {
        params,
      }).catch((err) => {
        if (err instanceof ApiError && err.status === 404) {
          return { items: [], total: 0, page, limit: PAGE_SIZE };
        }
        throw err;
      }),
    placeholderData: (prev) => prev,
  });

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  return (
    <div className="space-y-6 max-w-7xl">
      <header className="space-y-1">
        <h1 className="font-display text-3xl font-semibold tracking-tight text-text-primary">
          AI Interactions
        </h1>
        <p className="text-sm text-text-secondary">
          Riwayat percakapan BIMA di semua kanal — WhatsApp, Telegram, dan portal.
        </p>
      </header>

      <Card className="bg-surface-card border-0 rounded-card p-4 sm:p-6 space-y-4">
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-[1fr_180px_180px_auto] gap-3 items-end">
          <div>
            <label
              htmlFor="ai-search"
              className="text-xs text-text-muted block mb-1.5"
            >
              Pencarian
            </label>
            <div className="relative">
              <Search
                className="absolute left-2.5 top-1/2 -translate-y-1/2 size-4 text-text-muted pointer-events-none"
                aria-hidden
              />
              <Input
                id="ai-search"
                placeholder="Cari isi pesan, intent, session id…"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                className="pl-8 h-9 bg-surface-base/60"
                aria-label="Cari interaksi"
              />
            </div>
          </div>

          <div>
            <label className="text-xs text-text-muted block mb-1.5">
              Channel
            </label>
            <Select value={channel} onValueChange={setChannel}>
              <SelectTrigger className="h-9 w-full bg-surface-base/60">
                <SelectValue placeholder="Semua channel" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Semua channel</SelectItem>
                <SelectItem value="whatsapp">WhatsApp</SelectItem>
                <SelectItem value="telegram">Telegram</SelectItem>
                <SelectItem value="web">Web</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div>
            <label className="text-xs text-text-muted block mb-1.5">
              Tipe pesan
            </label>
            <Select value={messageType} onValueChange={setMessageType}>
              <SelectTrigger className="h-9 w-full bg-surface-base/60">
                <SelectValue placeholder="Semua tipe" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Semua tipe</SelectItem>
                <SelectItem value="user_message">User</SelectItem>
                <SelectItem value="ai_response">AI</SelectItem>
                <SelectItem value="system_event">System</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <label className="inline-flex items-center gap-2 cursor-pointer select-none h-9">
            <input
              type="checkbox"
              checked={todayOnly}
              onChange={(e) => setTodayOnly(e.target.checked)}
              className="size-4 accent-brand-navy"
              aria-label="Hari ini saja"
            />
            <span className="text-sm text-text-secondary">Hari ini saja</span>
          </label>
        </div>
      </Card>

      <Card className="bg-surface-card border-0 rounded-card overflow-hidden">
        <div className="px-4 py-3 flex items-center justify-between text-xs text-text-muted">
          <span>
            {data
              ? `${data.total.toLocaleString("id-ID")} hasil`
              : isLoading
                ? "Memuat…"
                : "—"}
          </span>
          {isFetching && !isLoading && <span>Menyegarkan…</span>}
        </div>

        {isLoading ? (
          <LoadingRows />
        ) : isError ? (
          <EmptyRow
            icon={MessageCircle}
            title="Gagal memuat data"
            hint="Coba muat ulang halaman atau periksa admin-api."
          />
        ) : !data || data.items.length === 0 ? (
          <EmptyRow
            icon={Inbox}
            title="Tidak ada interaksi yang cocok"
            hint="Sesuaikan filter atau kata kunci pencarian."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="border-b border-white/5 hover:bg-transparent text-text-muted">
                <TableHead className="text-text-muted">Waktu</TableHead>
                <TableHead className="text-text-muted">Channel</TableHead>
                <TableHead className="text-text-muted">Tipe</TableHead>
                <TableHead className="text-text-muted">Isi</TableHead>
                <TableHead className="text-text-muted">Intent</TableHead>
                <TableHead className="text-text-muted text-right">
                  Response
                </TableHead>
                <TableHead className="text-text-muted text-right">
                  Aksi
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.items.map((it, i) => (
                <motion.tr
                  key={String(it.id)}
                  initial={{ opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{
                    duration: 0.2,
                    ease: "easeOut",
                    delay: Math.min(i, 12) * 0.04,
                  }}
                  className="border-b border-white/5 hover:bg-surface-card-hover/60 transition-colors"
                >
                  <TableCell className="text-text-secondary text-xs">
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span>{relativeTime(it.created_at)}</span>
                      </TooltipTrigger>
                      <TooltipContent>
                        {new Date(it.created_at).toLocaleString("id-ID")}
                      </TooltipContent>
                    </Tooltip>
                  </TableCell>
                  <TableCell>
                    <ChannelBadge channel={it.channel} />
                  </TableCell>
                  <TableCell>
                    <MessageTypeBadge type={it.message_type} />
                  </TableCell>
                  <TableCell className="max-w-md">
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span className="text-sm text-text-secondary block truncate">
                          {truncate(it.content, 120) || "(empty)"}
                        </span>
                      </TooltipTrigger>
                      <TooltipContent className="max-w-md whitespace-pre-wrap">
                        {it.content || "(empty)"}
                      </TooltipContent>
                    </Tooltip>
                  </TableCell>
                  <TableCell className="text-text-secondary text-sm">
                    {it.intent ?? (
                      <span className="text-text-muted">—</span>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    <ResponseTimeBadge ms={it.response_time_ms} />
                  </TableCell>
                  <TableCell className="text-right">
                    <Link
                      href={`/ai-interactions/${encodeURIComponent(it.session_id)}`}
                      className="text-xs text-brand-amber hover:text-brand-amber-hover transition-colors"
                    >
                      View thread →
                    </Link>
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

function LoadingRows() {
  return (
    <div className="px-4 pb-4 space-y-2">
      {Array.from({ length: 8 }).map((_, i) => (
        <Skeleton key={i} className="h-10 w-full bg-surface-card-hover" />
      ))}
    </div>
  );
}

function EmptyRow({
  icon: Icon,
  title,
  hint,
}: {
  icon: typeof Inbox;
  title: string;
  hint?: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-16">
      <Icon className="size-8 text-text-muted mb-3" aria-hidden />
      <p className="text-sm text-text-secondary">{title}</p>
      {hint && <p className="text-xs text-text-muted mt-1">{hint}</p>}
    </div>
  );
}
