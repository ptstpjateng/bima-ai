"use client";

import { use } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Database, Inbox } from "lucide-react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { RiskBadge, SektorBadge } from "@/components/admin/badges";
import { apiFetch, ApiError } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import type { KbliDetail } from "@/lib/types";

/**
 * KBLI detail — all 14 fields organised into thematic sections.
 *
 * Fields per the admin-api contract:
 *   ruang_lingkup, parameter, kategori, golongan_pokok, golongan, subgolongan,
 *   sektor, tingkat_risiko, skala_usaha, perizinan_berusaha, pb_umku_names,
 *   jangka_waktu, masa_berlaku, kewenangan
 *
 * Plus: judul_kbli + kode_kbli (header) and chunk_count (knowledge badge).
 */
export default function KbliDetailPage({
  params,
}: {
  params: Promise<{ kode: string }>;
}) {
  const { kode } = use(params);
  const decoded = decodeURIComponent(kode);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["kbli", "detail", decoded],
    queryFn: () =>
      apiFetch<KbliDetail>(`/kbli/${encodeURIComponent(decoded)}`).catch(
        (err) => {
          if (err instanceof ApiError && err.status === 404) {
            return null as unknown as KbliDetail;
          }
          throw err;
        }
      ),
  });

  return (
    <div className="space-y-6 max-w-5xl">
      <Link
        href="/kbli"
        className="inline-flex items-center gap-1.5 text-sm text-text-secondary hover:text-text-primary transition-colors"
      >
        <ArrowLeft className="size-4" aria-hidden />
        Kembali ke pencarian
      </Link>

      {isLoading ? (
        <DetailSkeleton />
      ) : isError ? (
        <Card className="bg-surface-card border-0 rounded-card p-6 text-sm text-brand-rose">
          Tidak bisa memuat kode KBLI ini. Coba muat ulang halaman.
        </Card>
      ) : !data ? (
        <Card className="bg-surface-card border-0 rounded-card p-10 text-center">
          <Inbox className="size-8 text-text-muted mx-auto mb-3" aria-hidden />
          <p className="text-sm text-text-secondary">
            Kode KBLI tidak ditemukan.
          </p>
          <p className="text-xs text-text-muted mt-1">
            kode: <span className="font-mono">{decoded}</span>
          </p>
        </Card>
      ) : (
        <Detail data={data} />
      )}
    </div>
  );
}

function Detail({ data }: { data: KbliDetail }) {
  return (
    <>
      <Card className="bg-surface-card border-0 rounded-card p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="space-y-2">
            <div className="font-mono text-brand-amber text-3xl tracking-tight">
              {data.kode_kbli}
            </div>
            <h1 className="font-display text-2xl font-medium text-text-primary leading-tight">
              {data.judul_kbli}
            </h1>
            <div className="flex flex-wrap gap-1.5 mt-1">
              <SektorBadge sektor={data.sektor} />
              <RiskBadge risk={data.tingkat_risiko} />
              {data.skala_usaha && (
                <span className="inline-flex items-center rounded-pill bg-surface-card-hover text-text-secondary px-2 py-0.5 text-[11px]">
                  {data.skala_usaha}
                </span>
              )}
            </div>
          </div>

          <div className="rounded-card bg-brand-navy/15 px-4 py-3 text-right">
            <div className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-brand-navy-hover">
              <Database className="size-3" aria-hidden />
              Knowledge index
            </div>
            <div className="mt-1 font-display text-2xl text-text-primary tabular-nums">
              {formatNumber(data.chunk_count)}
            </div>
            <div className="text-[11px] text-text-muted">
              chunks indexed in BIMA&rsquo;s knowledge
            </div>
          </div>
        </div>
      </Card>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Section title="Klasifikasi">
          <Field label="Kategori" value={data.kategori} />
          <Field label="Golongan Pokok" value={data.golongan_pokok} />
          <Field label="Golongan" value={data.golongan} />
          <Field label="Subgolongan" value={data.subgolongan} />
        </Section>

        <Section title="Cakupan">
          <Field label="Ruang Lingkup" value={data.ruang_lingkup} multiline />
          <Field label="Parameter" value={data.parameter} multiline />
        </Section>

        <Section title="Risiko & Skala">
          <Field label="Sektor" value={data.sektor} />
          <Field label="Tingkat Risiko" value={data.tingkat_risiko} />
          <Field label="Skala Usaha" value={data.skala_usaha} />
        </Section>

        <Section title="Perizinan">
          <Field
            label="Perizinan Berusaha"
            value={data.perizinan_berusaha}
            multiline
          />
          <Field
            label="PB-UMKU"
            value={
              Array.isArray(data.pb_umku_names)
                ? data.pb_umku_names.join("\n")
                : data.pb_umku_names
            }
            multiline
          />
        </Section>

        <Section title="Masa Berlaku">
          <Field label="Jangka Waktu" value={data.jangka_waktu} />
          <Field label="Masa Berlaku" value={data.masa_berlaku} />
          <Field label="Kewenangan" value={data.kewenangan} />
        </Section>
      </div>
    </>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <Card className="bg-surface-card border-0 rounded-card">
      <CardHeader className="pb-2">
        <CardTitle className="font-display text-base font-medium text-text-primary">
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">{children}</CardContent>
    </Card>
  );
}

function Field({
  label,
  value,
  multiline = false,
}: {
  label: string;
  value: string | null | undefined;
  multiline?: boolean;
}) {
  return (
    <div className="space-y-1">
      <div className="text-[11px] uppercase tracking-wider text-text-muted">
        {label}
      </div>
      {value ? (
        multiline ? (
          <p className="text-sm text-text-primary whitespace-pre-wrap leading-relaxed">
            {value}
          </p>
        ) : (
          <p className="text-sm text-text-primary">{value}</p>
        )
      ) : (
        <p className="text-sm text-text-muted">—</p>
      )}
    </div>
  );
}

function DetailSkeleton() {
  return (
    <>
      <Skeleton className="h-32 w-full bg-surface-card" />
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-48 w-full bg-surface-card" />
        ))}
      </div>
    </>
  );
}
