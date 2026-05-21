"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  Inbox,
  MessageCircle,
  FileText,
  Database,
  Network,
  Settings,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";

type NavItem = {
  label: string;
  href: string;
  icon: LucideIcon;
  disabled?: boolean;
};

// Sprint B.2 nav set per brief:
//   Dasbor, Interaksi AI, KBLI (live) + Pengaturan (placeholder, disabled).
// Users CRUD + Ingestion CRUD are Sprint C scope — intentionally absent.
const NAV_ITEMS: NavItem[] = [
  { label: "Dasbor", href: "/dashboard", icon: LayoutDashboard },
  { label: "Kotak Masuk", href: "/inbox", icon: Inbox },
  { label: "Interaksi AI", href: "/ai-interactions", icon: MessageCircle },
  { label: "KBLI", href: "/kbli", icon: FileText },
  { label: "Sumber Data", href: "/data", icon: Database },
  { label: "Arsitektur", href: "/architecture", icon: Network },
  { label: "Pengaturan", href: "/settings", icon: Settings, disabled: true },
];

/**
 * Shared nav body — used by both the persistent desktop sidebar and the
 * mobile Sheet drawer. `onNavigate` lets the Sheet auto-close when a link is
 * tapped.
 */
function SidebarNavBody({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();

  return (
    <>
      <div className="px-6 pt-6 pb-8">
        <Link
          href="/dashboard"
          className="block group"
          onClick={onNavigate}
        >
          <span className="font-display text-2xl font-semibold tracking-tight text-text-primary">
            BIMA
          </span>
          <span className="block text-[10px] font-medium uppercase tracking-widest text-text-muted mt-0.5">
            Konsol Admin
          </span>
        </Link>
      </div>

      <nav className="flex-1 px-3 space-y-1 overflow-y-auto">
        {NAV_ITEMS.map((item) => {
          const active =
            pathname === item.href || pathname?.startsWith(`${item.href}/`);
          const Icon = item.icon;
          if (item.disabled) {
            return (
              <span
                key={item.href}
                aria-disabled="true"
                title="Sprint C"
                className="flex items-center gap-3 px-4 py-2 rounded-pill text-sm text-text-muted cursor-not-allowed"
              >
                <Icon className="size-4" aria-hidden />
                <span>{item.label}</span>
              </span>
            );
          }
          return (
            <Link
              key={item.href}
              href={item.href}
              onClick={onNavigate}
              className={cn(
                "flex items-center gap-3 px-4 py-2 rounded-pill text-sm transition-colors",
                active
                  ? "bg-brand-navy text-white"
                  : "text-text-secondary hover:text-text-primary hover:bg-surface-card-hover"
              )}
              aria-current={active ? "page" : undefined}
            >
              <Icon className="size-4" aria-hidden />
              <span>{item.label}</span>
            </Link>
          );
        })}
      </nav>

      <div className="px-6 py-4 text-[10px] text-text-muted">
        Phase 1 shell · v0.1.0
      </div>
    </>
  );
}

/**
 * Persistent sidebar — visible at md (≥768px) and up. On mobile, the sidebar
 * is hidden in favour of a hamburger-triggered Sheet rendered by TopBar.
 */
export function Sidebar() {
  return (
    <aside
      className="hidden md:flex fixed left-0 top-0 bottom-0 w-60 bg-surface-low flex-col z-30"
      aria-label="Primary navigation"
    >
      <SidebarNavBody />
    </aside>
  );
}

/**
 * Body of the mobile drawer — rendered inside a shadcn Sheet by TopBar.
 * Wrapping element matches the desktop sidebar look so the visual is consistent.
 */
export function MobileSidebarBody({ onNavigate }: { onNavigate: () => void }) {
  return (
    <div className="flex h-full flex-col bg-surface-low">
      <SidebarNavBody onNavigate={onNavigate} />
    </div>
  );
}
