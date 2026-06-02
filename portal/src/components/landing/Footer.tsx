/**
 * Minimal footer. Server component — no motion needed here. Migrated to the
 * Bima token system with a quiet top border for separation.
 */
export function Footer() {
  return (
    <footer className="border-t border-border px-4 pb-10 pt-10 sm:px-6 sm:pb-12 lg:px-8">
      <div className="mx-auto max-w-6xl space-y-2 text-center">
        <p className="font-display text-base font-medium text-ink">
          BIMA — Asisten AI Perizinan UMKM
        </p>
        <p className="text-sm text-ink-2">
          Dikembangkan untuk DPMPTSP Provinsi Jawa Tengah
        </p>
        <p className="text-xs text-ink-3">© 2026 · Powered by AI</p>
      </div>
    </footer>
  );
}
