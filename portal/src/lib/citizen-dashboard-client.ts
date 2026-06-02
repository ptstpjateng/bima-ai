/**
 * Typed client for the citizen post-licensing dashboard.
 *
 * ─────────────────────────────────────────────────────────────────────────
 * HONESTY CONTRACT:
 *
 * SIAP does NOT yet expose a read endpoint for a citizen's post-licensing
 * data (issued SK, renewals, license lifecycle). Until it does, every
 * function here returns clearly-labelled MOCK data behind the DASHBOARD_MOCK
 * flag. The dashboard UI renders a persistent, dismissible "Data contoh"
 * (sample data) banner on every surface so mock data is NEVER mistaken for
 * real SIAP data.
 *
 * TODO(2026-06-02): wire to admin-api `/sso/licenses` (and `/sso/renewals`,
 *   `/sso/applications`) once SIAP's post-licensing read endpoint exists.
 *   When that lands, flip DASHBOARD_MOCK to false and replace the mock
 *   bodies with authenticated fetches — the types and the UI do NOT change.
 *
 * The async signatures are intentional: the real implementation will fetch
 * over the network, and the dashboard already renders Suspense / skeleton
 * loading states against these promises.
 * ─────────────────────────────────────────────────────────────────────────
 */

/** Flip to `false` when admin-api/SIAP can serve real post-licensing data. */
export const DASHBOARD_MOCK = true as const;

/** Lifecycle status of an issued/processing license. */
export type LicenseStatus =
  | "aktif" // active, valid
  | "akan_kedaluwarsa" // active but expiring soon
  | "kedaluwarsa" // expired
  | "proses" // being processed (renewal/new)
  | "ditolak"; // rejected

/** Status of an application moving through the OSS/SIAP desks. */
export type ApplicationStatus = "proses" | "selesai" | "ditolak";

export type CitizenLicense = {
  id: string;
  /** Human-readable nomenklatur, e.g. "Sertifikat Standar — Industri Roti". */
  license_name: string;
  /** KBLI sector label. */
  sector: string;
  /** 5-digit KBLI code (zero-padded). */
  kbli_code: string;
  status: LicenseStatus;
  /** Risk tier label from OSS RBA. */
  risk_level: "Rendah" | "Menengah Rendah" | "Menengah Tinggi" | "Tinggi";
  /** ISO date the SK was issued, or null if not yet issued. */
  issued_at: string | null;
  /** ISO date the license expires, or null if non-expiring. */
  expires_at: string | null;
  /** Issuing authority. */
  authority: string;
  /** Direct download URL for the SK PDF, or null when not available yet. */
  sk_document_url: string | null;
  /** Lifecycle events, newest last. */
  timeline: LicenseTimelineEvent[];
};

export type LicenseTimelineEvent = {
  /** ISO date. */
  date: string;
  label: string;
  /** Phase of the 3-phase lifecycle this event belongs to. */
  phase: "pengajuan" | "terbit" | "perpanjangan";
  done: boolean;
};

export type CitizenRenewal = {
  id: string;
  license_id: string;
  license_name: string;
  /** ISO date the underlying license expires. */
  expires_at: string;
  /** Whether the renewal window is open yet. */
  eligible: boolean;
  /** Days before expiry the renewal window opens. */
  window_days: number;
  /** WhatsApp deeplink that pre-fills a renewal request to BIMA. */
  action_url: string;
};

export type CitizenApplication = {
  id: string;
  /** 9-digit SIAP tracking ticket (links to /track/[ticket]). */
  ticket: string;
  license_name: string;
  sector: string;
  status: ApplicationStatus;
  /** Current desk / position in the workflow. */
  current_desk: string;
  /** ISO date submitted. */
  submitted_at: string;
};

// ─── Mock fixtures ─────────────────────────────────────────────────────────
// Dates are computed relative to "now" so the expiry thresholds (RenewalCountdown)
// demonstrate all three states (healthy / expiring / expired) regardless of
// when the demo is run.

function daysFromNow(n: number): string {
  const d = new Date();
  d.setHours(12, 0, 0, 0);
  d.setDate(d.getDate() + n);
  return d.toISOString();
}

const MOCK_LICENSES: CitizenLicense[] = [
  {
    id: "lic-10231",
    license_name: "Sertifikat Standar — Industri Roti dan Kue",
    sector: "Industri Pengolahan",
    kbli_code: "10710",
    status: "aktif",
    risk_level: "Menengah Rendah",
    issued_at: daysFromNow(-300),
    expires_at: daysFromNow(180),
    authority: "DPMPTSP Provinsi Jawa Tengah",
    sk_document_url: "/dashboard/sample-sk.pdf",
    timeline: [
      { date: daysFromNow(-330), label: "Permohonan diajukan", phase: "pengajuan", done: true },
      { date: daysFromNow(-318), label: "Verifikasi persyaratan", phase: "pengajuan", done: true },
      { date: daysFromNow(-300), label: "Sertifikat Standar terbit", phase: "terbit", done: true },
    ],
  },
  {
    id: "lic-10485",
    license_name: "NIB — Perdagangan Eceran Pakaian",
    sector: "Perdagangan Besar dan Eceran",
    kbli_code: "47711",
    status: "akan_kedaluwarsa",
    risk_level: "Rendah",
    issued_at: daysFromNow(-700),
    expires_at: daysFromNow(28),
    authority: "Lembaga OSS — BKPM",
    sk_document_url: "/dashboard/sample-sk.pdf",
    timeline: [
      { date: daysFromNow(-705), label: "Permohonan diajukan", phase: "pengajuan", done: true },
      { date: daysFromNow(-700), label: "NIB terbit otomatis", phase: "terbit", done: true },
      { date: daysFromNow(28), label: "Jatuh tempo perpanjangan", phase: "perpanjangan", done: false },
    ],
  },
  {
    id: "lic-10902",
    license_name: "Izin Edar PIRT — Keripik Singkong",
    sector: "Industri Pengolahan",
    kbli_code: "10792",
    status: "proses",
    risk_level: "Menengah Tinggi",
    issued_at: null,
    expires_at: null,
    authority: "Dinas Kesehatan Kabupaten",
    sk_document_url: null,
    timeline: [
      { date: daysFromNow(-12), label: "Permohonan diajukan", phase: "pengajuan", done: true },
      { date: daysFromNow(-5), label: "Pemeriksaan sarana produksi", phase: "pengajuan", done: true },
      { date: daysFromNow(0), label: "Menunggu penerbitan SPP-IRT", phase: "terbit", done: false },
    ],
  },
];

const MOCK_RENEWALS: CitizenRenewal[] = [
  {
    id: "ren-2201",
    license_id: "lic-10485",
    license_name: "NIB — Perdagangan Eceran Pakaian",
    expires_at: daysFromNow(28),
    eligible: true,
    window_days: 60,
    action_url:
      "https://wa.me/6285117557091?text=Halo%20BIMA%2C%20saya%20mau%20perpanjang%20izin%20NIB%2047711",
  },
  {
    id: "ren-2202",
    license_id: "lic-10231",
    license_name: "Sertifikat Standar — Industri Roti dan Kue",
    expires_at: daysFromNow(180),
    eligible: false,
    window_days: 60,
    action_url:
      "https://wa.me/6285117557091?text=Halo%20BIMA%2C%20saya%20mau%20perpanjang%20Sertifikat%20Standar%2010710",
  },
];

const MOCK_APPLICATIONS: CitizenApplication[] = [
  {
    id: "app-90021",
    ticket: "000451238",
    license_name: "Izin Edar PIRT — Keripik Singkong",
    sector: "Industri Pengolahan",
    status: "proses",
    current_desk: "Dinas Kesehatan Kabupaten",
    submitted_at: daysFromNow(-12),
  },
  {
    id: "app-90014",
    ticket: "000449107",
    license_name: "Sertifikat Standar — Industri Roti dan Kue",
    sector: "Industri Pengolahan",
    status: "selesai",
    current_desk: "Selesai — SK diterbitkan",
    submitted_at: daysFromNow(-330),
  },
  {
    id: "app-89980",
    ticket: "000447502",
    license_name: "Izin Reklame Papan Nama",
    sector: "Jasa",
    status: "ditolak",
    current_desk: "Ditolak — dokumen tidak lengkap",
    submitted_at: daysFromNow(-58),
  },
];

// Small artificial latency so skeleton/loading states are exercised in dev.
function settle<T>(value: T): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), 280));
}

/**
 * List the citizen's licenses.
 * TODO(2026-06-02): wire to admin-api GET /sso/licenses (bearer = cookie JWT).
 */
export async function getCitizenLicenses(): Promise<CitizenLicense[]> {
  if (DASHBOARD_MOCK) return settle(MOCK_LICENSES);
  return settle([]); // unreachable until DASHBOARD_MOCK flips
}

/**
 * Fetch a single license by id (for the detail page).
 * TODO(2026-06-02): wire to admin-api GET /sso/licenses/{id}.
 */
export async function getCitizenLicense(
  id: string,
): Promise<CitizenLicense | null> {
  if (DASHBOARD_MOCK) {
    return settle(MOCK_LICENSES.find((l) => l.id === id) ?? null);
  }
  return settle(null);
}

/**
 * List renewals due / upcoming.
 * TODO(2026-06-02): wire to admin-api GET /sso/renewals.
 */
export async function getCitizenRenewals(): Promise<CitizenRenewal[]> {
  if (DASHBOARD_MOCK) return settle(MOCK_RENEWALS);
  return settle([]);
}

/**
 * List in-flight / historical applications (links to live /track tickets).
 * TODO(2026-06-02): wire to admin-api GET /sso/applications.
 */
export async function getCitizenApplications(): Promise<CitizenApplication[]> {
  if (DASHBOARD_MOCK) return settle(MOCK_APPLICATIONS);
  return settle([]);
}
