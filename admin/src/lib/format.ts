/**
 * Formatting helpers shared across admin pages.
 *
 * Indonesian locale by default (we display "id-ID" date/time/relative-time
 * because the audience is DPMPTSP staff in Jawa Tengah).
 */

const RELATIVE = new Intl.RelativeTimeFormat("id-ID", { numeric: "auto" });

const RELATIVE_THRESHOLDS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ["year", 60 * 60 * 24 * 365],
  ["month", 60 * 60 * 24 * 30],
  ["week", 60 * 60 * 24 * 7],
  ["day", 60 * 60 * 24],
  ["hour", 60 * 60],
  ["minute", 60],
  ["second", 1],
];

/**
 * "3 menit lalu", "kemarin", "5 detik lalu" — accepts ISO strings or Date.
 */
export function relativeTime(value: string | number | Date | null | undefined): string {
  if (!value) return "—";
  const date = value instanceof Date ? value : new Date(value);
  const ms = date.getTime();
  if (Number.isNaN(ms)) return "—";

  const diffSec = Math.round((ms - Date.now()) / 1000);
  const absSec = Math.abs(diffSec);

  for (const [unit, secsPer] of RELATIVE_THRESHOLDS) {
    if (absSec >= secsPer || unit === "second") {
      const valueInUnit = Math.round(diffSec / secsPer);
      return RELATIVE.format(valueInUnit, unit);
    }
  }
  return RELATIVE.format(diffSec, "second");
}

const DATE_TIME = new Intl.DateTimeFormat("id-ID", {
  dateStyle: "medium",
  timeStyle: "short",
});

export function formatDateTime(value: string | number | Date | null | undefined): string {
  if (!value) return "—";
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return DATE_TIME.format(date);
}

const TIME = new Intl.DateTimeFormat("id-ID", { timeStyle: "short" });

export function formatTime(value: string | number | Date | null | undefined): string {
  if (!value) return "—";
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return TIME.format(date);
}

export function formatNumber(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString("id-ID");
}

export function truncate(s: string | null | undefined, max = 80): string {
  if (!s) return "";
  return s.length <= max ? s : `${s.slice(0, max - 1).trimEnd()}…`;
}
