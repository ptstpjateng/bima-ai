"use client";

import {
  FileText,
  LayoutDashboard,
  PlusCircle,
  User,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

const BOTTOM_NAV = [
  { href: "/dashboard", label: "Beranda", icon: LayoutDashboard },
  { href: "/permits", label: "Perizinan", icon: FileText },
  { href: "/permits/apply", label: "Ajukan", icon: PlusCircle },
  { href: "/profile", label: "Profil", icon: User },
] as const;

export function BottomNav() {
  const pathname = usePathname();

  return (
    <nav className="fixed bottom-0 left-0 right-0 z-30 border-t border-gray-100 bg-white/95 backdrop-blur-sm lg:hidden">
      <div className="flex">
        {BOTTOM_NAV.map(({ href, label, icon: Icon }) => {
          const isActive =
            href === "/dashboard"
              ? pathname === "/dashboard"
              : pathname.startsWith(href);

          return (
            <Link
              key={href}
              href={href}
              className={cn(
                "flex flex-1 flex-col items-center gap-0.5 py-2.5 text-[10px] font-medium transition-colors",
                isActive
                  ? "text-brand-600"
                  : "text-gray-500 hover:text-gray-700",
              )}
            >
              <Icon
                className={cn(
                  "h-5 w-5",
                  href === "/permits/apply" && isActive && "text-brand-600",
                )}
              />
              {label}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
