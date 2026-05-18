import { Suspense } from "react";
import type { Metadata } from "next";
import { CaseView, CaseSkeleton } from "./case-view";
import { isDemoFixture, type DemoFixture } from "@/lib/case-types";

/**
 * Officer-facing case detail — Phase 2 (Sprint D) "killer feature" surface.
 *
 * Server Component shell. Resolves the dynamic `ticket` param plus an optional
 * `?demo=clean|name_mismatch|nik_typo` searchParam (used by the demo
 * rehearsal arc — see [[BIMA Vision]] §"Sprint D demo arc"), then hands them
 * to a client island that fetches via the existing same-origin `/api/proxy`
 * pipeline. That keeps the httpOnly admin_token flow unchanged.
 *
 * The middleware in `src/middleware.ts` (alias: `src/proxy.ts`) has already
 * verified the cookie before this component renders — no extra guard needed.
 *
 * Suspense + skeleton satisfies the "Server Component initial load" requirement
 * from the brief; the client island swaps to live data once react-query
 * resolves on the client.
 */
export const metadata: Metadata = {
  title: "Case · BIMA Admin",
};

type Props = {
  params: Promise<{ ticket: string }>;
  searchParams: Promise<{ demo?: string | string[] }>;
};

export default async function CaseDetailPage({ params, searchParams }: Props) {
  const [{ ticket }, sp] = await Promise.all([params, searchParams]);

  // searchParams values can be string | string[] | undefined. Pick the first
  // and validate against the typed union — anything unrecognised drops to null
  // so the page falls back to a real-validator call.
  const demoRaw = Array.isArray(sp.demo) ? sp.demo[0] : sp.demo;
  const fixture: DemoFixture | null = isDemoFixture(demoRaw) ? demoRaw : null;

  const decodedTicket = decodeURIComponent(ticket);

  return (
    <Suspense fallback={<CaseSkeleton />}>
      <CaseView ticket={decodedTicket} fixture={fixture} />
    </Suspense>
  );
}
