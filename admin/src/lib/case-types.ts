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
