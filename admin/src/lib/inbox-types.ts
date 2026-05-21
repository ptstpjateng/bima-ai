/**
 * Officer inbox contract — Wave 4 (multi-ticket officer inbox).
 *
 * Mirrors admin-api's `InboxResponse` / `InboxCase` schemas
 * (`admin-api/app/schemas/inbox.py`). Backed by:
 *
 *   GET /inbox?desk=<substr>&limit=<n>
 *
 * If the upstream contract drifts, update this file and the `/inbox` page
 * consumer together so the destructure stays narrow (no `as` casts).
 */

/**
 * Coarse SOP (jangka waktu / SLA) bucket. Drives the calm progress
 * indicator's tone on the inbox — SOP is a *motivator, not a whip*, so even
 * `over` stays a steady amber, never an alarming red-shame.
 */
export type SopState = "on_track" | "approaching" | "over" | "unknown";

export interface InboxCase {
  /** Zero-padded 9-digit SIAP ticket. */
  ticket: string;
  license_name: string | null;
  sector_name: string | null;
  applicant_name: string | null;
  /** `bloksistem_nama` — the desk currently holding the file. */
  current_desk: string | null;
  /** Raw SIAP `status_permohonan` (free-form Indonesian). */
  status: string | null;
  /** ISO-8601 submission timestamp. */
  submitted_at: string | null;

  /** Whole calendar days since submission. */
  days_open: number;
  /** Statutory SLA days (jangka waktu); null when unresolved. */
  sop_days: number | null;

  /** Inbox-ranker score — the list arrives already sorted by this, desc. */
  urgency: number;
  sop_state: SopState;
}

export interface InboxResponse {
  /** Active cases, urgency-sorted descending (server-side). */
  cases: InboxCase[];
  total: number;
  /** The desk filter applied, if any. */
  desk: string | null;
}
