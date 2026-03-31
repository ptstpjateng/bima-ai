import {
  AlertCircle,
  Building2,
  Calendar,
  ChevronRight,
  Hash,
} from "lucide-react";
import Link from "next/link";
import type { PermitApplication } from "@/types";
import {
  RISK_COLORS,
  RISK_LABELS,
  SCALE_LABELS,
  cn,
  formatDate,
} from "@/lib/utils";
import { StatusBadge } from "./StatusBadge";

interface PermitCardProps {
  permit: PermitApplication;
}

export function PermitCard({ permit }: PermitCardProps) {
  const needsAttention =
    permit.status === "additional_docs_required";

  return (
    <Link href={`/permits/${permit.id}`}>
      <div
        className={cn(
          "group rounded-2xl border bg-white p-5 shadow-sm transition-all hover:shadow-md",
          needsAttention
            ? "border-orange-200 ring-1 ring-orange-100"
            : "border-gray-100",
        )}
      >
        <div className="mb-3 flex items-start justify-between gap-3">
          <div className="flex-1 min-w-0">
            <div className="mb-1 flex items-center gap-2">
              <span className="rounded-md bg-brand-50 px-2 py-0.5 font-mono text-xs font-bold text-brand-700">
                {permit.kbli_code}
              </span>
              <span
                className={cn(
                  "rounded-md px-2 py-0.5 text-xs font-medium",
                  RISK_COLORS[permit.risk_level],
                )}
              >
                {RISK_LABELS[permit.risk_level]}
              </span>
            </div>
            <p className="text-sm font-semibold leading-snug text-gray-900 group-hover:text-brand-700 transition-colors line-clamp-2">
              {permit.kbli_description}
            </p>
          </div>
          <ChevronRight className="h-4 w-4 flex-shrink-0 text-gray-300 group-hover:text-brand-400 transition-colors mt-1" />
        </div>

        {needsAttention && (
          <div className="mb-3 flex items-start gap-2 rounded-xl bg-orange-50 px-3 py-2">
            <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-orange-500" />
            <p className="text-xs text-orange-700">
              Dokumen tambahan diperlukan. Segera lengkapi agar proses dilanjutkan.
            </p>
          </div>
        )}

        <div className="mt-3 grid grid-cols-2 gap-2 border-t border-gray-50 pt-3">
          <div className="flex items-center gap-1.5 text-xs text-gray-500">
            <Hash className="h-3.5 w-3.5 text-gray-400" />
            <span className="truncate font-mono">{permit.application_number}</span>
          </div>
          <div className="flex items-center gap-1.5 text-xs text-gray-500">
            <Building2 className="h-3.5 w-3.5 text-gray-400" />
            <span>{SCALE_LABELS[permit.business_scale]}</span>
          </div>
          <div className="col-span-2 flex items-center justify-between">
            <div className="flex items-center gap-1.5 text-xs text-gray-500">
              <Calendar className="h-3.5 w-3.5 text-gray-400" />
              <span>
                {permit.submitted_at
                  ? `Diajukan ${formatDate(permit.submitted_at)}`
                  : `Dibuat ${formatDate(permit.created_at)}`}
              </span>
            </div>
            <StatusBadge status={permit.status} />
          </div>
        </div>
      </div>
    </Link>
  );
}
