"use client";

import { useRef, useId, type ClipboardEvent, type KeyboardEvent } from "react";

import { cn } from "@/lib/utils";

const LENGTH = 6;

/**
 * VerificationCode — six grouped single-character inputs for a 6-digit
 * one-time code. Paste-aware (pasting a 6-digit code fills every box),
 * arrow/backspace navigation, inputMode=numeric, autocomplete=one-time-code.
 *
 * Accessibility: wrapped in a <fieldset>/<legend>; each box has an aria-label
 * "Digit N dari 6". The parent owns the combined value as a string of digits.
 */
export function VerificationCode({
  value,
  onChange,
  disabled,
  error,
  legend = "Kode verifikasi 6 digit",
}: {
  /** Current code, 0–6 digits. */
  value: string;
  onChange: (next: string) => void;
  disabled?: boolean;
  error?: string | null;
  legend?: string;
}) {
  const id = useId();
  const errId = `${id}-error`;
  const refs = useRef<Array<HTMLInputElement | null>>([]);
  const digits = value.split("").slice(0, LENGTH);

  function setDigit(index: number, char: string) {
    const next = value.split("");
    next[index] = char;
    onChange(next.join("").replace(/\D/g, "").slice(0, LENGTH));
  }

  function handleInput(index: number, raw: string) {
    const char = raw.replace(/\D/g, "").slice(-1);
    if (!char) {
      setDigit(index, "");
      return;
    }
    setDigit(index, char);
    if (index < LENGTH - 1) refs.current[index + 1]?.focus();
  }

  function handleKeyDown(index: number, e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Backspace" && !digits[index] && index > 0) {
      refs.current[index - 1]?.focus();
    } else if (e.key === "ArrowLeft" && index > 0) {
      e.preventDefault();
      refs.current[index - 1]?.focus();
    } else if (e.key === "ArrowRight" && index < LENGTH - 1) {
      e.preventDefault();
      refs.current[index + 1]?.focus();
    }
  }

  function handlePaste(e: ClipboardEvent<HTMLInputElement>) {
    e.preventDefault();
    const pasted = e.clipboardData
      .getData("text")
      .replace(/\D/g, "")
      .slice(0, LENGTH);
    if (!pasted) return;
    onChange(pasted);
    const focusIndex = Math.min(pasted.length, LENGTH - 1);
    refs.current[focusIndex]?.focus();
  }

  return (
    <fieldset
      className="flex flex-col gap-2"
      aria-describedby={error ? errId : undefined}
    >
      <legend className="text-xs font-medium uppercase tracking-wide text-ink-2">
        {legend}
      </legend>
      <div className="flex items-center gap-2 sm:gap-3">
        {Array.from({ length: LENGTH }).map((_, i) => (
          <input
            key={i}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="text"
            inputMode="numeric"
            autoComplete={i === 0 ? "one-time-code" : "off"}
            maxLength={1}
            disabled={disabled}
            value={digits[i] ?? ""}
            aria-label={`Digit ${i + 1} dari ${LENGTH}`}
            aria-invalid={error ? true : undefined}
            onChange={(e) => handleInput(i, e.target.value)}
            onKeyDown={(e) => handleKeyDown(i, e)}
            onPaste={handlePaste}
            className={cn(
              "size-12 rounded-card border bg-surface-2 text-center font-mono text-lg text-ink outline-none transition-shadow duration-150 focus:ring-2 focus:ring-accent-ink/40 disabled:opacity-60 sm:size-14",
              error
                ? "border-error/60"
                : "border-border focus:border-accent-ink",
            )}
          />
        ))}
      </div>
      {error && (
        <p id={errId} role="alert" className="text-xs font-medium text-error">
          {error}
        </p>
      )}
    </fieldset>
  );
}
