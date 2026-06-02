import type { Metadata } from "next";
import { redirect } from "next/navigation";
import Link from "next/link";
import {
  ArrowLeft,
  MapPin,
  Phone,
  User as UserIcon,
  LayoutDashboard,
  FileText,
} from "lucide-react";

import { LogoutButton } from "@/components/me/LogoutButton";
import { Button } from "@/components/ui/button";
import { getServerUser } from "@/lib/auth-server";

/**
 * Profile page — protected (middleware + page-level getServerUser). Identity-
 * only: the rich post-licensing data lives at /dashboard, which this page
 * links to prominently. Migrated to the Bima token system.
 */
export const metadata: Metadata = {
  title: "Akun Saya · BIMA",
  description: "Profil akun BIMA dan ringkasan permohonan perizinan.",
  robots: { index: false },
};

/** Mask all but the last 4 digits of NIK (16) / NPWP (15). PII hygiene. */
function maskIdentity(value: string): string {
  if (value.length < 5) return value;
  const visible = value.slice(-4);
  const hidden = "•".repeat(value.length - 4);
  return `${hidden} ${visible}`;
}

export default async function MePage() {
  const user = await getServerUser();
  if (!user) {
    redirect("/login?next=/me");
  }

  return (
    <main className="min-h-screen px-4 py-10 sm:px-6 sm:py-12 lg:px-8">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-6">
        <div className="flex items-center justify-between gap-4">
          <Link
            href="/"
            className="inline-flex items-center gap-2 text-sm text-ink-2 transition-colors hover:text-ink"
          >
            <ArrowLeft className="size-4" aria-hidden="true" />
            <span>Beranda</span>
          </Link>
          <LogoutButton />
        </div>

        <header className="mt-2">
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-brand-green-text">
            Akun Saya
          </p>
          <h1 className="mt-3 font-display text-2xl font-bold leading-tight tracking-tight text-ink sm:text-3xl md:text-4xl">
            {user.name}
          </h1>
        </header>

        <section
          aria-labelledby="profile-heading"
          className="rounded-card border border-border bg-surface p-6 sm:p-8"
        >
          <h2 id="profile-heading" className="sr-only">
            Informasi profil
          </h2>

          <dl className="grid gap-x-6 gap-y-5 sm:grid-cols-2">
            <ProfileField
              icon={UserIcon}
              label={user.identity_type === "NPWP" ? "NPWP" : "NIK"}
              value={maskIdentity(user.identity_number)}
              valueClassName="font-mono"
            />
            <ProfileField icon={Phone} label="Nomor HP" value={user.mobile ?? "—"} />
            <ProfileField
              icon={MapPin}
              label="Kabupaten / Kota"
              value={user.kabupaten ?? "—"}
            />
          </dl>
        </section>

        <section
          aria-labelledby="summary-heading"
          className="rounded-card border border-border bg-surface p-6 sm:p-8"
        >
          <div className="flex items-center gap-3">
            <span className="inline-flex size-9 items-center justify-center rounded-pill bg-surface-2">
              <FileText className="size-4 text-primary" aria-hidden="true" />
            </span>
            <h2
              id="summary-heading"
              className="font-display text-lg font-semibold text-ink sm:text-xl"
            >
              Ringkasan izin
            </h2>
          </div>

          <p className="mt-4 text-sm text-ink-2 sm:text-base">
            Lihat izin aktif, SK yang terbit, jadwal perpanjangan, dan riwayat
            permohonan Anda di Dashboard Izin.
          </p>

          <Button asChild variant="primary" size="lg" className="mt-5">
            <Link href="/dashboard">
              <LayoutDashboard aria-hidden="true" />
              <span>Buka Dashboard</span>
            </Link>
          </Button>
        </section>
      </div>
    </main>
  );
}

function ProfileField({
  icon: Icon,
  label,
  value,
  valueClassName = "text-ink",
}: {
  icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  label: string;
  value: string;
  valueClassName?: string;
}) {
  return (
    <div className="flex items-start gap-3">
      <span className="mt-0.5 inline-flex size-8 shrink-0 items-center justify-center rounded-pill bg-surface-2">
        <Icon className="size-4 text-ink-2" aria-hidden />
      </span>
      <div className="min-w-0">
        <dt className="text-xs uppercase tracking-wide text-ink-3">{label}</dt>
        <dd className={`mt-1 break-words ${valueClassName}`}>{value}</dd>
      </div>
    </div>
  );
}
