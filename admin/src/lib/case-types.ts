/**
 * Officer-facing case detail contract.
 *
 * Mirrors the response shape of:
 *   POST /case/{ticket}/validate?demo_fixture=clean|name_mismatch|nik_typo
 *
 * Built in parallel by the `feat/case-validator-proxy` track. If the upstream
 * contract drifts, update both this file and the page consumer together so the
 * destructure stays narrow (no `as any` casts anywhere on the page).
 */

export type DemoFixture = "clean" | "name_mismatch" | "nik_typo";

export const DEMO_FIXTURES: DemoFixture[] = [
  "clean",
  "name_mismatch",
  "nik_typo",
];

export function isDemoFixture(value: string | undefined | null): value is DemoFixture {
  return value === "clean" || value === "name_mismatch" || value === "nik_typo";
}

/**
 * Validator severity ladder — sorted critical → high → medium → low so the
 * officer reads worst issues first.
 */
export type IssueSeverity = "critical" | "high" | "medium" | "low";

export const SEVERITY_ORDER: Record<IssueSeverity, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
};

export interface ValidationIssue {
  severity: IssueSeverity;
  field: string;
  /** Indonesian, officer-readable. */
  message: string;
  /** Filenames of uploaded docs the issue references (chip list in UI). */
  related_docs: string[];
}

export type ValidationStatus =
  | "ready"
  | "minor_issues"
  | "major_issues"
  | "unverified";

/**
 * Indonesian labels per BIMA persona — these strings appear directly on the
 * gauge label. Map exhaustively so the type checker catches a missing case.
 */
export const VALIDATION_STATUS_LABEL: Record<ValidationStatus, string> = {
  ready: "Siap dikirim",
  minor_issues: "Perlu koreksi minor",
  major_issues: "Perlu koreksi besar",
  unverified: "Belum terverifikasi",
};

export interface PerDocumentScore {
  filename: string;
  fields_extracted: number;
  fields_expected: number;
  notes?: string | null;
}

export interface CaseValidationPayload {
  /** 0–100. Authoritative; we render `score_percent` rounded. */
  score: number;
  score_percent: number;
  status: ValidationStatus;
  /** One-paragraph Indonesian summary below the gauge. */
  summary: string;
  per_document: PerDocumentScore[];
  issues: ValidationIssue[];
}

export interface CaseHeaderPayload {
  ticket: string;
  license_name: string;
  sector_name: string | null;
  applicant_name: string;
  current_desk: string | null;
  /** Free-form Indonesian status from SIAP — e.g. "Menunggu verifikasi". */
  status: string;
  submitted_at: string;
}

export interface CaseValidateResponse {
  case: CaseHeaderPayload;
  validation: CaseValidationPayload;
}

/* -------------------------------------------------------------------------- */
/* Officer Copilot                                                             */
/* -------------------------------------------------------------------------- */

/**
 * Officer Copilot contract — mirrors admin-api's `CopilotChatResponse`.
 *
 *   POST /case/{ticket}/copilot/chat   → run one turn
 *   GET  /case/{ticket}/copilot/session → rehydrate the persisted session
 *
 * The session is per-officer-per-case-per-mode and lives in admin-api's
 * `copilot_session` table; ai-engine's agent stays stateless. See
 * [[Decisions]] §22.
 */

/**
 * Copilot variant (Wave 4 / Vision req #13):
 *  - "officer"   — the validation-first desk copilot.
 *  - "signature" — the Head-of-DPMPTSP signing assistant: synthesises the
 *    whole approval chain for the final signing decision, then hands off to
 *    SIAP Jateng (which owns the actual TTE/BSRE signature).
 * Each mode keeps its own persisted session transcript per ticket.
 */
export type CopilotMode = "officer" | "signature";

export type CopilotRole = "user" | "model";

export interface CopilotTurn {
  role: CopilotRole;
  text: string;
}

export interface CopilotToolCall {
  name: string;
  args: Record<string, unknown>;
  result_preview: string;
}

export interface CopilotChatResponse {
  reply: string;
  tool_calls: CopilotToolCall[];
  history: CopilotTurn[];
}

/* -------------------------------------------------------------------------- */
/* SIAP signing handoff — Wave 4, Vision req #13                               */
/* -------------------------------------------------------------------------- */

/**
 * Base URL of SIAP Jateng's Filament admin panel. BIMA never signs — the
 * digital signature (TTE/BSRE) is performed in SIAP. We deep-link the Head
 * of DPMPTSP straight to SIAP's "Tanda Tangan Berkas" page.
 *
 * Defaults to Beta-SIAP so a missing env var still produces a working
 * rehearsal link. Production (`perizinan.jatengprov.go.id`) is set via
 * `NEXT_PUBLIC_SIAP_BASE_URL` in Vercel only when explicitly cut over.
 */
const SIAP_BASE_URL = (
  process.env.NEXT_PUBLIC_SIAP_BASE_URL ?? "https://beta-siap.nolongin.com"
).replace(/\/+$/, "");

/**
 * Build the deep-link that opens SIAP's signing page for one case.
 *
 * SIAP route (verified against the SIAP repo, 2026-05-21):
 *   - Filament panel path:  `/admin`
 *   - Resource slug:        `tanda-tangan-berkas` (TandaTanganBerkasResource)
 *   - The page table column `properties.ticket` is searchable, so we
 *     pre-filter it to this one case via Filament's `?tableSearch=` param.
 *
 * The actual TTE is a per-row action on that page; the Head clicks it and
 * signs in SIAP with their BSrE passphrase. BIMA only opens the door.
 */
export function buildSiapSigningUrl(ticket: string): string {
  const padded = /^\d+$/.test(ticket) ? ticket.padStart(9, "0") : ticket;
  return `${SIAP_BASE_URL}/admin/tanda-tangan-berkas?tableSearch=${encodeURIComponent(
    padded
  )}`;
}
