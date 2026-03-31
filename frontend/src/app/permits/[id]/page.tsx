"use client";

import {
  ArrowLeft,
  Building2,
  Calendar,
  FileText,
  Hash,
  MapPin,
  Users,
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
          </>
        )}
      </div>
    </AppLayout>
  );
}
