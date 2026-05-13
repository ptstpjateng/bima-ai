"use client";

import { use } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { ArrowLeft, Inbox, MessageCircle, User as UserIcon } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ChannelBadge,
  ResponseTimeBadge,
} from "@/components/admin/badges";
import { apiFetch, ApiError } from "@/lib/api";
import { formatDateTime, formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { AiInteractionSession, AiInteractionTurn } from "@/lib/types";

/**
 * Thread view for a single AI session.
 *
 * Chat-style bubble layout — user_message right-aligned on brand-navy,
 * ai_response left-aligned on surface-card, system_event centered + muted.
 */
export default function ThreadPage({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  const { sessionId } = use(params);
  const decoded = decodeURIComponent(sessionId);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["ai-interactions", "session", decoded],
    queryFn: () =>
      apiFetch<AiInteractionSession>(
        `/ai-interactions/sessions/${encodeURIComponent(decoded)}`
      ).catch((err) => {
        if (err instanceof ApiError && err.status === 404) {
          return null as unknown as AiInteractionSession;
        }
        throw err;
      }),
  });

  return (
    <div className="space-y-6 max-w-4xl">
      <Link
        href="/ai-interactions"
        className="inline-flex items-center gap-1.5 text-sm text-text-secondary hover:text-text-primary transition-colors"
      >
        <ArrowLeft className="size-4" aria-hidden />
        Kembali ke daftar
      </Link>

      {isLoading ? (
        <HeaderSkeleton />
      ) : isError ? (
        <Card className="bg-surface-card border-0 rounded-card p-6 text-sm text-brand-rose">
          Tidak bisa memuat percakapan ini. Coba muat ulang halaman.
        </Card>
      ) : !data ? (
        <Card className="bg-surface-card border-0 rounded-card p-10 text-center">
          <Inbox className="size-8 text-text-muted mx-auto mb-3" aria-hidden />
          <p className="text-sm text-text-secondary">
            Sesi tidak ditemukan atau sudah dibersihkan.
          </p>
          <p className="text-xs text-text-muted mt-1 break-all">
            session_id: <span className="font-mono">{decoded}</span>
          </p>
        </Card>
      ) : (
        <>
          <SessionHeader session={data} />
          <div className="space-y-3">
            {data.turns.length === 0 ? (
              <Card className="bg-surface-card border-0 rounded-card p-8 text-center text-sm text-text-secondary">
                Sesi ini tidak memiliki turn.
              </Card>
            ) : (
              data.turns.map((t, i) => (
                <TurnBubble key={String(t.id)} turn={t} index={i} />
              ))
            )}
          </div>
        </>
      )}
    </div>
  );
}

function SessionHeader({ session }: { session: AiInteractionSession }) {
  return (
    <Card className="bg-surface-card border-0 rounded-card p-6">
      <div className="flex flex-wrap items-start gap-x-6 gap-y-3">
        <div>
          <div className="text-[11px] uppercase tracking-wider text-text-muted">
            Channel
          </div>
          <div className="mt-1.5">
            <ChannelBadge channel={session.channel} />
          </div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-wider text-text-muted">
            Turn count
          </div>
          <div className="mt-1.5 font-display text-xl text-text-primary tabular-nums">
            {session.turn_count.toLocaleString("id-ID")}
          </div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-wider text-text-muted">
            Dimulai
          </div>
          <div className="mt-1.5 text-sm text-text-secondary">
            {formatDateTime(session.started_at)}
          </div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-wider text-text-muted">
            Turn terakhir
          </div>
          <div className="mt-1.5 text-sm text-text-secondary">
            {formatDateTime(session.last_turn_at)}
          </div>
        </div>
        <div className="ml-auto">
          <div className="text-[11px] uppercase tracking-wider text-text-muted">
            User
          </div>
          <div className="mt-1.5">
            {session.user_id ? (
              <span className="inline-flex items-center gap-1.5 text-sm text-text-primary">
                <UserIcon className="size-3.5 text-text-muted" aria-hidden />
                <span className="font-mono">#{String(session.user_id)}</span>
              </span>
            ) : (
              <span className="text-sm text-text-muted">Anonim</span>
            )}
          </div>
        </div>
      </div>
      <div className="mt-4 pt-4 border-t border-white/5 text-[11px] text-text-muted">
        session_id ·{" "}
        <span className="font-mono break-all">{session.session_id}</span>
      </div>
    </Card>
  );
}

function TurnBubble({ turn, index }: { turn: AiInteractionTurn; index: number }) {
  const type = (turn.message_type ?? "").toLowerCase();
  const isUser = type === "user_message";
  const isAi = type === "ai_response";
  const isSystem = type === "system_event";

  if (isSystem) {
    return (
      <motion.div
        initial={{ opacity: 0, y: 4 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.2, ease: "easeOut", delay: Math.min(index, 12) * 0.03 }}
        className="flex justify-center"
      >
        <div className="rounded-pill bg-surface-card-hover px-3 py-1 text-[11px] text-text-muted">
          <span className="font-mono mr-2">#{turn.turn_index}</span>
          {turn.content || "system event"}
          <span className="ml-2">· {formatTime(turn.created_at)}</span>
        </div>
      </motion.div>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, ease: "easeOut", delay: Math.min(index, 12) * 0.03 }}
      className={cn(
        "flex",
        isUser ? "justify-end" : "justify-start"
      )}
    >
      <div
        className={cn(
          "max-w-[80%] rounded-card p-4 space-y-2",
          isUser
            ? "bg-brand-navy text-white"
            : "bg-surface-card text-text-primary"
        )}
      >
        <div
          className={cn(
            "flex items-center gap-2 text-[11px]",
            isUser ? "text-white/70" : "text-text-muted"
          )}
        >
          <span className="font-mono">#{turn.turn_index}</span>
          <span>·</span>
          <span>{isUser ? "User" : isAi ? "BIMA" : turn.message_type}</span>
          <span>·</span>
          <span>{formatTime(turn.created_at)}</span>
        </div>
        <div className="text-sm whitespace-pre-wrap leading-relaxed break-words">
          {turn.content || "(empty)"}
        </div>
        {isAi && (turn.intent || turn.response_time_ms != null) && (
          <div className="flex items-center gap-3 pt-2 border-t border-white/5 text-[11px] text-text-muted">
            {turn.intent && (
              <span className="inline-flex items-center gap-1">
                <MessageCircle className="size-3" aria-hidden />
                {turn.intent}
              </span>
            )}
            {turn.response_time_ms != null && (
              <ResponseTimeBadge ms={turn.response_time_ms} />
            )}
          </div>
        )}
      </div>
    </motion.div>
  );
}

function HeaderSkeleton() {
  return (
    <>
      <Skeleton className="h-32 w-full bg-surface-card" />
      <div className="space-y-3">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton
            key={i}
            className={`h-20 ${i % 2 ? "w-2/3" : "w-3/4 ml-auto"} bg-surface-card-hover`}
          />
        ))}
      </div>
    </>
  );
}
