"use client";

import { useState } from "react";
import { useAuth } from "@/context/AuthContext";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { Navbar } from "./Navbar";
import { Sidebar } from "./Sidebar";
import { PageLoader } from "@/components/shared/LoadingSkeleton";
import { BottomNav } from "./BottomNav";

interface AppLayoutProps {
  children: React.ReactNode;
}

export function AppLayout({ children }: AppLayoutProps) {
  const { isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const [sidebarOpen, setSidebarOpen] = useState(false);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      router.replace("/login");
    }
  }, [isLoading, isAuthenticated, router]);

  if (isLoading) return <PageLoader />;
  if (!isAuthenticated) return null;

  return (
    <div className="flex min-h-screen bg-gray-50">
      {/* Sidebar (hidden on mobile, always visible on lg+) */}
      <div className="hidden lg:flex lg:flex-shrink-0">
        <Sidebar open={true} onClose={() => {}} />
      </div>

      {/* Mobile sidebar (drawer) */}
      <div className="lg:hidden">
        <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />
      </div>

      {/* Main content */}
      <div className="flex min-w-0 flex-1 flex-col">
        <Navbar onMenuClick={() => setSidebarOpen(true)} />
        <main className="flex-1 overflow-x-hidden px-4 py-6 pb-24 sm:px-6 lg:pb-6">
          <div className="mx-auto max-w-5xl animate-fade-in">
            {children}
          </div>
        </main>
      </div>

      {/* Bottom navigation (mobile only) */}
      <BottomNav />
    </div>
  );
}
