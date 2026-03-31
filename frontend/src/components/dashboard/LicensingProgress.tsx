import {
  CheckCircle2,
  Circle,
  FileSearch,
  FileText,
  MessageCircle,
  Send,
  Shield,
} from "lucide-react";
import type { PermitApplication } from "@/types";
import { cn } from "@/lib/utils";

interface LicensingProgressProps {
  permits: PermitApplication[];
  hasAiSession: boolean;
}

interface Step {
  id: string;
  label: string;
  description: string;
  icon: React.ElementType;
  completed: boolean;
  active: boolean;
}

export function LicensingProgress({
  permits,
  hasAiSession,
}: LicensingProgressProps) {
  const hasAnyPermit = permits.length > 0;
  const hasSubmitted = permits.some((p) => p.status !== "draft");
  const hasApproved = permits.some((p) => p.status === "approved");
  const hasUnderReview = permits.some((p) =>
    ["under_review", "additional_docs_required"].includes(p.status),
  );

  const steps: Step[] = [
    {
      id: "consultation",
      label: "Konsultasi AI",
      description: "Chat dengan asisten BIMA-AI via WhatsApp",
      icon: MessageCircle,
      completed: hasAiSession,
      active: !hasAiSession,
    },
    {
      id: "classification",
      label: "Klasifikasi KBLI",
      description: "Identifikasi kode bisnis & tingkat risiko",
      icon: FileSearch,
      completed: hasAnyPermit,
      active: hasAiSession && !hasAnyPermit,
    },
    {
      id: "submission",
      label: "Pengajuan Dokumen",
      description: "Ajukan permohonan izin ke DPMPTSP",
      icon: Send,
      completed: hasSubmitted,
      active: hasAnyPermit && !hasSubmitted,
    },
    {
      id: "review",
      label: "Proses Peninjauan",
      description: "DPMPTSP meninjau permohonan Anda",
      icon: FileText,
      completed: hasUnderReview || hasApproved,
      active: hasSubmitted && !hasUnderReview && !hasApproved,
    },
    {
      id: "approved",
      label: "Izin Diterbitkan",
      description: "Izin usaha Anda telah disetujui",
      icon: Shield,
      completed: hasApproved,
      active: hasUnderReview && !hasApproved,
    },
  ];

  const completedCount = steps.filter((s) => s.completed).length;
  const progress = Math.round((completedCount / steps.length) * 100);

  return (
    <div className="rounded-2xl border border-gray-100 bg-white p-6 shadow-sm">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="text-base font-bold text-gray-900">
            Progres Perizinan
          </h2>
          <p className="text-xs text-gray-500">
            {completedCount} dari {steps.length} tahap selesai
          </p>
        </div>
        <div className="text-right">
          <span className="text-2xl font-bold text-brand-700">{progress}%</span>
        </div>
      </div>

      {/* Progress bar */}
      <div className="mb-5 h-2 overflow-hidden rounded-full bg-gray-100">
        <div
          className="h-full rounded-full bg-gradient-to-r from-brand-500 to-brand-700 transition-all duration-700"
          style={{ width: `${progress}%` }}
        />
      </div>

      {/* Steps */}
      <div className="space-y-3">
        {steps.map((step) => {
          const Icon = step.icon;
          return (
            <div key={step.id} className="flex items-start gap-3">
              {/* Step icon */}
              <div
                className={cn(
                  "flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full transition-colors",
                  step.completed
                    ? "bg-green-100 text-green-600"
                    : step.active
                      ? "bg-brand-100 text-brand-600"
                      : "bg-gray-100 text-gray-400",
                )}
              >
                {step.completed ? (
                  <CheckCircle2 className="h-4 w-4" />
                ) : step.active ? (
                  <Icon className="h-4 w-4" />
                ) : (
                  <Circle className="h-4 w-4" />
                )}
              </div>

              {/* Connector line */}
              <div className="flex-1">
                <p
                  className={cn(
                    "text-sm font-medium",
                    step.completed
                      ? "text-green-700"
                      : step.active
                        ? "text-brand-700"
                        : "text-gray-400",
                  )}
                >
                  {step.label}
                </p>
                <p className="text-xs text-gray-400">{step.description}</p>
              </div>

              {step.active && (
                <span className="mt-0.5 rounded-full bg-brand-100 px-2 py-0.5 text-[10px] font-semibold text-brand-700">
                  Saat ini
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
