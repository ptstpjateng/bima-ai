import { redirect } from "next/navigation";

/**
 * Root entry — always defers to the middleware-aware redirect targets.
 * Authenticated users get bounced to /dashboard by the middleware (it sees
 * the admin_token cookie + a /login redirect rule); unauthenticated users
 * to /login.
 */
export default function RootPage() {
  redirect("/dashboard");
}
