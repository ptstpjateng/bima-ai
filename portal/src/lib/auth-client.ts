/**
 * Client-side SSO helpers.
 *
 * These call our own Next.js Route Handlers (`/api/sso/*`), not admin-api
 * directly. The Route Handlers (see `app/api/sso/[...slug]/route.ts`) are
 * the only thing that knows the admin-api base URL — that boundary is what
 * lets us set an httpOnly cookie and keep the browser ignorant of the JWT.
 *
 * Why no fetch wrapper? The three operations here are different enough that
 * sharing them through a single `request()` helper would obscure intent. The
 * tradeoff: ~10 lines of duplication for explicit error handling per call.
 */

/** Identity types citizens can use for SSO. KTP for individuals, NPWP for businesses. */
export type IdentityType = "KTP" | "NPWP";

/** Citizen identity returned by admin-api /sso/me + carried in JWT claims. */
export type SsoUser = {
  profile_id: number;
  /** Raw digits — 16 for NIK, 15 for NPWP. Format/mask only at render time. */
  identity_number: string;
  identity_type: IdentityType;
  name: string;
  mobile: string | null;
  kabupaten: string | null;
  kind: "pemohon";
};

/** Successful login response shape (admin-api SsoLoginResponse). */
export type SsoLoginOk = {
  ok: true;
  user: SsoUser;
};

/** Discriminated failure shape — keeps form code switch-exhaustive. */
export type SsoLoginErr = {
  ok: false;
  /** Translated, user-safe message. Indonesian copy. */
  message: string;
  /** Original HTTP status — useful for analytics, not for display. */
  status: number;
};

export type SsoLoginResult = SsoLoginOk | SsoLoginErr;

/**
 * Send identity number (NIK or NPWP) to the Route Handler, which forwards
 * to admin-api and sets the httpOnly auth cookie on success.
 *
 * Pre-strips spaces/dashes/dots so the wire format is bare digits matching
 * admin-api's regex (16=NIK, 15=NPWP). Server-side validation is still
 * authoritative — this is just to avoid a guaranteed 422 round-trip.
 */
export async function loginByIdentity(identityNumber: string): Promise<SsoLoginResult> {
  const cleaned = identityNumber.replace(/[\s\-.]/g, "");

  let resp: Response;
  try {
    resp = await fetch("/api/sso/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ identity_number: cleaned }),
    });
  } catch {
    // Network failure — DNS, offline, timeout. Same copy regardless.
    return {
      ok: false,
      status: 0,
      message: "Tidak bisa menghubungi server. Periksa koneksi Anda.",
    };
  }

  if (resp.ok) {
    const body = (await resp.json()) as { user: SsoUser };
    return { ok: true, user: body.user };
  }

  // Map admin-api error shapes to Indonesian copy. The Route Handler passes
  // status through unchanged, so the mapping lives in one place — here.
  let message: string;
  if (resp.status === 404) {
    message = "NIK atau NPWP tidak terdaftar di SIAP Jateng.";
  } else if (resp.status === 422) {
    message = "Harus 16 digit (NIK) atau 15 digit (NPWP).";
  } else if (resp.status === 503) {
    message = "Layanan login sementara tidak tersedia. Coba lagi nanti.";
  } else {
    message = "Login gagal. Coba lagi beberapa saat lagi.";
  }

  return { ok: false, status: resp.status, message };
}

/**
 * Best-effort logout. Always clears the cookie even if the network call
 * fails — a stuck-cookie user experience is worse than a stale server log.
 */
export async function logout(): Promise<void> {
  try {
    await fetch("/api/sso/logout", { method: "POST" });
  } catch {
    // Swallow — the Route Handler always tries to clear the cookie via the
    // Set-Cookie header even if admin-api is down.
  }
}

/**
 * Fetch the current user from the cookie. Returns null when no cookie /
 * cookie expired / admin-api unreachable — the caller treats all three the
 * same way (route to /login).
 *
 * Suitable for Client Components. Server Components should read the cookie
 * directly + call the Route Handler with `cookies().get(...)` semantics; see
 * `app/me/page.tsx` for the SSR pattern.
 */
export async function getCurrentUser(): Promise<SsoUser | null> {
  let resp: Response;
  try {
    resp = await fetch("/api/sso/me", {
      method: "GET",
      // Default credentials: 'same-origin' is fine — Route Handlers are
      // same-origin and the httpOnly cookie is bound to this host.
    });
  } catch {
    return null;
  }

  if (!resp.ok) return null;

  const body = (await resp.json()) as { user: SsoUser };
  return body.user;
}
