"use client";

import { useState, type FormEvent } from "react";
import { Mail } from "lucide-react";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";

import { Button } from "@/components/ui/button";
import { AuthCard } from "@/components/auth/AuthLayout";
import { EmailInput } from "@/components/auth/EmailInput";
import { NotAvailablePanel } from "@/components/auth/NotAvailablePanel";
import {
  startMagicLinkFlow,
  requestPasswordReset,
  type AuthFlowResult,
} from "@/lib/auth-flow";

/**
 * MagicLinkForm — the shared single-email-field form for the scaffold-only
 * flows: account-create (magic-link signup) and password reset.
 *
 * It calls the typed stub (which today returns `not_implemented`) and, on a
 * non-implemented result, cross-fades the form out and the honest
 * NotAvailablePanel in. It NEVER fabricates a success or a logged-in state.
 *
 * The submit button stays ENABLED whenever the email is valid so keyboard /
 * AT testing exercises the full path — the honest "belum tersedia" response
 * is what the user receives, not a disabled dead-end.
 */
type Mode = "signup" | "reset";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function MagicLinkForm({
  mode,
  submitLabel,
  hint,
}: {
  mode: Mode;
  submitLabel: string;
  hint?: string;
}) {
  const reduce = useReducedMotion();
  const [email, setEmail] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [notAvailable, setNotAvailable] = useState<string | null>(null);

  const canSubmit = EMAIL_RE.test(email.trim());

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFieldError(null);
    setPending(true);

    const result: AuthFlowResult<{ step: string }> | AuthFlowResult =
      mode === "signup"
        ? await startMagicLinkFlow(email)
        : await requestPasswordReset(email);

    setPending(false);

    if (result.ok) {
      // Reserved for when FLOW_BACKEND_READY flips. Not reachable today.
      return;
    }

    if (result.reason === "invalid_input") {
      setFieldError(result.message);
      return;
    }

    // not_implemented (the only terminal state today) — show the honest panel.
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
            <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-5">
              <EmailInput
                value={email}
                onChange={(v) => {
                  setEmail(v);
                  if (fieldError) setFieldError(null);
                }}
                error={fieldError}
                hint={hint}
                autoFocus
                disabled={pending}
              />
              <Button
                type="submit"
                variant="primary"
                size="lg"
                disabled={pending || !canSubmit}
                aria-busy={pending || undefined}
              >
                <Mail aria-hidden="true" />
                <span>{pending ? "Mengirim…" : submitLabel}</span>
              </Button>
            </form>
          </AuthCard>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
