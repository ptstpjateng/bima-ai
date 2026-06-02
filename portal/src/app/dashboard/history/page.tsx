import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { getServerUser } from "@/lib/auth-server";
import { getCitizenApplications } from "@/lib/citizen-dashboard-client";
import { DashboardShell } from "@/components/dashboard/DashboardShell";
import { ApplicationHistoryTable } from "@/components/dashboard/ApplicationHistoryTable";

/**
 * /dashboard/history — full, filterable, sortable application history. MOCK
 * data via the typed stub (banner makes that explicit).
 */
export const metadata: Metadata = {
  title: "Riwayat Permohonan · BIMA",
  robots: { index: false },
};

export default async function HistoryPage() {
  const user = await getServerUser();
  if (!user) redirect("/login?next=/dashboard/history");

  const applications = await getCitizenApplications();

  return (
    <DashboardShell
      eyebrow="Riwayat"
      title="Riwayat permohonan"
      lede="Semua permohonan izin Anda — filter berdasarkan status, urutkan berdasarkan tanggal, dan lacak setiap tiket."
      backHref="/dashboard"
      backLabel="Dashboard"
    >
      <ApplicationHistoryTable applications={applications} />
    </DashboardShell>
  );
}
