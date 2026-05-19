"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { Search, LogOut, User as UserIcon, Menu } from "lucide-react";
import { toast } from "sonner";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Sheet,
  SheetContent,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { MobileSidebarBody } from "@/components/layout/sidebar";

/**
 * Top bar — 60px tall, sits above the main scroll area.
 *
 * Left: command-palette trigger (⌘K / Ctrl+K).
 * Right: user avatar dropdown with sign-out.
 *
 * Phase 2 will resolve the avatar initials from the JWT-derived user object.
 * For Phase 1 we show "A" — the shell renders identically without a real user
 * since the middleware already gated the route.
 */
export function TopBar() {
  const router = useRouter();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  async function handleSignOut() {
    try {
      await fetch("/api/auth/set-token", { method: "DELETE" });
      toast.success("Berhasil keluar.");
      router.push("/login");
      router.refresh();
    } catch {
      toast.error("Gagal keluar. Coba lagi.");
    }
  }

  return (
    <header className="h-15 sticky top-0 z-20 bg-surface-base/95 backdrop-blur supports-[backdrop-filter]:bg-surface-base/75 flex items-center justify-between gap-2 px-4 md:px-8 py-3">
      <div className="flex items-center gap-1">
        <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
          <SheetTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              className="md:hidden text-text-secondary hover:text-text-primary hover:bg-surface-card-hover"
              aria-label="Buka navigasi"
            >
              <Menu className="size-5" aria-hidden />
            </Button>
          </SheetTrigger>
          <SheetContent
            side="left"
            className="w-60 max-w-[80vw] p-0 bg-surface-low"
            showCloseButton={false}
          >
            <SheetTitle className="sr-only">Navigasi utama</SheetTitle>
            <MobileSidebarBody onNavigate={() => setMobileNavOpen(false)} />
          </SheetContent>
        </Sheet>

        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => setPaletteOpen(true)}
          className="text-text-secondary hover:text-text-primary hover:bg-surface-card-hover gap-2"
          aria-label="Open command palette"
        >
          <Search className="size-4" aria-hidden />
          <span className="text-sm hidden sm:inline">Search…</span>
          <kbd className="ml-3 hidden md:inline-flex items-center gap-1 rounded bg-surface-card px-1.5 py-0.5 text-[10px] font-mono text-text-muted">
            <span className="text-xs">⌘</span>K
          </kbd>
        </Button>
      </div>

      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="rounded-full focus:outline-none focus:ring-2 focus:ring-brand-navy/40"
            aria-label="Open user menu"
          >
            <Avatar className="size-9 bg-brand-navy-muted">
              <AvatarFallback className="bg-brand-navy text-white font-display text-sm">
                A
              </AvatarFallback>
            </Avatar>
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          className="bg-surface-elevated border-0 min-w-48"
        >
          <DropdownMenuLabel className="text-text-muted text-xs font-normal">
            Admin
          </DropdownMenuLabel>
          <DropdownMenuSeparator className="bg-white/5" />
          <DropdownMenuItem
            className="text-text-secondary hover:text-text-primary focus:text-text-primary"
            disabled
          >
            <UserIcon className="size-4" aria-hidden />
            <span>Profile (Phase 2)</span>
          </DropdownMenuItem>
          <DropdownMenuItem
            className="text-text-secondary hover:text-text-primary focus:text-text-primary"
            onSelect={handleSignOut}
          >
            <LogOut className="size-4" aria-hidden />
            <span>Sign out</span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <CommandDialog open={paletteOpen} onOpenChange={setPaletteOpen}>
        <CommandInput placeholder="Search admin…" />
        <CommandList>
          <CommandEmpty>Tidak ada hasil.</CommandEmpty>
          <CommandGroup heading="Navigation">
            <CommandItem
              onSelect={() => {
                setPaletteOpen(false);
                router.push("/dashboard");
              }}
            >
              Dashboard
            </CommandItem>
            <CommandItem
              onSelect={() => {
                setPaletteOpen(false);
                router.push("/ai-interactions");
              }}
            >
              AI Interactions
            </CommandItem>
            <CommandItem
              onSelect={() => {
                setPaletteOpen(false);
                router.push("/kbli");
              }}
            >
              KBLI
            </CommandItem>
          </CommandGroup>
        </CommandList>
      </CommandDialog>
    </header>
  );
}
