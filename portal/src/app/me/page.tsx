import type { Metadata } from "next";
import { redirect } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, FileText, MapPin, Phone, User as UserIcon } from "lucide-react";

import { LogoutButton } from "@/components/me/LogoutButton";
import { getServerUser } from "@/lib/auth-server";

/**
 * Profile page — protected route, gated by middleware AND by a fresh
 * admin-api check here. Middleware blocks at the edge for cheap; this
 * page-level check catches the edge case where the cookie is present but
 * the JWT has been revoked or admin-api rejects it.
 *
 * Per the Phase 1 brief, the permit list ("active_permits") is stubbed.
 * The placeholder makes the empty state look intentional.
 */

export const metadata: Metadata = {
  title: "Akun Saya · BIMA",
  description: "Profil akun BIMA dan ringkasan permohonan perizinan.",
  robots: { index: false },
};

/**
 * Mask all but the last 4 digits of a credential. Works for both NIK (16)
 * and NPWP (15). PII surface hygiene.
 */
function maskIdentity(value: string): string {
  if (value.length < 5) return value;
  const visible = value.slice(-4);
  const hidden = "•".repeat(value.length - 4);
  return `${hidden} ${visible}`;
}

export default async function MePage() {
  const user = await getServerUser();
  if (!user) {
    // Middleware should have caught this, but defense in depth — if a stale
    // cookie made it past, push the user back through login with a return.
    redirect("/login?next=/me");
  }

  return (
    <main className="min-h-screen px-4 py-10 sm:px-6 sm:py-12 lg:px-8">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-6">
        <div className="flex items-center justify-between gap-4">
          <Link
            href="/"
            className="inline-flex items-center gap-2 text-sm text-text-secondary transition hover:text-brand-amber"
          >
            <ArrowLeft className="size-4" aria-hidden="true" />
            <span>Beranda</span>
          </Link>
          <LogoutButton />
        </div>

        <header className="mt-2">
          <p className="text-xs font-medium uppercase tracking-[0.18em] text-brand-amber">
            Akun Saya
          </p>
          <h1 className="mt-3 font-display text-2xl font-semibold leading-tight tracking-tight text-text-primary sm:text-3xl md:text-4xl">
            {user.name}
          </h1>
        </header>

        <section
          aria-labelledby="profile-heading"
          className="rounded-card bg-surface-card p-6 sm:p-8"
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
            <ProfileField
              icon={Phone}
              label="Nomor HP"
              value={user.mobile ?? "—"}
            />
            <ProfileField
              icon={MapPin}
              label="Kabupaten / Kota"
              value={user.kabupaten ?? "—"}
            />
          </dl>
        </section>

        <section
          aria-labelledby="permits-heading"
          className="rounded-card bg-surface-card p-6 sm:p-8"
        >
          <div className="flex items-center gap-3">
            <span className="inline-flex h-9 w-9 items-center justify-center rounded-pill bg-brand-amber/10">
              <FileText className="size-4 text-brand-amber" aria-hidden="true" />
            </span>
            <h2
              id="permits-heading"
              className="font-display text-lg font-semibold text-text-primary sm:text-xl"
            >
              Permohonan Aktif
            </h2>
          </div>

          <p className="mt-4 text-sm text-text-secondary sm:text-base">
            Belum ada permohonan aktif yang ter-link dengan akun ini di SIAP
            Jateng. Ajukan izin lewat OSS RBA — BIMA akan memandu di WhatsApp.
          </p>
        </section>
      </div>
    </main>
  );
}

function ProfileField({
  icon: Icon,
  label,
  value,
  valueClassName = "text-text-primary",
}: {
  icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  label: string;
  value: string;
  valueClassName?: string;
}) {
  return (
    <div className="flex items-start gap-3">
      <span className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-pill bg-surface-base">
        <Icon className="size-4 text-text-secondary" aria-hidden />
      </span>
      <div className="min-w-0">
        <dt className="text-xs uppercase tracking-wide text-text-secondary">
          {label}
        </dt>
        <dd className={`mt-1 break-words ${valueClassName}`}>{value}</dd>
      </div>
    </div>
  );
}
