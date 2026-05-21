"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { Bot, SendHorizonal, Sparkles, UserRound, Wrench } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { apiFetch } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  CaseValidationPayload,
  CopilotChatResponse,
  CopilotTurn,
} from "@/lib/case-types";

/**
 * Officer Copilot side panel — the per-officer "right-hand man" (Decisions §22).
 *
 * Behaviour:
 *  - On mount, rehydrates the persisted session via
 *    GET /case/{ticket}/copilot/session. The session is per-officer-per-case
 *    and survives navigation (admin-api owns the `copilot_session` table).
 *  - If the session is empty, it auto-sends a silent kickoff turn so the
 *    copilot LEADS with the BIMA validation summary — score + worst-first
 *    issues — instead of a blank chatbot box. The validator result the case
 *    page already holds is forwarded so the copilot needs no fresh Gemini
 *    Vision pass.
 *  - Subsequent turns POST to /case/{ticket}/copilot/chat; admin-api persists
 *    the updated history.
 *
 * Brand: Midnight Government — surface-card panels, no hard borders, navy/
 * amber accents, Manrope via the inherited font stack.
 */

// The silent kickoff prompt. Never shown in the transcript — it just nudges
// the validation-first system prompt to produce the opening narrative.
const KICKOFF_PROMPT =
  "Mulai sesi. Ringkas hasil validasi berkas ini: sebutkan skor, lalu " +
  "pandu saya menelusuri masalah dari yang paling parah.";

type PanelState = "loading" | "idle" | "sending";

export function CopilotPanel({
  ticket,
  validation,
}: {
  ticket: string;
  /** The validator result the case page already computed, forwarded so the
   *  copilot's get_validation_summary tool can surface it. Null until the
   *  case page has a result. */
  validation: CaseValidationPayload | null;
}) {
  const [turns, setTurns] = useState<CopilotTurn[]>([]);
  const [state, setState] = useState<PanelState>("loading");
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  // Guards against the kickoff firing twice (React 18 StrictMode double-mount
  // in dev, plus any re-render race).
  const kickoffFired = useRef(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  // Send one turn to the copilot. `silent` keeps the kickoff prompt out of
  // the visible transcript while still showing the model's reply.
  const sendTurn = useCallback(
    async (message: string, opts?: { silent?: boolean }) => {
      const silent = opts?.silent ?? false;
      setState("sending");
      setError(null);

      // Optimistically render the officer's own message.
      if (!silent) {
        setTurns((prev) => [...prev, { role: "user", text: message }]);
      }

      try {
        const res = await apiFetch<CopilotChatResponse>(
          `/case/${encodeURIComponent(ticket)}/copilot/chat`,
          {
            method: "POST",
            body: {
              message,
              validation: validation ?? undefined,
            },
          }
        );
        // admin-api returns the full persisted history — adopt it verbatim so
        // the panel state always matches the server. The silent kickoff turn
        // is stored server-side; we strip it from the view below.
        setTurns(stripKickoff(res.history));
      } catch {
        // Roll back the optimistic user bubble on failure.
        if (!silent) {
          setTurns((prev) => prev.slice(0, -1));
        }
        setError(
          "Copilot tidak merespons. Periksa koneksi lalu coba lagi."
        );
      } finally {
        setState("idle");
      }
    },
    [ticket, validation]
  );

  // Mount: rehydrate the persisted session, then kick off if it's empty.
  useEffect(() => {
    let cancelled = false;

    async function bootstrap() {
      try {
        const session = await apiFetch<CopilotChatResponse>(
          `/case/${encodeURIComponent(ticket)}/copilot/session`
        );
        if (cancelled) return;

        const existing = stripKickoff(session.history);
        setTurns(existing);
        setState("idle");

        // Empty session → lead with the validation summary.
        if (existing.length === 0 && !kickoffFired.current) {
          kickoffFired.current = true;
          void sendTurn(KICKOFF_PROMPT, { silent: true });
        }
      } catch {
        if (cancelled) return;
        // A failed rehydrate must not block the panel — start fresh.
        setState("idle");
        if (!kickoffFired.current) {
          kickoffFired.current = true;
          void sendTurn(KICKOFF_PROMPT, { silent: true });
        }
      }
    }

    void bootstrap();
    return () => {
      cancelled = true;
    };
  }, [ticket, sendTurn]);

  useEffect(() => {
    scrollToBottom();
  }, [turns, state, scrollToBottom]);

  const handleSubmit = useCallback(() => {
    const text = draft.trim();
    if (!text || state === "sending") return;
    setDraft("");
    void sendTurn(text);
  }, [draft, state, sendTurn]);

  return (
    <Card className="bg-surface-card border-0 rounded-card flex flex-col overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2.5 px-5 py-4 bg-surface-card-hover/40">
        <div className="rounded-full bg-brand-navy/25 p-1.5 text-brand-amber">
          <Sparkles className="size-4" aria-hidden />
        </div>
        <div className="min-w-0">
          <h2 className="font-display text-sm font-semibold text-text-primary leading-tight">
            Copilot Validasi BIMA
          </h2>
          <p className="text-[11px] text-text-muted">
            Asisten pribadi untuk tiket {ticket}
          </p>
        </div>
      </div>

      {/* Transcript */}
      <div
        ref={scrollRef}
        className="flex-1 min-h-[280px] max-h-[460px] overflow-y-auto px-4 py-4 space-y-3"
        aria-live="polite"
        aria-busy={state !== "idle"}
      >
        {state === "loading" ? (
          <CopilotLoading />
        ) : (
          turns.map((turn, i) => (
            <ChatBubble key={`${turn.role}-${i}`} turn={turn} index={i} />
          ))
        )}

        {state === "sending" && <TypingIndicator />}

        {error && (
          <p
            role="alert"
            className="text-xs text-brand-rose px-1 leading-relaxed"
          >
            {error}
          </p>
        )}
      </div>

      {/* Composer */}
      <div className="px-4 py-3 bg-surface-card-hover/30">
        <div className="flex items-end gap-2">
          <Textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSubmit();
              }
            }}
            placeholder="Tanya copilot soal berkas ini…"
            rows={1}
            disabled={state === "sending"}
            aria-label="Pesan untuk copilot"
            className="min-h-[42px] max-h-32 resize-none bg-surface-base/60 text-sm"
          />
          <button
            type="button"
            onClick={handleSubmit}
            disabled={state === "sending" || draft.trim().length === 0}
            aria-label="Kirim pesan"
            className={cn(
              "shrink-0 rounded-card p-2.5 transition-colors",
              "bg-brand-navy hover:bg-brand-navy-hover text-white",
              "disabled:opacity-40 disabled:cursor-not-allowed"
            )}
          >
            <SendHorizonal className="size-4" aria-hidden />
          </button>
        </div>
        <p className="mt-1.5 text-[10px] text-text-muted px-1">
          Enter untuk kirim · Shift+Enter baris baru
        </p>
      </div>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Sub-components                                                              */
/* -------------------------------------------------------------------------- */

function ChatBubble({ turn, index }: { turn: CopilotTurn; index: number }) {
  const isOfficer = turn.role === "user";
  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, ease: "easeOut", delay: Math.min(index, 6) * 0.03 }}
      className={cn("flex gap-2", isOfficer ? "flex-row-reverse" : "flex-row")}
    >
      <div
        className={cn(
          "shrink-0 rounded-full p-1.5 size-7 flex items-center justify-center",
          isOfficer
            ? "bg-surface-card-hover text-text-secondary"
            : "bg-brand-navy/25 text-brand-amber"
        )}
        aria-hidden
      >
        {isOfficer ? (
          <UserRound className="size-3.5" />
        ) : (
          <Bot className="size-3.5" />
        )}
      </div>
      <div
        className={cn(
          "max-w-[82%] rounded-card px-3.5 py-2.5 text-sm leading-relaxed whitespace-pre-wrap",
          isOfficer
            ? "bg-surface-card-hover text-text-primary"
            : "bg-surface-base/70 text-text-primary"
        )}
      >
        {turn.text}
      </div>
    </motion.div>
  );
}

function TypingIndicator() {
  return (
    <div className="flex gap-2" aria-label="Copilot sedang mengetik">
      <div className="shrink-0 rounded-full bg-brand-navy/25 text-brand-amber p-1.5 size-7 flex items-center justify-center">
        <Bot className="size-3.5" aria-hidden />
      </div>
      <div className="rounded-card bg-surface-base/70 px-3.5 py-3 flex items-center gap-1">
        {[0, 1, 2].map((i) => (
          <motion.span
            key={i}
            className="size-1.5 rounded-full bg-text-muted"
            animate={{ opacity: [0.3, 1, 0.3] }}
            transition={{
              duration: 1,
              repeat: Infinity,
              delay: i * 0.18,
              ease: "easeInOut",
            }}
          />
        ))}
      </div>
    </div>
  );
}

function CopilotLoading() {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
      <Wrench className="size-5 text-text-muted animate-pulse" aria-hidden />
      <p className="text-xs text-text-muted">Memuat sesi copilot…</p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Helpers                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * The first stored user turn is the silent kickoff prompt — drop it from the
 * displayed transcript so the officer sees the copilot's validation narrative
 * as the opening message, not the machinery that triggered it.
 */
function stripKickoff(history: CopilotTurn[]): CopilotTurn[] {
  if (history.length > 0 && history[0].role === "user") {
    return history.slice(1);
  }
  return history;
}
