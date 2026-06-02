import type { Metadata } from "next";

import { AuthLayout } from "@/components/auth/AuthLayout";
import { SoonChip } from "@/components/auth/SoonChip";
import { MagicLinkForm } from "@/components/auth/MagicLinkForm";

/**
 * /auth/reset-password — SCAFFOLD ONLY.
 *
 * SIAP exposes no public password-reset API to the portal. The UI is built
 * and accessible; on submit, requestPasswordReset() returns not_implemented
 * and the honest NotAvailablePanel points the user at SIAP's own reset flow.
 * NEVER claims a reset email was sent.
 */
export const metadata: Metadata = {
  title: "Atur Ulang Kata Sandi · BIMA",
  description: "Atur ulang kata sandi akun SIAP Jateng Anda.",
  robots: { index: false },
};

export default function ResetPasswordPage() {
  return (
    <AuthLayout
      eyebrow="Atur Ulang Kata Sandi"
      title="Lupa kata sandi?"
      lede="Masukkan email akun Anda untuk menerima tautan pengaturan ulang."
      headerSlot={<SoonChip />}
      backHref="/login"
      backLabel="Kembali ke login"
    >
      <MagicLinkForm
        mode="reset"
        submitLabel="Kirim tautan reset"
        hint="Email yang sama dengan akun SIAP Jateng Anda."
      />
    </AuthLayout>
  );
}
