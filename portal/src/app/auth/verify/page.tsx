import type { Metadata } from "next";

import { AuthLayout } from "@/components/auth/AuthLayout";
import { SoonChip } from "@/components/auth/SoonChip";
import { StepIndicator } from "@/components/auth/StepIndicator";
import { VerifyForm } from "@/components/auth/VerifyForm";

/**
 * /auth/verify — SCAFFOLD ONLY (the 6-digit code step of the magic-link flow).
 *
 * Fully built + accessible, but verifyMagicLinkCode() returns not_implemented
 * (SIAP has no passwordless endpoint). Submitting reveals the honest
 * NotAvailablePanel. NEVER fabricates a session or routes to /me.
 *
 * `?email=` is optional context for the copy; nothing is sent server-side.
 */
export const metadata: Metadata = {
  title: "Verifikasi Kode · BIMA",
  description: "Masukkan kode verifikasi 6 digit untuk masuk ke BIMA.",
  robots: { index: false },
};

export default async function VerifyPage({
  searchParams,
}: {
  searchParams: Promise<{ email?: string }>;
}) {
  const { email } = await searchParams;

  return (
    <AuthLayout
      eyebrow="Verifikasi"
      title="Masukkan kode verifikasi"
      lede="Cek email Anda untuk kode 6 digit, lalu masukkan di bawah ini."
      headerSlot={<SoonChip />}
      backHref="/auth/signup"
      backLabel="Kembali"
    >
      <StepIndicator steps={["Email", "Verifikasi", "Selesai"]} current={1} />
      <VerifyForm email={email ?? null} />
    </AuthLayout>
  );
}
