"use client";

import { Bell, LogOut, Menu, User } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { useAuth } from "@/context/AuthContext";
import { useRouter } from "next/navigation";

interface NavbarProps {
  onMenuClick: () => void;
}

export function Navbar({ onMenuClick }: NavbarProps) {
  const { user, signOut } = useAuth();
  const router = useRouter();
  const [dropdownOpen, setDropdownOpen] = useState(false);

  async function handleLogout() {
    await signOut();
    router.push("/login");
  }

  return (
    <header className="sticky top-0 z-30 border-b border-gray-100 bg-white/95 backdrop-blur-sm">
      <div className="flex h-16 items-center gap-4 px-4 sm:px-6">
        {/* Hamburger (mobile) */}
        <button
          onClick={onMenuClick}
          className="flex h-9 w-9 items-center justify-center rounded-lg text-gray-500 hover:bg-gray-100 lg:hidden"
        >
          <Menu className="h-5 w-5" />
        </button>

        {/* Logo */}
        <Link href="/dashboard" className="flex items-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-800 text-xs font-bold text-white">
            B
          </div>
          <span className="hidden font-bold text-brand-800 sm:block">
            BIMA<span className="text-brand-500">-AI</span>
          </span>
        </Link>

        <div className="flex-1" />

        {/* Notification bell */}
        <button className="flex h-9 w-9 items-center justify-center rounded-lg text-gray-500 hover:bg-gray-100">
          <Bell className="h-5 w-5" />
        </button>

        {/* User menu */}
        <div className="relative">
          <button
            onClick={() => setDropdownOpen((v) => !v)}
            className="flex items-center gap-2 rounded-xl p-1.5 hover:bg-gray-100"
          >
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-brand-100 text-brand-700">
              <User className="h-4 w-4" />
            </div>
            <div className="hidden text-left sm:block">
              <p className="max-w-[120px] truncate text-xs font-semibold text-gray-800">
                {user?.name ?? "Pengguna"}
              </p>
              <p className="text-[10px] text-gray-500 capitalize">
                {user?.role ?? "msme"}
              </p>
            </div>
          </button>

          {dropdownOpen && (
            <>
              <div
                className="fixed inset-0 z-10"
                onClick={() => setDropdownOpen(false)}
              />
              <div className="absolute right-0 z-20 mt-2 w-48 rounded-xl border border-gray-100 bg-white py-1 shadow-lg">
                <Link
                  href="/profile"
                  onClick={() => setDropdownOpen(false)}
                  className="flex items-center gap-2 px-4 py-2 text-sm text-gray-700 hover:bg-gray-50"
                >
                  <User className="h-4 w-4" />
                  Profil Saya
                </Link>
                <button
                  onClick={handleLogout}
                  className="flex w-full items-center gap-2 px-4 py-2 text-sm text-red-600 hover:bg-red-50"
                >
                  <LogOut className="h-4 w-4" />
                  Keluar
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
