"use client";

import {
  ArrowLeft,
  Building2,
  Calendar,
  CheckCircle2,
  ChevronRight,
  Circle,
  Clock,
  Download,
  FileText,
  Hash,
  MapPin,
  PlusCircle,
  Users,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { use } from "react";
import { AppLayout } from "@/components/layout/AppLayout";
import { StatusBadge } from "@/components/permits/StatusBadge";
import { usePermits } from "@/hooks/usePermits";
import {
  RISK_LABELS,
  SCALE_LABELS,
  cn,
  formatCurrency,
  formatDate,
} from "@/lib/utils";
import { Skeleton } from "@/components/shared/LoadingSkeleton";
import type { PermitApplication } from "@/types";

interface Props {
  params: Promise<{ id: string }>;
}

function InfoRow({
  label,
  value,
  icon: Icon,
}: {
  label: string;
  value: React.ReactNode;
  icon?: React.ElementType;
}) {
  return (
    <div className="flex items-start gap-3 border-b border-gray-50 py-3 last:border-0">
      {Icon && (
        <div className="mt-0.5 flex-shrink-0">
          <Icon className="h-4 w-4 text-gray-400" />
        </div>
      )}
      <div className="flex-1 min-w-0">
        <p className="text-xs font-medium text-gray-500">{label}</p>
        <p className="mt-0.5 text-sm text-gray-900">{value || "-"}</p>
      </div>
    </div>
  );
}

function NextActionCard({ permit }: { permit: PermitApplication }) {
  const config: Record<
    string,
    { icon: React.ElementType; color: string; title: string; body: string; href?: string; cta?: string }
  > = {
    draft: {
      icon: PlusCircle,
      color: "blue",
      title: "Lengkapi Permohonan",
      body: "Permohonan Anda masih berstatus draf. Lanjutkan pengisian formulir untuk mengajukan izin.",
      href: "/permits/apply",
      cta: "Lanjutkan Pengisian",
    },
    submitted: {
      icon: Clock,
      color: "indigo",
      title: "Menunggu Peninjauan",
      body: "Permohonan Anda telah diterima dan sedang menunggu antrian peninjauan oleh petugas DPMPTSP.",
    },
    under_review: {
      icon: Clock,
      color: "yellow",
      title: "Sedang Diproses",
      body: "Petugas sedang memeriksa kelengkapan dan kebenaran dokumen Anda. Harap tunggu notifikasi selanjutnya.",
    },
    additional_docs_required: {
      icon: FileText,
      color: "orange",
      title: "Unggah Dokumen Tambahan",
      body: "Petugas membutuhkan dokumen tambahan. Silakan siapkan dokumen yang diminta dan hubungi petugas DPMPTSP.",
    },
    approved: {
      icon: CheckCircle2,
      color: "green",
      title: "Kewajiban Pasca Izin",
      body: "Izin usaha Anda telah aktif. Pastikan Anda memenuhi kewajiban pelaporan LKPM setiap triwulan melalui portal OSS.",
      href: "https://oss.go.id",
      cta: "Buka Portal OSS",
    },
    rejected: {
      icon: XCircle,
      color: "red",
      title: "Ajukan Permohonan Baru",
      body: "Permohonan Anda ditolak. Perbaiki kekurangan yang disebutkan dalam catatan penolakan, lalu ajukan permohonan baru.",
      href: "/permits/apply",
      cta: "Ajukan Ulang",
    },
  };

  const cfg = config[permit.status];
  if (!cfg) return null;

  const Icon = cfg.icon;
  const colorMap: Record<string, string> = {
    blue: "border-blue-200 bg-blue-50",
    indigo: "border-indigo-200 bg-indigo-50",
    yellow: "border-yellow-200 bg-yellow-50",
    orange: "border-orange-200 bg-orange-50",
    green: "border-green-200 bg-green-50",
    red: "border-red-200 bg-red-50",
  };
  const iconColorMap: Record<string, string> = {
    blue: "text-blue-600",
    indigo: "text-indigo-600",
    yellow: "text-yellow-600",
    orange: "text-orange-600",
    green: "text-green-600",
    red: "text-red-600",
  };
  const titleColorMap: Record<string, string> = {
    blue: "text-blue-900",
    indigo: "text-indigo-900",
    yellow: "text-yellow-900",
    orange: "text-orange-900",
    green: "text-green-900",
    red: "text-red-900",
  };
  const bodyColorMap: Record<string, string> = {
    blue: "text-blue-700",
    indigo: "text-indigo-700",
    yellow: "text-yellow-700",
    orange: "text-orange-700",
    green: "text-green-700",
    red: "text-red-700",
  };
  const ctaColorMap: Record<string, string> = {
    blue: "bg-blue-600 hover:bg-blue-700",
    indigo: "bg-indigo-600 hover:bg-indigo-700",
    yellow: "bg-yellow-600 hover:bg-yellow-700",
    orange: "bg-orange-600 hover:bg-orange-700",
    green: "bg-green-600 hover:bg-green-700",
    red: "bg-red-600 hover:bg-red-700",
  };

  return (
    <div className={cn("rounded-2xl border p-5", colorMap[cfg.color])}>
      <div className="flex items-start gap-3">
        <Icon className={cn("mt-0.5 h-5 w-5 flex-shrink-0", iconColorMap[cfg.color])} />
        <div className="flex-1">
          <p className={cn("text-sm font-bold", titleColorMap[cfg.color])}>{cfg.title}</p>
          <p className={cn("mt-0.5 text-xs leading-relaxed", bodyColorMap[cfg.color])}>
            {cfg.body}
          </p>
          {cfg.href && cfg.cta && (
            <Link
              href={cfg.href}
              target={cfg.href.startsWith("http") ? "_blank" : undefined}
              rel={cfg.href.startsWith("http") ? "noopener noreferrer" : undefined}
              className={cn(
                "mt-3 inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold text-white transition-colors",
                ctaColorMap[cfg.color],
              )}
            >
              {cfg.cta}
              <ChevronRight className="h-3 w-3" />
            </Link>
          )}
        </div>
      </div>
    </div>
  );
}

export default function PermitDetailPage({ params }: Props) {
  const { id } = use(params);
  const { permits, isLoading } = usePermits();

  const permit = permits.find((p) => p.id === Number(id));

  return (
    <AppLayout>
      <div className="mx-auto max-w-2xl space-y-5">
        {/* Back link */}
        <Link
          href="/permits"
          className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" />
          Kembali ke Perizinan
        </Link>

        {isLoading ? (
          <div className="rounded-2xl border border-gray-100 bg-white p-6 shadow-sm">
            <Skeleton className="mb-4 h-6 w-2/3" />
            <Skeleton className="mb-2 h-4 w-1/3" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        ) : !permit ? (
          <div className="rounded-2xl border border-gray-100 bg-white p-12 text-center shadow-sm">
            <FileText className="mx-auto mb-3 h-12 w-12 text-gray-300" />
            <p className="font-semibold text-gray-700">Permohonan tidak ditemukan</p>
            <p className="mt-1 text-sm text-gray-400">
              Permohonan dengan ID #{id} tidak ditemukan atau tidak dapat diakses.
            </p>
          </div>
        ) : (
          <>
            {/* Next action guidance */}
            <NextActionCard permit={permit} />

            {/* Header card */}
            <div className="rounded-2xl border border-gray-100 bg-white p-6 shadow-sm">
              <div className="mb-4 flex items-start justify-between gap-3">
                <div>
                  <div className="mb-2 flex items-center gap-2">
                    <span className="rounded-md bg-brand-50 px-2 py-0.5 font-mono text-xs font-bold text-brand-700">
                      {permit.kbli_code}
                    </span>
                    <StatusBadge status={permit.status} />
                  </div>
                  <h1 className="text-lg font-bold text-gray-900">
                    {permit.kbli_description}
                  </h1>
                  {permit.kbli_section && (
                    <p className="mt-0.5 text-sm text-gray-500">
                      {permit.kbli_section}
                    </p>
                  )}
                </div>
              </div>

              {/* Status timeline dots */}
              {permit.status === "additional_docs_required" && (
                <div className="rounded-xl border border-orange-200 bg-orange-50 p-4">
                  <p className="text-sm font-semibold text-orange-800">
                    ⚠️ Dokumen Tambahan Diperlukan
                  </p>
                  {permit.reviewer_notes && (
                    <p className="mt-1 text-sm text-orange-700">
                      {permit.reviewer_notes}
                    </p>
                  )}
                </div>
              )}

              {permit.status === "approved" && (
                <div className="rounded-xl border border-green-200 bg-green-50 p-4">
                  <p className="text-sm font-semibold text-green-800">
                    ✓ Izin Telah Disetujui
                  </p>
                  {permit.nib && (
                    <p className="mt-1 text-sm text-green-700">
                      NIB: <span className="font-mono font-bold">{permit.nib}</span>
                    </p>
                  )}
                  {permit.permit_expiry_date && (
                    <p className="mt-0.5 text-sm text-green-600">
                      Berlaku hingga: {formatDate(permit.permit_expiry_date)}
                    </p>
                  )}
                </div>
              )}

              {permit.status === "rejected" && permit.reviewer_notes && (
                <div className="rounded-xl border border-red-200 bg-red-50 p-4">
                  <p className="text-sm font-semibold text-red-800">
                    Permohonan Ditolak
                  </p>
                  <p className="mt-1 text-sm text-red-700">
                    {permit.reviewer_notes}
                  </p>
                </div>
              )}
            </div>

            {/* Details grid */}
            <div className="grid gap-5 sm:grid-cols-2">
              {/* Application info */}
              <div className="rounded-2xl border border-gray-100 bg-white p-5 shadow-sm">
                <h2 className="mb-3 text-sm font-bold text-gray-900">
                  Informasi Permohonan
                </h2>
                <InfoRow
                  label="Nomor Permohonan"
                  value={<span className="font-mono">{permit.application_number}</span>}
                  icon={Hash}
                />
                {permit.oss_application_number && (
                  <InfoRow
                    label="Nomor OSS"
                    value={<span className="font-mono">{permit.oss_application_number}</span>}
                    icon={Hash}
                  />
                )}
                <InfoRow
                  label="Tanggal Pengajuan"
                  value={formatDate(permit.submitted_at ?? permit.created_at)}
                  icon={Calendar}
                />
                {permit.approved_at && (
                  <InfoRow
                    label="Tanggal Persetujuan"
                    value={formatDate(permit.approved_at)}
                    icon={Calendar}
                  />
                )}
              </div>

              {/* Business info */}
              <div className="rounded-2xl border border-gray-100 bg-white p-5 shadow-sm">
                <h2 className="mb-3 text-sm font-bold text-gray-900">
                  Detail Usaha
                </h2>
                <InfoRow
                  label="Skala Usaha"
                  value={SCALE_LABELS[permit.business_scale]}
                  icon={Building2}
                />
                <InfoRow
                  label="Tingkat Risiko OSS"
                  value={
                    <span
                      className={cn(
                        "rounded-md px-2 py-0.5 text-xs font-medium",
                        permit.risk_level === "rendah"
                          ? "bg-green-50 text-green-700"
                          : permit.risk_level === "menengah_rendah"
                            ? "bg-yellow-50 text-yellow-700"
                            : permit.risk_level === "menengah_tinggi"
                              ? "bg-orange-50 text-orange-700"
                              : "bg-red-50 text-red-700",
                      )}
                    >
                      {RISK_LABELS[permit.risk_level]}
                    </span>
                  }
                />
                {permit.annual_revenue_estimate && (
                  <InfoRow
                    label="Estimasi Omset"
                    value={formatCurrency(permit.annual_revenue_estimate)}
                  />
                )}
                {permit.employee_count && (
                  <InfoRow
                    label="Jumlah Karyawan"
                    value={`${permit.employee_count} orang`}
                    icon={Users}
                  />
                )}
              </div>
            </div>

            {/* Location */}
            {(permit.business_location_city || permit.business_location_address) && (
              <div className="rounded-2xl border border-gray-100 bg-white p-5 shadow-sm">
                <div className="flex items-center gap-2 mb-3">
                  <MapPin className="h-4 w-4 text-gray-400" />
                  <h2 className="text-sm font-bold text-gray-900">
                    Lokasi Usaha
                  </h2>
                </div>
                {permit.business_location_province && (
                  <p className="text-sm text-gray-600">
                    {permit.business_location_city
                      ? `${permit.business_location_city}, `
                      : ""}
                    {permit.business_location_province}
                  </p>
                )}
                {permit.business_location_address && (
                  <p className="mt-1 text-sm text-gray-500">
                    {permit.business_location_address}
                  </p>
                )}
              </div>
            )}

            {/* Applicant notes */}
            {permit.applicant_notes && (
              <div className="rounded-2xl border border-gray-100 bg-white p-5 shadow-sm">
                <h2 className="mb-2 text-sm font-bold text-gray-900">
                  Catatan Permohonan
                </h2>
                <p className="text-sm text-gray-600 leading-relaxed">
                  {permit.applicant_notes}
                </p>
              </div>
            )}

            {/* Requirements checklist */}
            {permit.requirements_checklist && permit.requirements_checklist.length > 0 && (
              <div className="rounded-2xl border border-gray-100 bg-white p-5 shadow-sm">
                <h2 className="mb-3 text-sm font-bold text-gray-900">
                  Persyaratan & Checklist
                </h2>
                <ul className="space-y-2">
                  {permit.requirements_checklist.map((item, i) => (
                    <li key={i} className="flex items-start gap-2.5">
                      {item.completed ? (
                        <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0 text-green-500" />
                      ) : (
                        <Circle className="mt-0.5 h-4 w-4 flex-shrink-0 text-gray-300" />
                      )}
                      <div>
                        <p className={cn("text-sm", item.completed ? "text-gray-700" : "text-gray-500")}>
                          {item.label}
                        </p>
                        {item.notes && (
                          <p className="mt-0.5 text-xs text-gray-400">{item.notes}</p>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* Documents */}
            {permit.documents && permit.documents.length > 0 && (
              <div className="rounded-2xl border border-gray-100 bg-white p-5 shadow-sm">
                <h2 className="mb-3 text-sm font-bold text-gray-900">
                  Dokumen Permohonan
                </h2>
                <ul className="space-y-2">
                  {permit.documents.map((doc, i) => (
                    <li
                      key={i}
                      className="flex items-center justify-between gap-3 rounded-xl border border-gray-100 px-4 py-2.5"
                    >
                      <div className="flex items-center gap-2.5 min-w-0">
                        <FileText className="h-4 w-4 flex-shrink-0 text-brand-500" />
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium text-gray-800">
                            {doc.name}
                          </p>
                          <p className="text-xs text-gray-400 capitalize">
                            {doc.type}
                            {doc.uploaded_at ? ` · ${formatDate(doc.uploaded_at)}` : ""}
                          </p>
                        </div>
                      </div>
                      <a
                        href={doc.path}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="flex-shrink-0 rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 transition-colors"
                        title="Unduh dokumen"
                      >
                        <Download className="h-4 w-4" />
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </div>
    </AppLayout>
  );
}
