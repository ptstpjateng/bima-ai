import { TopBarClient } from "@/components/landing/TopBarClient";
import { getServerUserFromClaims } from "@/lib/auth-server";

/**
 * Server wrapper for the sticky top bar.
 *
 * Reads the SSO cookie (claims-only, no admin-api round-trip — see
 * `lib/auth-server.ts` for why that's safe for non-authoritative chrome),
 * then hands the result to the client island that owns motion + the
 * dropdown menu.
 *
 * Server-side render keeps the bar's auth state correct on first paint —
 * a client-only check would briefly show "Login" to logged-in users.
 */
export async function TopBar() {
  const user = await getServerUserFromClaims();
  return <TopBarClient initialUser={user} />;
}
