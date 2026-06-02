import type { Metadata } from "next";

import { LoginForm } from "@/components/auth/LoginForm";

/**
 * /auth/login — the auth-shell mount of the canonical login. Re-exports the
 * exact same <LoginForm> used at /login (no duplicated logic), so the new
 * /auth/* family is cohesive. Both routes are REAL identity-lookup login.
 */
export const metadata: Metadata = {
  title: "Masuk · BIMA",
  description: "Login ke BIMA dengan akun SIAP Jateng (NIK atau NPWP).",
  robots: { index: false },
};

export default function AuthLoginPage() {
  return <LoginForm />;
}
