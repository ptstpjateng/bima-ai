"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import Link from "next/link";
import {
  ArrowLeft,
  Briefcase,
  CalendarDays,
  CheckCircle2,
  CloudOff,
  FileQuestion,
  PenLine,
  RefreshCcw,
  ShieldCheck,
  Sparkles,
  UserRound,
} from "lucide-react";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import { Button } from "@/components/ui/button";
import { ScoreGauge } from "@/components/case/score-gauge";
import { IssueCard } from "@/components/case/issue-card";
import { CopilotPanel } from "@/components/case/copilot-panel";
import { SigningHandoffCard } from "@/components/case/signing-handoff-card";
import {
  CaseStatusBadge,
  DemoBadge,
} from "@/components/case/case-status-badge";
import { SektorBadge } from "@/components/admin/badges";
import { apiFetch, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatDateTime } from "@/lib/format";
import {
  SEVERITY_ORDER,
  VALIDATION_STATUS_LABEL,
  type CaseValidateResponse,
  type CopilotMode,
  type DemoFixture,
} from "@/lib/case-types";

/**
 * Officer-facing case validator view. Phase 2 (Sprint D) "killer feature".
 *
 * Talks to admin-api via the same-origin proxy (`apiFetch` → `/api/proxy/...`).
 * Uses POST because the validator endpoint is non-idempotent on the AI side
 * (it can rerun the Gemini call), but the resulting JSON is cached for
 * 5 minutes by react-query's default staleTime to keep the page snappy when
 * the officer flips between cases.
 */
/**
 * Heuristic: is this case at the FINAL signing stage?
 *
 * BIMA does not own a clean SIAP role/stage enum, so we infer from the
 * free-form `current_desk` (`posisi_berkas`) and `status` text. SIAP's
 * signing surface is the "Tanda Tangan Berkas" desk, registered for the
 * `Kepala DPMPTSP` role. When the case sits at a desk/status that mentions
 * the Head or signing, the signature-assistant is the natural default.
 *
 * This is a best-effort default only — the mode toggle always lets a user
 * switch in or out regardless. See the PR notes on scoping.
 */
const SIGNING_STAGE_PATTERN = /tanda.?tangan|kepala|penandatangan|tte|^ttd/i;

function looksLikeSigningStage(
  desk: string | null,
  status: string
): boolean {
  return (
    SIGNING_STAGE_PATTERN.test(desk ?? "") ||
    SIGNING_STAGE_PATTERN.test(status)
  );
}

export function CaseView({
  ticket,
  fixture,
}: {
  ticket: string;
  fixture: DemoFixture | null;
}) {
  const params: Record<string, string | undefined> = {};
  if (fixture) params.demo_fixture = fixture;

  // Copilot mode — null until the case loads, then defaulted from the SIAP
  // stage heuristic. A user-driven toggle overrides it.
  const [copilotMode, setCopilotMode] = useState<CopilotMode | null>(null);

  const { data, isLoading, isError, error, isFetching, refetch } = useQuery({
    queryKey: ["case-validate", ticket, fixture ?? "live"],
    queryFn: () =>
      apiFetch<CaseValidateResponse>(
        `/case/${encodeURIComponent(ticket)}/validate`,
        {
          method: "POST",
          params,
        }
      ),
    retry: false,
  });

  if (isLoading) return <CaseSkeleton />;

  if (isError) {
    const status =
      error instanceof ApiError ? error.status : 0;

    if (status === 404) {
      return <NotFoundCard ticket={ticket} />;
    }
    if (status === 502 || status === 503 || status === 504) {
      return <ValidatorOfflineCard onRetry={() => refetch()} />;
    }
    return (
      <GenericErrorCard
        ticket={ticket}
        message={
          error instanceof Error
            ? error.message
            : "Terjadi kesalahan tak terduga."
        }
        onRetry={() => refetch()}
      />
    );
  }

  if (!data) return <CaseSkeleton />;

  const { case: caseInfo, validation } = data;
  const sortedIssues = [...validation.issues].sort(
    (a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]
  );

  // Resolve the copilot mode: the user toggle wins; otherwise default from
  // the SIAP signing-stage heuristic so a case sitting on the Head's desk
  // opens the signature assistant automatically.
  const stageIsSigning = looksLikeSigningStage(
    caseInfo.current_desk,
    caseInfo.status
  );
  const activeMode: CopilotMode =
    copilotMode ?? (stageIsSigning ? "signature" : "officer");

  return (
    <div className="space-y-6 max-w-6xl mx-auto">
      {/* Top row: back link (left) + demo badge + refresh (right). */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <Link
          href="/dashboard"
          className="inline-flex items-center gap-1.5 text-sm text-text-secondary hover:text-text-primary transition-colors"
        >
          <ArrowLeft className="size-4" aria-hidden />
          Kembali ke dashboard
        </Link>

        <div className="flex items-center gap-3">
          {fixture && <DemoBadge fixture={fixture} />}
          <button
            type="button"
            onClick={() => refetch()}
            className="inline-flex items-center gap-1.5 text-xs text-text-secondary hover:text-text-primary transition-colors"
            aria-label="Refresh validator"
          >
            <RefreshCcw
              className={`size-3 ${isFetching ? "animate-spin" : ""}`}
              aria-hidden
            />
            <span>Jalankan ulang</span>
          </button>
        </div>
      </div>

      {/* (a) Case header */}
      <Card className="bg-surface-card border-0 rounded-card p-6 sm:p-8">
        <div className="flex flex-col gap-5 md:flex-row md:items-start md:justify-between md:gap-8">
          <div className="space-y-3 min-w-0">
            <div className="text-[11px] uppercase tracking-widest text-text-muted">
              Tiket SIAP
            </div>
            <div className="font-mono text-brand-amber text-3xl sm:text-4xl tracking-tight break-all">
              {caseInfo.ticket}
            </div>
            <h1 className="font-display text-xl sm:text-2xl font-medium text-text-primary leading-snug">
              {caseInfo.license_name}
            </h1>
            <div className="flex flex-wrap items-center gap-2">
              <SektorBadge sektor={caseInfo.sector_name} />
              <CaseStatusBadge status={caseInfo.status} />
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-1 gap-3 md:min-w-[240px]">
            <HeaderMeta
              icon={UserRound}
              label="Pemohon"
              value={caseInfo.applicant_name}
            />
            <HeaderMeta
              icon={Briefcase}
              label="Meja saat ini"
              value={caseInfo.current_desk ?? "—"}
            />
            <HeaderMeta
              icon={CalendarDays}
              label="Diajukan"
              value={formatDateTime(caseInfo.submitted_at)}
            />
          </div>
        </div>
      </Card>

      {/* (b) BIMA Score card */}
      <Card className="bg-surface-card border-0 rounded-card p-6 sm:p-8">
        <div className="flex flex-col gap-6 md:flex-row md:items-center md:gap-10">
          <div className="flex justify-center md:justify-start shrink-0">
            <ScoreGauge
              percent={validation.score_percent}
              status={validation.status}
            />
          </div>

          <Separator
            orientation="vertical"
            className="hidden md:block bg-white/[0.06]"
          />

          <div className="space-y-3 flex-1 min-w-0">
            <div className="inline-flex items-center gap-2">
              <ShieldCheck
                className="size-4 text-brand-navy-hover"
                aria-hidden
              />
              <span className="text-[11px] uppercase tracking-widest text-text-muted">
                Status validasi BIMA
              </span>
            </div>
            <h2 className="font-display text-2xl sm:text-3xl font-semibold text-text-primary leading-tight">
              {VALIDATION_STATUS_LABEL[validation.status]}
            </h2>
            <p className="text-sm sm:text-base text-text-secondary leading-relaxed">
              {validation.summary}
            </p>

            {validation.per_document?.length > 0 && (
              <div className="pt-2 flex flex-wrap gap-1.5">
                {validation.per_document.map((doc) => (
                  <span
                    key={doc.filename}
                    className="inline-flex items-center gap-1.5 rounded-pill bg-surface-card-hover px-2 py-0.5 text-[11px] text-text-secondary"
                  >
                    <span className="text-text-muted">{doc.filename}</span>
                    <span className="font-mono tabular-nums text-text-secondary">
                      {doc.fields_extracted}/{doc.fields_expected}
                    </span>
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
      </Card>

      {/* (c) Issues list + (d) Officer Copilot — two columns on xl. */}
      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_380px] gap-6 items-start">
        <section aria-label="Daftar masalah" className="space-y-3">
          <div className="flex items-baseline justify-between gap-3 px-1">
            <h2 className="font-display text-lg sm:text-xl font-medium text-text-primary">
              Masalah ditemukan
            </h2>
            <span className="text-xs text-text-muted tabular-nums">
              {sortedIssues.length.toLocaleString("id-ID")} item
            </span>
          </div>

          {sortedIssues.length === 0 ? (
            <ReadyToSendCard />
          ) : (
            <div className="grid grid-cols-1 gap-3">
              {sortedIssues.map((issue, i) => (
                <IssueCard
                  key={`${issue.severity}-${issue.field}-${i}`}
                  issue={issue}
                  index={i}
                />
              ))}
            </div>
          )}
        </section>

        {/* (d) Copilot — officer validation copilot or, in signature mode,
            the Head-of-DPMPTSP signing assistant. Sticky on xl so it stays
            visible while the officer scrolls the issues. */}
        <aside className="xl:sticky xl:top-6 space-y-4">
          <CopilotModeToggle
            mode={activeMode}
            onChange={setCopilotMode}
            stageIsSigning={stageIsSigning}
          />

          {/* Re-key by mode so switching does a clean remount: a fresh
              session rehydrate + the right kickoff opener. */}
          <CopilotPanel
            key={activeMode}
            ticket={caseInfo.ticket}
            validation={validation}
            mode={activeMode}
          />

          {/* The signing handoff CTA — only in signature mode. BIMA never
              signs; this deep-links the Head into SIAP Jateng to sign. */}
          {activeMode === "signature" && (
            <SigningHandoffCard ticket={caseInfo.ticket} />
          )}
        </aside>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Copilot mode toggle                                                         */
/* -------------------------------------------------------------------------- */

/**
 * Two-way switch between the officer validation copilot and the Head-of-
 * DPMPTSP signature assistant.
 *
 * Scoping note (Vision req #13): BIMA has no clean SIAP role claim on the
 * admin JWT, so we cannot hard-gate "signature mode = the Head only". We
 * instead (a) default the mode from the SIAP stage heuristic and (b) leave
 * the toggle always available. The signature assistant is read-only over
 * the case and the actual signature happens in SIAP, so an officer opening
 * it early is harmless — they simply get the approval-chain synthesis.
 */
function CopilotModeToggle({
  mode,
  onChange,
  stageIsSigning,
}: {
  mode: CopilotMode;
  onChange: (mode: CopilotMode) => void;
  stageIsSigning: boolean;
}) {
  const options: {
    value: CopilotMode;
    label: string;
    icon: typeof Sparkles;
  }[] = [
    { value: "officer", label: "Validasi", icon: Sparkles },
    { value: "signature", label: "Tanda Tangan", icon: PenLine },
  ];

  return (
    <div
      className="rounded-card bg-surface-card p-1 flex gap-1"
      role="tablist"
      aria-label="Mode copilot"
    >
      {options.map((opt) => {
        const Icon = opt.icon;
        const active = mode === opt.value;
        return (
          <button
            key={opt.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(opt.value)}
            className={cn(
              "flex-1 inline-flex items-center justify-center gap-1.5 rounded-card px-3 py-2 text-xs font-medium transition-colors",
              active
                ? "bg-brand-navy text-white"
                : "text-text-secondary hover:text-text-primary hover:bg-surface-card-hover"
            )}
          >
            <Icon className="size-3.5" aria-hidden />
            {opt.label}
            {opt.value === "signature" && stageIsSigning && !active && (
              <span
                className="size-1.5 rounded-full bg-brand-amber"
                aria-label="Disarankan untuk tahap ini"
              />
            )}
          </button>
        );
      })}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Sub-components                                                              */
/* -------------------------------------------------------------------------- */

function HeaderMeta({
  icon: Icon,
  label,
  value,
}: {
  icon: typeof Briefcase;
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-card bg-surface-base/40 px-3 py-2.5">
      <div className="inline-flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-text-muted">
        <Icon className="size-3" aria-hidden />
        {label}
      </div>
      <div className="text-sm text-text-primary mt-0.5 truncate">{value}</div>
    </div>
  );
}

function ReadyToSendCard() {
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: "easeOut" }}
      className="rounded-card bg-brand-emerald/10 ring-1 ring-brand-emerald/30 p-6 sm:p-8 flex flex-col sm:flex-row items-center sm:items-start gap-4 text-center sm:text-left"
    >
      <div className="shrink-0 rounded-full bg-brand-emerald/15 p-3">
        <CheckCircle2
          className="size-6 text-brand-emerald"
          aria-hidden
        />
      </div>
      <div className="space-y-1">
        <h3 className="font-display text-lg font-medium text-text-primary">
          Tidak ada masalah ditemukan
        </h3>
        <p className="text-sm text-text-secondary">
          Berkas siap dikirim ke PTSP.
        </p>
      </div>
    </motion.div>
  );
}

/* -------------------------------------------------------------------------- */
/* Loading + error states                                                      */
/* -------------------------------------------------------------------------- */

export function CaseSkeleton() {
  return (
    <div className="space-y-6 max-w-6xl mx-auto" aria-busy="true">
      <Skeleton className="h-4 w-40 bg-surface-card" />
      <Skeleton className="h-44 w-full bg-surface-card rounded-card" />
      <Skeleton className="h-60 w-full bg-surface-card rounded-card" />
      <div className="space-y-3">
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton
            key={i}
            className="h-24 w-full bg-surface-card rounded-card"
          />
        ))}
      </div>
    </div>
  );
}

function NotFoundCard({ ticket }: { ticket: string }) {
  return (
    <Card className="bg-surface-card border-0 rounded-card p-8 sm:p-10 text-center max-w-2xl mx-auto">
      <FileQuestion
        className="size-10 text-text-muted mx-auto mb-4"
        aria-hidden
      />
      <h2 className="font-display text-xl font-medium text-text-primary mb-2">
        Case tidak ditemukan
      </h2>
      <p className="text-sm text-text-secondary mb-1">
        Tiket SIAP berikut tidak ada di basis data BIMA.
      </p>
      <p className="font-mono text-xs text-text-muted">{ticket}</p>
      <Link
        href="/dashboard"
        className="inline-flex items-center gap-1.5 mt-6 text-sm text-brand-amber hover:text-brand-amber-hover transition-colors"
      >
        <ArrowLeft className="size-4" aria-hidden />
        Kembali ke dashboard
      </Link>
    </Card>
  );
}

function ValidatorOfflineCard({ onRetry }: { onRetry: () => void }) {
  return (
    <Card className="bg-surface-card border-0 rounded-card p-8 sm:p-10 text-center max-w-2xl mx-auto">
      <div className="inline-flex items-center justify-center rounded-full bg-brand-rose/15 p-3 mb-4">
        <CloudOff className="size-6 text-brand-rose" aria-hidden />
      </div>
      <h2 className="font-display text-xl font-medium text-text-primary mb-2">
        BIMA validator sedang offline
      </h2>
      <p className="text-sm text-text-secondary mb-6">
        Layanan validasi tidak merespons. Coba beberapa saat lagi.
      </p>
      <Button
        type="button"
        onClick={onRetry}
        className="bg-brand-navy hover:bg-brand-navy-hover text-white"
      >
        <RefreshCcw className="size-4 mr-2" aria-hidden />
        Coba lagi
      </Button>
    </Card>
  );
}

function GenericErrorCard({
  ticket,
  message,
  onRetry,
}: {
  ticket: string;
  message: string;
  onRetry: () => void;
}) {
  return (
    <Card className="bg-surface-card border-0 rounded-card p-8 sm:p-10 max-w-2xl mx-auto">
      <h2 className="font-display text-xl font-medium text-text-primary mb-2">
        Tidak bisa memuat case
      </h2>
      <p className="text-sm text-brand-rose mb-1">{message}</p>
      <p className="font-mono text-xs text-text-muted mb-6">tiket: {ticket}</p>
      <Button
        type="button"
        onClick={onRetry}
        className="bg-brand-navy hover:bg-brand-navy-hover text-white"
      >
        <RefreshCcw className="size-4 mr-2" aria-hidden />
        Coba lagi
      </Button>
    </Card>
  );
}
