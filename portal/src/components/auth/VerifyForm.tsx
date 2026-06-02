"use client";

import { useEffect, useState, type FormEvent } from "react";
import { KeyRound, RotateCcw } from "lucide-react";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";

import { Button } from "@/components/ui/button";
import { AuthCard } from "@/components/auth/AuthLayout";
import { VerificationCode } from "@/components/auth/VerificationCode";
import { NotAvailablePanel } from "@/components/auth/NotAvailablePanel";
import { verifyMagicLinkCode } from "@/lib/auth-flow";

const RESEND_SECONDS = 30;

/**
 * VerifyForm — the 6-digit code entry step of the (scaffolded) magic-link
 * flow. Fully built + accessible (paste-aware boxes, resend countdown), but
 * verifyMagicLinkCode() returns not_implemented today, so submitting reveals
 * the honest NotAvailablePanel. NEVER routes to /me or fabricates a session.
 */
export function VerifyForm({ email }: { email: string | null }) {
  const reduce = useReducedMotion();
  const [code, setCode] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [notAvailable, setNotAvailable] = useState<string | null>(null);
  const [secondsLeft, setSecondsLeft] = useState(RESEND_SECONDS);

  const canSubmit = /^\d{6}$/.test(code);

  useEffect(() => {
    if (secondsLeft <= 0) return;
    const t = setTimeout(() => setSecondsLeft((s) => s - 1), 1000);
    return () => clearTimeout(t);
  }, [secondsLeft]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFieldError(null);
    setPending(true);

    const result = await verifyMagicLinkCode(email ?? "", code);
    setPending(false);

    if (result.ok) {
      // Reserved for FLOW_BACKEND_READY. Not reachable today.
      return;
    }
    if (result.reason === "invalid_input") {
      setFieldError(result.message);
      return;
    }
    setNotAvailable(result.message);
  }

  return (
    <AnimatePresence mode="wait" initial={false}>
      {notAvailable ? (
        <motion.div
          key="not-available"
          initial={reduce ? false : { opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
          transition={{ duration: reduce ? 0 : 0.2, ease: "easeOut" }}
        >
          <NotAvailablePanel message={notAvailable} />
        </motion.div>
      ) : (
        <motion.div
          key="form"
          initial={reduce ? false : { opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
          transition={{ duration: reduce ? 0 : 0.2, ease: "easeOut" }}
        >
          <AuthCard>
            <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-6">
              {email && (
                <p className="text-sm text-ink-2">
                  Kami kirim kode 6 digit ke{" "}
                  <span className="font-medium text-ink">{email}</span>.
                </p>
              )}

              <VerificationCode
                value={code}
                onChange={(v) => {
                  setCode(v);
                  if (fieldError) setFieldError(null);
                }}
                error={fieldError}
                disabled={pending}
              />

              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <Button
                  type="submit"
                  variant="primary"
                  size="lg"
                  disabled={pending || !canSubmit}
                  aria-busy={pending || undefined}
                >
                  <KeyRound aria-hidden="true" />
                  <span>{pending ? "Memverifikasi…" : "Verifikasi"}</span>
                </Button>

                <button
                  type="button"
                  disabled={secondsLeft > 0}
                  onClick={() => setSecondsLeft(RESEND_SECONDS)}
                  className="inline-flex cursor-pointer items-center gap-1.5 text-sm text-ink-2 transition-colors hover:text-ink disabled:cursor-not-allowed disabled:text-ink-3"
                >
                  <RotateCcw className="size-4" aria-hidden="true" />
                  {secondsLeft > 0
                    ? `Kirim ulang dalam ${secondsLeft}s`
                    : "Kirim ulang kode"}
                </button>
              </div>
            </form>
          </AuthCard>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
