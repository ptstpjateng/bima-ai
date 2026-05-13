"use client";

import { usePathname } from "next/navigation";
import { AnimatePresence, motion } from "framer-motion";
import { Sidebar } from "@/components/layout/sidebar";
import { TopBar } from "@/components/layout/topbar";

/**
 * Protected admin shell. The middleware in `src/middleware.ts` has already
 * verified the admin_token cookie before this layout renders, so we can assume
 * an authenticated session without a client-side guard.
 *
 * Sidebar is fixed at 240px (w-60). Main column offsets left and scrolls
 * independently from the sidebar to keep nav visible on long pages.
 */
export default function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();

  return (
    <div className="min-h-screen bg-surface-base">
      <Sidebar />
      <div className="ml-60 flex min-h-screen flex-col">
        <TopBar />
        <main className="flex-1 p-8 overflow-y-auto">
          <AnimatePresence mode="wait">
            <motion.div
              key={pathname}
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={{ duration: 0.2, ease: "easeOut" }}
            >
              {children}
            </motion.div>
          </AnimatePresence>
        </main>
      </div>
    </div>
  );
}
