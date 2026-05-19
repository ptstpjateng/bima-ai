import { ArrowLeft, MessageCircle } from "lucide-react";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { Metadata } from "next";

import { Button } from "@/components/ui/button";

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

const BULAN_ID = [
  "",
  "Januari",
  "Februari",
  "Maret",
  "April",
  "Mei",
  "Juni",
  "Juli",
  "Agustus",
  "September",
  "Oktober",
  "November",
  "Desember",
];

function formatDateID(raw: string | null): string {
  if (!raw) return "—";
  // Accepts '2026-05-17 18:40:06' OR ISO 8601. We treat the SIAP timestamp as
  // local time (it is) and format day + month name + year only.
  const isoish = raw.includes("T") ? raw : raw.replace(" ", "T");
  const d = new Date(isoish);
  if (Number.isNaN(d.getTime())) return raw;
  return `${d.getDate()} ${BULAN_ID[d.getMonth() + 1]} ${d.getFullYear()}`;
}

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
          className="inline-flex items-center gap-2 text-sm text-text-secondary transition hover:text-brand-amber"
        >
          <ArrowLeft className="size-4" aria-hidden="true" />
          <span>Kembali ke beranda BIMA</span>
        </Link>

        <header className="mt-2">
          <p className="text-xs font-medium uppercase tracking-[0.18em] text-brand-amber">
            Lacak Permohonan Izin
          </p>
          <h1 className="mt-3 font-display text-2xl font-semibold leading-tight tracking-tight text-text-primary sm:text-3xl md:text-4xl">
            Tiket{" "}
            <span className="font-mono text-xl text-brand-amber sm:text-3xl md:text-4xl">
              {padded}
            </span>
          </h1>
        </header>

        {result.kind === "ok" && <StatusCard record={result.record} />}
        {result.kind === "not_found" && <NotFoundCard ticket={padded} />}
        {result.kind === "service_down" && (
          <ServiceDownCard ticket={padded} reason={result.reason} />
        )}

        <CtaFooter />
      </div>
    </main>
  );
}

// ----- Subviews --------------------------------------------------------------

function StatusCard({ record }: { record: TrackingRecord }) {
  return (
    <section
      aria-labelledby="status-heading"
      className="rounded-2xl bg-white/[0.03] p-6 ring-1 ring-white/[0.04] backdrop-blur-sm sm:p-8"
    >
      <h2 id="status-heading" className="sr-only">
        Status permohonan
      </h2>

      <div className="space-y-1">
        <p className="text-xs uppercase tracking-wide text-text-secondary">
          Permohonan
        </p>
        <p className="font-display text-xl font-semibold leading-snug text-text-primary">
          {record.license_name}
        </p>
        {record.sector_name && (
          <p className="text-sm text-text-secondary">{record.sector_name}</p>
        )}
      </div>

      <dl className="mt-8 grid gap-x-6 gap-y-5 sm:grid-cols-2">
        <Field label="Pemohon" value={record.applicant_name} />
        <Field label="Tanggal daftar" value={formatDateID(record.submitted_at)} />
        <Field label="Posisi berkas" value={record.current_desk} />
        <Field
          label="Status"
          value={record.status}
          valueClassName="text-brand-amber font-semibold capitalize"
        />
      </dl>
    </section>
  );
}

function NotFoundCard({ ticket }: { ticket: string }) {
  return (
    <section className="rounded-2xl bg-white/[0.03] p-6 ring-1 ring-white/[0.04] backdrop-blur-sm sm:p-8">
      <h2 className="font-display text-xl font-semibold text-text-primary">
        Tiket tidak ditemukan
      </h2>
      <p className="mt-3 text-text-secondary">
        Kami tidak menemukan permohonan dengan tiket{" "}
        <span className="font-mono text-text-primary">{ticket}</span>. Coba
        periksa kembali nomor tiket Anda — biasanya 9 digit angka yang tercetak
        pada tanda terima saat Anda mendaftar.
      </p>
    </section>
  );
}

function ServiceDownCard({ ticket, reason }: { ticket: string; reason: string }) {
  return (
    <section className="rounded-2xl bg-brand-amber/[0.04] p-6 ring-1 ring-brand-amber/20 backdrop-blur-sm sm:p-8">
      <h2 className="font-display text-xl font-semibold text-text-primary">
        Sementara tidak dapat memeriksa
      </h2>
      <p className="mt-3 text-text-secondary">
        {reason} Silakan coba lagi beberapa menit ke depan.
      </p>
      <p className="mt-2 text-sm text-text-secondary">
        Atau tanyakan langsung ke BIMA via WhatsApp:{" "}
        <span className="text-text-primary">
          status izin {ticket}
        </span>
      </p>
    </section>
  );
}

function Field({
  label,
  value,
  valueClassName = "text-text-primary",
}: {
  label: string;
  value: string;
  valueClassName?: string;
}) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-text-secondary">
        {label}
      </dt>
      <dd className={`mt-1 ${valueClassName}`}>{value || "—"}</dd>
    </div>
  );
}

function CtaFooter() {
  return (
    <div className="mt-6 rounded-xl border border-white/5 bg-white/[0.02] p-5">
      <p className="text-sm text-text-secondary">
        Ada pertanyaan lanjutan? BIMA siap membantu di WhatsApp.
      </p>
      <Button asChild variant="primary" size="lg" className="mt-4">
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
