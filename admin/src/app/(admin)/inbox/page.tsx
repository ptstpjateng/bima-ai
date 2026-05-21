import type { Metadata } from "next";
import { InboxView } from "./inbox-view";

/**
 * Officer inbox — Wave 4 (multi-ticket officer inbox, Vision req #14 + #15).
 *
 * Where `/case/[ticket]` shows ONE case, this page is the officer's whole
 * working queue: every active SIAP case at once, urgency-ranked, each row
 * carrying a calm SOP-as-motivator progress surface (req #15). Rows click
 * through to the existing `/case/[ticket]` detail view.
 *
 * Server Component shell — the middleware in `src/middleware.ts` has already
 * verified the admin_token cookie, so no client-side guard. The interactive
 * list (filter + react-query fetch) lives in the `InboxView` client island.
 */
export const metadata: Metadata = {
  title: "Kotak Masuk · BIMA Admin",
};

export default function InboxPage() {
  return <InboxView />;
}
