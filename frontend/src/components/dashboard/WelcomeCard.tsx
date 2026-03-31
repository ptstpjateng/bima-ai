import { Building2, CheckCircle2, Clock } from "lucide-react";
import type { PermitApplication, User } from "@/types";
import { getGreeting } from "@/lib/utils";

interface WelcomeCardProps {
  user: User;
  permits: PermitApplication[];
}

export function WelcomeCard({ user, permits }: WelcomeCardProps) {
  const activeCount = permits.filter((p) =>
    ["submitted", "under_review", "approved"].includes(p.status),
  ).length;
  const approvedCount = permits.filter((p) => p.status === "approved").length;
  const pendingCount = permits.filter((p) =>
    ["submitted", "under_review", "additional_docs_required"].includes(p.status),
  ).length;

  const businessName = user.business_name ?? user.name;

  return (
    <div className="relative overflow-hidden rounded-2xl bg-gradient-to-br from-brand-800 to-brand-600 p-6 text-white shadow-lg">
      {/* Decorative circles */}
      <div className="absolute -right-8 -top-8 h-32 w-32 rounded-full bg-white/10" />
      <div className="absolute -bottom-12 -right-4 h-40 w-40 rounded-full bg-white/5" />

      <div className="relative">
        <p className="mb-1 text-sm text-brand-200">
          {getGreeting()}, 👋
        </p>
        <h1 className="mb-0.5 text-xl font-bold leading-tight">
          {businessName}
        </h1>
        {user.city && (
          <p className="mb-5 flex items-center gap-1 text-sm text-brand-200">
            <Building2 className="h-3.5 w-3.5" />
            {user.city}, {user.province}
          </p>
        )}
        {!user.city && <div className="mb-5" />}

        {/* Quick stats */}
        <div className="grid grid-cols-3 gap-3">
          <div className="rounded-xl bg-white/15 p-3 text-center backdrop-blur-sm">
            <p className="text-2xl font-bold">{activeCount}</p>
            <p className="text-[10px] leading-tight text-brand-100">Perizinan Aktif</p>
          </div>
          <div className="rounded-xl bg-white/15 p-3 text-center backdrop-blur-sm">
            <div className="flex items-center justify-center gap-1">
              <CheckCircle2 className="h-4 w-4 text-green-300" />
              <p className="text-2xl font-bold">{approvedCount}</p>
            </div>
            <p className="text-[10px] leading-tight text-brand-100">Disetujui</p>
          </div>
          <div className="rounded-xl bg-white/15 p-3 text-center backdrop-blur-sm">
            <div className="flex items-center justify-center gap-1">
              <Clock className="h-4 w-4 text-yellow-300" />
              <p className="text-2xl font-bold">{pendingCount}</p>
            </div>
            <p className="text-[10px] leading-tight text-brand-100">Menunggu</p>
          </div>
        </div>
      </div>
    </div>
  );
}
