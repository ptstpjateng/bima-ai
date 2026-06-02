import {
  ArrowLeft,
  MessageCircle,
  FileText,
  AlertTriangle,
} from "lucide-react";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { Metadata } from "next";

import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { formatDateID } from "@/lib/format";

/**
 * Citizen-facing tracking page.
 *
 * Server Component — fetches from admin-api during render so the browser
 * never sees the X-Internal-Key or the SIAP bearer. Renders three states
 * inline: success (status card), 404 (ticket not found), 503 (SIAP down).
 *
 * URL: portal.nolongin.com/track/{ticket}  (links from WhatsApp replies)
 *
 * Env required (server-only — do NOT prefix with NEXT_PUBLIC_):
 *   BIMA_ADMIN_API_URL    e.g. https://nolongin.com/admin-api
 *   BIMA_INTERNAL_API_KEY same value as ai-engine / admin-api / Laravel
 *
 * This is a REAL surface (live SIAP data via admin-api). Only the styling
 * was migrated to the Bima system; behavior is unchanged.
 */

type TrackingRecord = {
  ticket: string;
  license_name: string;
  sector_name: string | null;
  applicant_name: string;
  current_desk: string;
  status: string;
  submitted_at: string | null;
};

type FetchResult =
  | { kind: "ok"; record: TrackingRecord }
  | { kind: "not_found" }
  | { kind: "service_down"; reason: string };

async function fetchTracking(ticket: string): Promise<FetchResult> {
  const baseUrl = process.env.BIMA_ADMIN_API_URL;
  const internalKey = process.env.BIMA_INTERNAL_API_KEY;

  if (!baseUrl || !internalKey) {
    return {
      kind: "service_down",
      reason: "Tracking API belum dikonfigurasi pada lingkungan ini.",
    };
  }

  try {
    const resp = await fetch(`${baseUrl.replace(/\/$/, "")}/tracking/${ticket}`, {
      headers: {
        "X-Internal-Key": internalKey,
        Accept: "application/json",
      },
      // Status changes minute-to-minute as files move between desks; do not cache.
      cache: "no-store",
    });

    if (resp.status === 404) return { kind: "not_found" };
    if (resp.status === 503) {
      return {
        kind: "service_down",
        reason: "Sistem SIAP Jateng sedang tidak dapat dihubungi.",
      };
    }
    if (!resp.ok) {
      return {
        kind: "service_down",
        reason: `Server menjawab HTTP ${resp.status}.`,
      };
    }

    const record = (await resp.json()) as TrackingRecord;
    return { kind: "ok", record };
  } catch {
    return {
      kind: "service_down",
      reason: "Tidak bisa menghubungi server tracking.",
    };
  }
}

// ----- Metadata --------------------------------------------------------------

export async function generateMetadata({
  params,
}: {
  params: Promise<{ ticket: string }>;
}): Promise<Metadata> {
  const { ticket } = await params;
  return {
    title: `Lacak Izin ${ticket} · BIMA`,
    description: `Status permohonan perizinan tiket ${ticket} di DPMPTSP Jawa Tengah.`,
    robots: { index: false }, // do not index per-ticket pages
  };
}

// ----- Page ------------------------------------------------------------------

export default async function TrackingPage({
  params,
}: {
  params: Promise<{ ticket: string }>;
}) {
  const { ticket } = await params;

  // Defensive: validate the URL param shape before hitting the API.
  if (!/^\d{4,9}$/.test(ticket)) {
    notFound();
  }
  const padded = ticket.padStart(9, "0");
  const result = await fetchTracking(padded);

  return (
    <main className="min-h-screen px-4 py-10 sm:px-6 sm:py-12 lg:px-8">
      <div className="mx-auto flex w-full max-w-2xl flex-col gap-6">
        <Link
          href="/"
          className="inline-flex items-center gap-2 text-sm text-ink-2 transition-colors hover:text-ink"
        >
          <ArrowLeft className="size-4" aria-hidden="true" />
          <span>Kembali ke beranda BIMA</span>
        </Link>

        <header className="mt-2">
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-brand-green-text">
            Lacak Permohonan Izin
          </p>
          <h1 className="mt-3 font-display text-2xl font-bold leading-tight tracking-tight text-ink sm:text-3xl md:text-4xl">
            Tiket{" "}
            <span className="font-mono text-xl text-primary sm:text-3xl md:text-4xl">
              {padded}
            </span>
          </h1>
        </header>

        <ResultCard result={result} ticket={padded} />

        <CtaFooter />
      </div>
    </main>
  );
}

// ----- Subviews --------------------------------------------------------------

/** Wraps the chosen card in a one-beat fade+rise (reduced-motion safe via CSS). */
function ResultCard({
  result,
  ticket,
}: {
  result: FetchResult;
  ticket: string;
}) {
  return (
    <div className="animate-fade-in-up">
      {result.kind === "ok" && <StatusCard record={result.record} />}
      {result.kind === "not_found" && <NotFoundCard ticket={ticket} />}
      {result.kind === "service_down" && (
        <ServiceDownCard ticket={ticket} reason={result.reason} />
      )}
    </div>
  );
}

function StatusCard({ record }: { record: TrackingRecord }) {
  return (
    <section
      aria-labelledby="status-heading"
      className="rounded-card border border-border bg-surface p-6 sm:p-8"
    >
      <h2 id="status-heading" className="sr-only">
        Status permohonan
      </h2>

      <div className="flex items-start gap-3">
        <span className="mt-0.5 inline-flex size-10 shrink-0 items-center justify-center rounded-pill bg-surface-2">
          <FileText className="size-5 text-primary" aria-hidden="true" />
        </span>
        <div className="min-w-0 space-y-1">
          <p className="text-xs uppercase tracking-wide text-ink-3">Permohonan</p>
          <p className="font-display text-xl font-semibold leading-snug text-ink">
            {record.license_name}
          </p>
          {record.sector_name && (
            <p className="text-sm text-ink-2">{record.sector_name}</p>
          )}
        </div>
      </div>

      <dl className="mt-8 grid gap-x-6 gap-y-5 sm:grid-cols-2">
        <Field label="Pemohon" value={record.applicant_name} />
        <Field label="Tanggal daftar" value={formatDateID(record.submitted_at)} />
        <Field label="Posisi berkas" value={record.current_desk} />
        <div>
          <dt className="text-xs uppercase tracking-wide text-ink-3">Status</dt>
          <dd className="mt-1.5">
            <StatusBadge status={record.status} />
          </dd>
        </div>
      </dl>
    </section>
  );
}

function NotFoundCard({ ticket }: { ticket: string }) {
  return (
    <section className="rounded-card border border-border bg-surface p-6 sm:p-8">
      <h2 className="font-display text-xl font-semibold text-ink">
        Tiket tidak ditemukan
      </h2>
      <p className="mt-3 text-ink-2">
        Kami tidak menemukan permohonan dengan tiket{" "}
        <span className="font-mono text-ink">{ticket}</span>. Coba periksa
        kembali nomor tiket Anda — biasanya 9 digit angka yang tercetak pada
        tanda terima saat Anda mendaftar.
      </p>
    </section>
  );
}

function ServiceDownCard({ ticket, reason }: { ticket: string; reason: string }) {
  return (
    <section className="rounded-card border border-warning/30 bg-warning/6 p-6 sm:p-8">
      <div className="flex items-start gap-3">
        <AlertTriangle
          className="mt-0.5 size-5 shrink-0 text-warning"
          aria-hidden="true"
        />
        <div>
          <h2 className="font-display text-xl font-semibold text-ink">
            Sementara tidak dapat memeriksa
          </h2>
          <p className="mt-3 text-ink-2">
            {reason} Silakan coba lagi beberapa menit ke depan.
          </p>
          <p className="mt-2 text-sm text-ink-2">
            Atau tanyakan langsung ke BIMA via WhatsApp:{" "}
            <span className="font-medium text-ink">status izin {ticket}</span>
          </p>
        </div>
      </div>
    </section>
  );
}

function Field({
  label,
  value,
  valueClassName = "text-ink",
}: {
  label: string;
  value: string;
  valueClassName?: string;
}) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-ink-3">{label}</dt>
      <dd className={`mt-1 ${valueClassName}`}>{value || "—"}</dd>
    </div>
  );
}

function CtaFooter() {
  return (
    <div className="mt-2 rounded-card border border-border bg-surface-2 p-5">
      <p className="text-sm text-ink-2">
        Ada pertanyaan lanjutan? BIMA siap membantu di WhatsApp.
      </p>
      <Button asChild variant="amber" size="lg" className="mt-4">
        <a
          href="https://wa.me/6285117557091"
          target="_blank"
          rel="noopener noreferrer"
          aria-label="Chat dengan BIMA di WhatsApp"
        >
          <MessageCircle aria-hidden="true" />
          <span>Chat dengan BIMA</span>
        </a>
      </Button>
    </div>
  );
}
