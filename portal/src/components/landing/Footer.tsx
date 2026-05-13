/**
 * Minimal three-line footer. Server component — no motion needed here.
 */
export function Footer() {
  return (
    <footer className="px-6 pb-12 pt-8">
      <div className="mx-auto max-w-6xl space-y-2 text-center">
        <p className="font-display text-base font-medium text-text-primary">
          BIMA — Asisten AI Perizinan UMKM
        </p>
        <p className="text-sm text-text-secondary">
          Dikembangkan untuk DPMPTSP Provinsi Jawa Tengah
        </p>
        <p className="text-xs text-text-muted">© 2026 · Powered by AI</p>
      </div>
    </footer>
  );
}
