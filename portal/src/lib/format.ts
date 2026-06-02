/**
 * Small shared formatting helpers — Indonesian date display + license
 * countdown math. Pure functions, no React, safe in Server Components.
 */

const BULAN_ID = [
  "",
  "Januari",
  "Februari",
  "Maret",
  "April",
  "Mei",
  "Juni",
  "Juli",
  "Agustus",
  "September",
  "Oktober",
  "November",
  "Desember",
];

/**
 * Format a SIAP/ISO timestamp as "17 Mei 2026". Accepts ISO 8601 or the
 * SIAP "2026-05-17 18:40:06" shape. Returns "—" for null/empty.
 */
export function formatDateID(raw: string | null | undefined): string {
  if (!raw) return "—";
  const isoish = raw.includes("T") ? raw : raw.replace(" ", "T");
  const d = new Date(isoish);
  if (Number.isNaN(d.getTime())) return raw;
  return `${d.getDate()} ${BULAN_ID[d.getMonth() + 1]} ${d.getFullYear()}`;
}

/**
 * Whole days from now until `iso` (negative = already passed). Calendar-day
 * based (ignores time-of-day) so "expires today" reads as 0, not -1.
 */
export function daysUntil(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const target = new Date(iso.includes("T") ? iso : iso.replace(" ", "T"));
  if (Number.isNaN(target.getTime())) return null;
  const startOfDay = (d: Date) =>
    new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const ms = startOfDay(target) - startOfDay(new Date());
  return Math.round(ms / 86_400_000);
}
