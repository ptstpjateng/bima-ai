"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
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
//   Dashboard, AI Interactions, KBLI (live) + Settings (placeholder, disabled).
// Users CRUD + Ingestion CRUD are Sprint C scope — intentionally absent.
const NAV_ITEMS: NavItem[] = [
  { label: "Dashboard", href: "/dashboard", icon: LayoutDashboard },
  { label: "AI Interactions", href: "/ai-interactions", icon: MessageCircle },
  { label: "KBLI", href: "/kbli", icon: FileText },
  { label: "Sumber Data", href: "/data", icon: Database },
  { label: "Arsitektur", href: "/architecture", icon: Network },
  { label: "Settings", href: "/settings", icon: Settings, disabled: true },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside
      className="fixed left-0 top-0 bottom-0 w-60 bg-surface-low flex flex-col z-30"
      aria-label="Primary navigation"
    >
      <div className="px-6 pt-6 pb-8">
        <Link href="/dashboard" className="block group">
          <span className="font-display text-2xl font-semibold tracking-tight text-text-primary">
            BIMA
          </span>
          <span className="block text-[10px] font-medium uppercase tracking-widest text-text-muted mt-0.5">
            Admin Console
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
    </aside>
  );
}
