"use client";

import { useSyncExternalStore } from "react";
import { Info, X } from "lucide-react";

/**
 * MockDataBanner — a persistent, dismissible note shown on every dashboard
 * surface while the data is sample/mock (DASHBOARD_MOCK). This is the honesty
 * guard: it ensures sample data is NEVER mistaken for real SIAP data.
 *
 * - role="note": informational, NOT an alert (the user isn't in error).
 * - Dismiss state persists per browser session (sessionStorage) so it
 *   reappears on a fresh visit but doesn't nag within a session.
 * - Read via useSyncExternalStore (a tiny in-module store backed by
 *   sessionStorage) so there's no effect-driven setState / cascading render.
 * - Info token tint with ink text → AA-safe in both themes.
 */
const KEY = "bima-mock-banner-dismissed";

// In-module pub/sub so a same-tab dismiss re-renders every mounted banner.
const listeners = new Set<() => void>();

function isDismissed(): boolean {
  try {
    return sessionStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

function dismissStore() {
  try {
    sessionStorage.setItem(KEY, "1");
  } catch {
    /* storage disabled */
  }
  listeners.forEach((l) => l());
}

export function MockDataBanner() {
  const dismissed = useSyncExternalStore(
    subscribe,
    isDismissed,
    () => true, // server snapshot: render nothing until client confirms
  );

  if (dismissed) return null;

  return (
    <div
      role="note"
      className="flex items-start gap-3 rounded-card border border-info/40 bg-info/10 px-4 py-3 text-sm text-ink"
    >
      <Info className="mt-0.5 size-4 shrink-0 text-info" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="font-medium">Data contoh</p>
        <p className="mt-0.5 text-ink-2">
          Halaman ini menampilkan data contoh. Integrasi data izin asli dari
          SIAP Jateng masih menyusul — angka dan dokumen di bawah belum
          mencerminkan akun Anda.
        </p>
      </div>
      <button
        type="button"
        onClick={dismissStore}
        aria-label="Tutup pemberitahuan data contoh"
        className="-m-1 inline-flex cursor-pointer rounded-pill p-1 text-ink-3 transition-colors hover:bg-info/15 hover:text-ink"
      >
        <X className="size-4" aria-hidden="true" />
      </button>
    </div>
  );
}
