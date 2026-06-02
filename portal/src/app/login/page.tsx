import type { Metadata } from "next";

import { LoginForm } from "@/components/auth/LoginForm";

/**
 * Canonical citizen login. The REAL, shippable identity-lookup path
 * (NIK 16 / NPWP 15 → admin-api /sso/login → httpOnly cookie).
 *
 * Middleware and the Route Handler both point here; this stays at /login.
 * The form itself is the shared <LoginForm>, also mounted at /auth/login so
 * the new auth shell is cohesive without duplicating any logic.
 */
export const metadata: Metadata = {
  title: "Masuk · BIMA",
  description: "Login ke BIMA dengan akun SIAP Jateng (NIK atau NPWP).",
  robots: { index: false },
};

export default function LoginPage() {
  return <LoginForm />;
}
