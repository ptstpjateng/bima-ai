"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { LogOut } from "lucide-react";

import { Button } from "@/components/ui/button";
import { logout } from "@/lib/auth-client";

/**
 * Client island that lives inside the /me Server Component.
 *
 * Kept narrow — it only knows how to log out and route to /. The visual
 * affordance is the standard ghost button (no destructive red) because
 * logout is not a destructive action from the user's POV.
 */
export function LogoutButton() {
  const router = useRouter();
  const [pending, setPending] = useState(false);

  async function handleClick() {
    if (pending) return;
    setPending(true);
    await logout();
    // Hard refresh so any Server Components that read the cookie re-render.
    router.replace("/");
    router.refresh();
  }

  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      onClick={handleClick}
      disabled={pending}
      aria-label="Keluar dari akun"
    >
      <LogOut aria-hidden="true" />
      <span>{pending ? "Keluar…" : "Keluar"}</span>
    </Button>
  );
}
