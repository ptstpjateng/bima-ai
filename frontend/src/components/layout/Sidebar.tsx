"use client";

import {
  FileText,
  LayoutDashboard,
  MessageSquare,
  PlusCircle,
  User,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

interface SidebarProps {
  open: boolean;
  onClose: () => void;
}

const NAV_ITEMS = [
  { href: "/dashboard", label: "Beranda", icon: LayoutDashboard },
  { href: "/permits", label: "Perizinan Saya", icon: FileText },
  { href: "/permits/apply", label: "Ajukan Izin Baru", icon: PlusCircle },
  { href: "/profile", label: "Profil & Usaha", icon: User },
] as const;

export function Sidebar({ open, onClose }: SidebarProps) {
  const pathname = usePathname();

  return (
    <>
      {/* Mobile overlay */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm lg:hidden"
          onClick={onClose}
        />
      )}

      {/* Sidebar panel */}
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-50 flex w-64 flex-col border-r border-gray-100 bg-white transition-transform duration-300 ease-in-out lg:static lg:z-auto lg:translate-x-0 lg:transition-none",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        {/* Close button (mobile) */}
        <div className="flex h-16 items-center justify-between px-4 lg:hidden">
          <span className="font-bold text-brand-800">
            BIMA<span className="text-brand-500">-AI</span>
          </span>
          <button
            onClick={onClose}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-gray-500 hover:bg-gray-100"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Navigation */}
        <nav className="flex-1 space-y-0.5 overflow-y-auto px-3 py-4">
          <p className="mb-3 px-3 text-[10px] font-semibold uppercase tracking-widest text-gray-400">
            Menu Utama
          </p>
          {NAV_ITEMS.map(({ href, label, icon: Icon }) => {
            const isActive =
              href === "/dashboard"
                ? pathname === "/dashboard"
                : pathname.startsWith(href);

            return (
              <Link
                key={href}
                href={href}
                onClick={onClose}
                className={cn(
                  "flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-brand-50 text-brand-700"
                    : "text-gray-600 hover:bg-gray-100 hover:text-gray-900",
                )}
              >
                <Icon
                  className={cn(
                    "h-4 w-4 flex-shrink-0",
                    isActive ? "text-brand-600" : "text-gray-400",
                  )}
                />
                {label}
              </Link>
            );
          })}
        </nav>

        {/* Footer */}
        <div className="border-t border-gray-100 p-4">
          <div className="rounded-xl bg-brand-50 p-3">
            <div className="mb-1 flex items-center gap-2">
              <MessageSquare className="h-4 w-4 text-brand-600" />
              <p className="text-xs font-semibold text-brand-700">
                Butuh bantuan?
              </p>
            </div>
            <p className="text-[11px] leading-relaxed text-brand-600">
              Konsultasikan perizinan Anda via WhatsApp bersama asisten BIMA-AI
            </p>
          </div>
        </div>
      </aside>
    </>
  );
}
