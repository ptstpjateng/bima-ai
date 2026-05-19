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
 * Desktop (md+): persistent 240px (w-60) sidebar with the main column offset.
 * Mobile (<md): sidebar hides and is replaced by a hamburger-triggered Sheet
 * rendered inside TopBar — main column has zero left margin so content is
 * fully reachable on narrow screens.
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
      <div className="md:ml-60 flex min-h-screen flex-col">
        <TopBar />
        <main className="flex-1 p-4 md:p-8 overflow-y-auto">
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
