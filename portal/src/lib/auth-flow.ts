/**
 * Typed client interface for the magic-link / passwordless auth flows.
 *
 * ─────────────────────────────────────────────────────────────────────────
 * HONESTY CONTRACT — READ THIS BEFORE WIRING ANYTHING:
 *
 * SIAP (the Jawa Tengah identity provider) exposes ONLY a single citizen
 * auth endpoint to us today:
 *
 *     POST /v1/login   (Sanctum, email+password, throttle:5,1)
 *
 * There is NO passwordless-login endpoint, NO magic-link issuer, NO public
 * self-register, and NO public password-reset endpoint we can call from the
 * portal. Citizen auth in SIAP is Laravel session-based on the `pemohon`
 * guard (UserAuth → tblpengguna, bcrypt). Confirmed via read-only discovery
 * of routes/api.php in this session (2026-06-02).
 *
 * The portal's REAL, shippable citizen path is identity LOOKUP, not magic
 * link: NIK/NPWP → /api/sso/login Route Handler → admin-api POST /sso/login.
 * That lives in lib/auth-client.ts and STAYS REAL. See /login.
 *
 * Therefore every function in THIS module is a SCAFFOLD. It returns a
 * discriminated `{ ok: false, reason: "not_implemented" }` so the UI can
 * render an honest "belum tersedia" panel. It NEVER fabricates a token,
 * NEVER writes a fake session, NEVER reports success.
 *
 * TODO(2026-06-02): SIAP has no passwordless endpoint. Wire these to
 *   admin-api `/sso/magic-link` once SIAP adds `POST /api/v1/passwordless-login`
 *   or adapts `/pelengkapan/verify_resetpass`. When that lands, flip the
 *   FLOW_BACKEND_READY flag below and replace the stub bodies with the real
 *   fetch calls — the UI and these types do NOT need to change.
 * ─────────────────────────────────────────────────────────────────────────
 */

/** Where the user is in a multi-step passwordless flow. Drives StepIndicator. */
export type AuthStep = "identify" | "verify" | "done";

/**
 * Set to `true` only when SIAP/admin-api can actually back these flows.
 * While `false`, every function short-circuits to `not_implemented`.
 */
export const FLOW_BACKEND_READY = false as const;

/** Reasons a flow call can resolve unsuccessfully. */
export type AuthFlowReason =
  | "not_implemented" // SIAP has no endpoint yet — the only reason that fires today
  | "invalid_input" // client-side guard (bad email / wrong code length)
  | "rate_limited" // future: SIAP throttle:5,1
  | "expired" // future: code/link expired
  | "network"; // future: transport failure

export type AuthFlowOk<T = undefined> = {
  ok: true;
  data: T;
};

export type AuthFlowErr = {
  ok: false;
  reason: AuthFlowReason;
  /** User-safe Indonesian copy. Safe to render directly. */
  message: string;
};

export type AuthFlowResult<T = undefined> = AuthFlowOk<T> | AuthFlowErr;

/** Stable "not yet wired" result, reused by every stub below. */
function notImplemented(): AuthFlowErr {
  return {
    ok: false,
    reason: "not_implemented",
    message:
      "Fitur ini belum tersedia. SIAP Jateng belum menyediakan login tautan-ajaib. " +
      "Untuk saat ini, gunakan akun SIAP Anda lewat login NIK / NPWP.",
  };
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/**
 * Step 1 of the magic-link flow: ask SIAP to email a sign-in link / code.
 *
 * REAL behavior (future): POST email to admin-api /sso/magic-link, which
 * relays to SIAP's passwordless endpoint and 202s. Returns { step: "verify" }.
 *
 * STUB behavior (today): validates the email locally for AT/keyboard testing,
 * then returns not_implemented. Never claims a link was sent.
 */
export async function startMagicLinkFlow(
  email: string,
): Promise<AuthFlowResult<{ step: AuthStep }>> {
  if (!EMAIL_RE.test(email.trim())) {
    return {
      ok: false,
      reason: "invalid_input",
      message: "Masukkan alamat email yang valid.",
    };
  }

  if (!FLOW_BACKEND_READY) {
    // TODO(2026-06-02): replace with
    //   await fetch("/api/sso/magic-link", { method: "POST", body: { email } })
    return notImplemented();
  }

  // Unreachable until FLOW_BACKEND_READY flips. Shape is final.
  return { ok: true, data: { step: "verify" } };
}

/**
 * Step 2 of the magic-link flow: verify the 6-digit code from the email.
 *
 * REAL behavior (future): POST { email, code } to admin-api, which exchanges
 * it for a JWT and sets the httpOnly bima_pemohon_token cookie (same cookie
 * the NIK/NPWP path uses). Returns { step: "done" }; UI then routes to
 * /dashboard.
 *
 * STUB behavior (today): validates 6-digit shape, returns not_implemented.
 * Never sets a cookie, never reports a logged-in state.
 */
export async function verifyMagicLinkCode(
  email: string,
  code: string,
): Promise<AuthFlowResult<{ step: AuthStep }>> {
  if (!/^\d{6}$/.test(code)) {
    return {
      ok: false,
      reason: "invalid_input",
      message: "Kode verifikasi harus 6 digit angka.",
    };
  }

  if (!FLOW_BACKEND_READY) {
    // TODO(2026-06-02): replace with
    //   await fetch("/api/sso/magic-link/verify", { method: "POST", body: { email, code } })
    return notImplemented();
  }

  return { ok: true, data: { step: "done" } };
}

/**
 * Password-reset request.
 *
 * REAL behavior (future): relay to SIAP /pelengkapan/verify_resetpass once an
 * API-callable variant exists. Returns ok with a neutral "if the account
 * exists, we sent an email" message (no account enumeration).
 *
 * STUB behavior (today): validate email, return not_implemented pointing the
 * user at SIAP's own reset page.
 */
export async function requestPasswordReset(
  email: string,
): Promise<AuthFlowResult> {
  if (!EMAIL_RE.test(email.trim())) {
    return {
      ok: false,
      reason: "invalid_input",
      message: "Masukkan alamat email yang valid.",
    };
  }

  if (!FLOW_BACKEND_READY) {
    // TODO(2026-06-02): SIAP exposes no public reset API. Wire to admin-api
    //   /sso/reset once SIAP adapts /pelengkapan/verify_resetpass.
    return notImplemented();
  }

  return { ok: true, data: undefined };
}
