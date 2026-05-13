"use client";

import { Suspense, useState, useTransition } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { motion } from "framer-motion";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const loginSchema = z.object({
  email: z.string().email("Format email tidak valid"),
  password: z.string().min(8, "Minimal 8 karakter"),
});

type LoginValues = z.infer<typeof loginSchema>;

const ADMIN_API_URL =
  process.env.NEXT_PUBLIC_ADMIN_API_URL ?? "http://localhost:8001";

export default function LoginPage() {
  return (
    <Suspense fallback={<LoginFallback />}>
      <LoginForm />
    </Suspense>
  );
}

function LoginFallback() {
  return (
    <main className="min-h-screen flex items-center justify-center bg-surface-base">
      <Loader2 className="size-6 animate-spin text-text-secondary" aria-hidden />
    </main>
  );
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isPending, startTransition] = useTransition();

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<LoginValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: { email: "", password: "" },
  });

  async function onSubmit(values: LoginValues) {
    setIsSubmitting(true);
    try {
      // Talk to admin-api directly. Phase 2 will proxy through a route handler
      // so the JWT never lands in the browser's response body.
      const res = await fetch(`${ADMIN_API_URL}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
        // No credentials — admin-api returns the token in the JSON body, then
        // we hand it to our /api/auth/set-token handler to put it in an
        // httpOnly cookie.
      });

      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        toast.error(
          (errBody as { message?: string })?.message ??
            (res.status === 401
              ? "Email atau kata sandi salah."
              : "Gagal masuk. Coba lagi sebentar lagi.")
        );
        return;
      }

      const data = (await res.json()) as { token?: string };
      if (!data?.token) {
        toast.error("Respons admin-api tidak valid (tidak ada token).");
        return;
      }

      const setRes = await fetch("/api/auth/set-token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: data.token }),
      });

      if (!setRes.ok) {
        toast.error("Gagal menyimpan sesi. Coba lagi.");
        return;
      }

      toast.success("Berhasil masuk.");
      const next = searchParams.get("next");
      const target = next && next.startsWith("/") ? next : "/dashboard";
      startTransition(() => {
        router.push(target);
        router.refresh();
      });
    } catch (err) {
      // Network-level failure — most commonly admin-api isn't deployed yet
      // (expected during Phase 1 development).
      console.error("[login] network error", err);
      toast.error("Tidak bisa menghubungi server admin. Periksa koneksi.");
    } finally {
      setIsSubmitting(false);
    }
  }

  const busy = isSubmitting || isPending;

  return (
    <main className="min-h-screen flex items-center justify-center px-4 bg-surface-base">
      <motion.div
        initial={{ opacity: 0, y: 4 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.2, ease: "easeOut" }}
        className="w-full max-w-md"
      >
        <Card className="bg-surface-card border-0 rounded-card shadow-[0_8px_24px_-8px_rgba(46,77,204,0.3)]">
          <CardHeader className="text-center space-y-3 pt-8">
            <div className="font-display text-3xl font-semibold tracking-tight text-text-primary">
              BIMA
            </div>
            <CardTitle className="font-display text-xl font-medium text-text-primary">
              Admin Console
            </CardTitle>
            <CardDescription className="text-text-secondary text-sm">
              DPMPTSP Jawa Tengah
            </CardDescription>
          </CardHeader>

          <CardContent className="pt-2 pb-8 px-8">
            <form
              onSubmit={handleSubmit(onSubmit)}
              className="space-y-5"
              noValidate
            >
              <div className="space-y-2">
                <Label htmlFor="email" className="text-text-secondary">
                  Email
                </Label>
                <Input
                  id="email"
                  type="email"
                  autoComplete="email"
                  placeholder="anda@dpmptsp.jatengprov.go.id"
                  disabled={busy}
                  aria-invalid={Boolean(errors.email)}
                  {...register("email")}
                />
                {errors.email && (
                  <p className="text-xs text-brand-rose">{errors.email.message}</p>
                )}
              </div>

              <div className="space-y-2">
                <Label htmlFor="password" className="text-text-secondary">
                  Kata sandi
                </Label>
                <Input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  disabled={busy}
                  aria-invalid={Boolean(errors.password)}
                  {...register("password")}
                />
                {errors.password && (
                  <p className="text-xs text-brand-rose">{errors.password.message}</p>
                )}
              </div>

              <motion.div whileTap={{ scale: 0.99 }} transition={{ duration: 0.1 }}>
                <Button
                  type="submit"
                  disabled={busy}
                  className="w-full bg-brand-navy hover:bg-brand-navy-hover text-white font-medium"
                >
                  {busy ? (
                    <>
                      <Loader2 className="size-4 animate-spin" aria-hidden />
                      <span>Memproses…</span>
                    </>
                  ) : (
                    "Masuk"
                  )}
                </Button>
              </motion.div>

              <div className="text-center">
                <button
                  type="button"
                  className="text-xs text-text-muted hover:text-text-secondary transition-colors"
                  onClick={() =>
                    toast.info(
                      "Hubungi administrator sistem untuk reset kata sandi."
                    )
                  }
                >
                  Lupa password?
                </button>
              </div>
            </form>
          </CardContent>
        </Card>

        <p className="text-center text-xs text-text-muted mt-6">
          Phase 1 shell — admin-api auth wired Phase 2.
        </p>
      </motion.div>
    </main>
  );
}
