"use client";

import { useEffect, useState } from "react";

/**
 * Debounce a value (e.g. a search input) so hooks downstream don't fire
 * on every keystroke.
 */
export function useDebounced<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}
