"use client";

import { useId } from "react";

/**
 * EmailInput — a single labelled email field with format validation wiring.
 * Pill radius, ring-1 border → focus ring-2 accent-ink (matches the NIK/NPWP
 * field on /login). Error is surfaced via aria-invalid + aria-describedby;
 * the actual error message lives in the parent (role=alert).
 */
export function EmailInput({
  value,
  onChange,
  error,
  hint,
  disabled,
  autoFocus,
  label = "Alamat email",
  placeholder = "nama@email.com",
}: {
  value: string;
  onChange: (v: string) => void;
  error?: string | null;
  hint?: string;
  disabled?: boolean;
  autoFocus?: boolean;
  label?: string;
  placeholder?: string;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errId = `${id}-error`;

  return (
    <div className="flex flex-col gap-2">
      <label
        htmlFor={id}
        className="text-xs font-medium uppercase tracking-wide text-ink-2"
      >
        {label}
      </label>
      <input
        id={id}
        name="email"
        type="email"
        inputMode="email"
        autoComplete="email"
        autoFocus={autoFocus}
        required
        disabled={disabled}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? errId : hint ? hintId : undefined}
        className="w-full rounded-pill border border-border bg-surface-2 px-5 py-3 text-base text-ink outline-none transition-shadow duration-150 placeholder:text-ink-3 focus:border-accent-ink focus:ring-2 focus:ring-accent-ink/40 disabled:opacity-60"
      />
      {hint && !error && (
        <p id={hintId} className="text-xs text-ink-3">
          {hint}
        </p>
      )}
      {error && (
        <p id={errId} role="alert" className="text-xs font-medium text-error">
          {error}
        </p>
      )}
    </div>
  );
}
