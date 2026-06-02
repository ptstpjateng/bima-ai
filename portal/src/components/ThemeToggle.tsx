"use client";

import { useSyncExternalStore } from "react";
import { Moon, Sun } from "lucide-react";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";

import { Button } from "@/components/ui/button";

/**
 * ThemeToggle — a real, shippable 2-state toggle (Moon ↔ Sun) that persists
 * to localStorage `bima-theme` and toggles the `.dark`/`.light` class on
 * <html>. The pre-hydration <ThemeScript> already applied the stored theme
 * before paint, so this component just reads the resolved state and lets the
 * user flip it.
 *
 * We read the current theme via useSyncExternalStore (subscribing to <html>
 * class mutations + the OS media query) so the value is always in sync with
 * the DOM the script set up — no effect-driven setState, no hydration
 * mismatch. getServerSnapshot returns "light" (the light-first default), and
 * during hydration the icon resolves to the real value on the client tick.
 *
 * Default is "system": until the user explicitly toggles, the OS preference
 * drives the palette via prefers-color-scheme. The first toggle writes an
 * explicit light|dark and from then on the user's choice wins.
 */
type Resolved = "light" | "dark";

function systemPrefersDark(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
  );
}

function resolveCurrent(): Resolved {
  if (typeof document === "undefined") return "light";
  const root = document.documentElement;
  if (root.classList.contains("dark")) return "dark";
  if (root.classList.contains("light")) return "light";
  return systemPrefersDark() ? "dark" : "light";
}

/** Subscribe to <html> class changes + OS scheme changes. */
function subscribe(onChange: () => void): () => void {
  const mql = window.matchMedia("(prefers-color-scheme: dark)");
  mql.addEventListener("change", onChange);
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["class"],
  });
  return () => {
    mql.removeEventListener("change", onChange);
    observer.disconnect();
  };
}

export function ThemeToggle({ className }: { className?: string }) {
  const reduce = useReducedMotion();
  const theme = useSyncExternalStore<Resolved>(
    subscribe,
    resolveCurrent,
    () => "light", // server snapshot — light-first
  );

  function toggle() {
    const next: Resolved = theme === "dark" ? "light" : "dark";
    const root = document.documentElement;
    root.classList.remove("light", "dark");
    root.classList.add(next);
    try {
      localStorage.setItem("bima-theme", next);
    } catch {
      // private mode / storage disabled — toggle still works for this session
    }
    // MutationObserver in subscribe() picks up the class change and re-renders.
  }

  const isDark = theme === "dark";

  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      onClick={toggle}
      aria-pressed={isDark}
      aria-label={isDark ? "Ganti ke mode terang" : "Ganti ke mode gelap"}
      title={isDark ? "Mode terang" : "Mode gelap"}
      className={className}
    >
      <AnimatePresence mode="wait" initial={false}>
        <motion.span
          key={isDark ? "sun" : "moon"}
          initial={reduce ? false : { opacity: 0, rotate: -45, scale: 0.7 }}
          animate={{ opacity: 1, rotate: 0, scale: 1 }}
          exit={reduce ? { opacity: 0 } : { opacity: 0, rotate: 45, scale: 0.7 }}
          transition={{ duration: reduce ? 0 : 0.2, ease: "easeOut" }}
          className="inline-flex"
        >
          {isDark ? (
            <Sun className="size-4" aria-hidden="true" />
          ) : (
            <Moon className="size-4" aria-hidden="true" />
          )}
        </motion.span>
      </AnimatePresence>
    </Button>
  );
}
