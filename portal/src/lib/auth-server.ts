/**
 * Server-side helpers for reading the SSO session.
 *
 * Used by Server Components and middleware-adjacent code paths that need to
 * read the bima_pemohon_token cookie without going through the Route Handler
 * round-trip.
 *
 * Two paths:
 *   - `getServerUser()` — calls admin-api directly using the cookie as bearer.
 *     Reaches across the network, so don't call this on hot pages without
 *     thought. Returns null on any failure (cookie missing, expired, network).
 *   - `getServerUserFromClaims()` — decode-only, NO admin-api call. Returns
 *     the JWT claims as an SsoUser. Cheaper but skips token expiry checks
 *     (jose handles expiry only when verifying signature, which we can't
 *     here because the portal doesn't hold SECRET_KEY).
 *
 * For most authenticated pages, prefer `getServerUser()`. Use the claims-only
 * variant for TopBar / nav-style chrome where a freshness mismatch is fine.
 */

import { cookies } from "next/headers";
import type { SsoUser } from "@/lib/auth-client";

const COOKIE_NAME = "bima_pemohon_token";

function adminApiBase(): string | null {
  const raw = process.env.BIMA_ADMIN_API_URL;
  if (!raw) return null;
  return raw.replace(/\/$/, "");
}

/**
 * Fetch the current user via admin-api /sso/me. Returns null on any error.
 *
 * Use this for pages that require live identity (e.g. /me itself). The
 * fetch is `no-store` so Next.js doesn't accidentally cache a stale user.
 */
export async function getServerUser(): Promise<SsoUser | null> {
  const base = adminApiBase();
  if (!base) return null;

  const jar = await cookies();
  const token = jar.get(COOKIE_NAME)?.value;
  if (!token) return null;

  try {
    const resp = await fetch(`${base}/sso/me`, {
      method: "GET",
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    if (!resp.ok) return null;
    const body = (await resp.json()) as { user: SsoUser };
    return body.user;
  } catch {
    return null;
  }
}

/**
 * Decode the JWT payload locally and return claims as an SsoUser.
 *
 * NOTE: This does NOT verify the signature — that's why it's marked "from
 * claims". It's safe in narrow uses (e.g. TopBar display) because:
 *   1. The cookie is httpOnly + same-site, so an attacker can't substitute one
 *   2. We never grant authority based on this — every protected endpoint
 *      goes through admin-api, which DOES verify the signature.
 *
 * Returns null if cookie missing OR payload malformed.
 */
export async function getServerUserFromClaims(): Promise<SsoUser | null> {
  const jar = await cookies();
  const token = jar.get(COOKIE_NAME)?.value;
  if (!token) return null;

  const parts = token.split(".");
  if (parts.length !== 3) return null;

  try {
    // Base64URL decode the middle segment. Pad to multiple of 4 because
    // JWT strips trailing '='. atob is available in the Node runtime.
    const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
    const json = Buffer.from(padded, "base64").toString("utf-8");
    const claims = JSON.parse(json) as {
      sub?: string;
      nik?: string;
      name?: string;
      kind?: string;
      exp?: number;
    };

    if (claims.kind !== "pemohon") return null;
    if (!claims.sub || !claims.nik || !claims.name) return null;
    // Soft expiry check — admin-api's signature verification is authoritative.
    if (claims.exp && claims.exp * 1000 < Date.now()) return null;

    return {
      profile_id: parseInt(claims.sub, 10),
      nik: claims.nik,
      name: claims.name,
      mobile: null,
      kabupaten: null,
      kind: "pemohon",
    };
  } catch {
    return null;
  }
}
