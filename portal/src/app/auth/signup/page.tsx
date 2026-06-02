import type { Metadata } from "next";

import { AuthLayout } from "@/components/auth/AuthLayout";
import { SoonChip } from "@/components/auth/SoonChip";
import { StepIndicator } from "@/components/auth/StepIndicator";
import { MagicLinkForm } from "@/components/auth/MagicLinkForm";

/**
 * /auth/signup — SCAFFOLD ONLY (magic-link account create).
 *
 * The UI is fully built + accessible, but the underlying flow is not wired:
 * SIAP exposes no passwordless/register endpoint to the portal (see
 * lib/auth-flow.ts TODO). On submit, the typed stub returns not_implemented
 * and the honest NotAvailablePanel appears, pointing the user at the working
 * NIK/NPWP login and SIAP's own portal. NEVER a fake logged-in state.
 */
export const metadata: Metadata = {
  title: "Buat Akun · BIMA",
  description: "Daftar akun BIMA dengan tautan masuk lewat email.",
  robots: { index: false },
};

export default function SignupPage() {
  return (
    <AuthLayout
      eyebrow="Buat Akun"
      title="Daftar dengan tautan masuk"
      lede="Masukkan email Anda dan kami kirimkan tautan masuk — tanpa kata sandi."
      headerSlot={<SoonChip />}
      backHref="/login"
      backLabel="Kembali ke login"
    >
      <StepIndicator
        steps={["Email", "Verifikasi", "Selesai"]}
        current={0}
      />
      <MagicLinkForm
        mode="signup"
        submitLabel="Kirim tautan masuk"
        hint="Gunakan email yang terdaftar di akun SIAP Jateng Anda."
      />
    </AuthLayout>
  );
}
