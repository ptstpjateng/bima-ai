/**
 * Pre-hydration theme script.
 *
 * Runs synchronously in <head>, BEFORE first paint, so the stored theme is
 * applied with zero flash-of-wrong-theme (FOUC). It mirrors the resolution
 * logic in ThemeToggle: read `bima-theme` from localStorage (light|dark|
 * system|absent), and add `.light` or `.dark` to <html> accordingly.
 *
 * globals.css supports BOTH an explicit `.dark`/`.light` class AND
 * `prefers-color-scheme`. The rule there is: `:root:not(.light)` + the media
 * query handles system-dark; an explicit `.dark`/`.light` class overrides.
 * So:
 *   - stored "dark"   → add `.dark`
 *   - stored "light"  → add `.light`
 *   - stored "system" → add neither, but resolve to the matchMedia result so
 *                       ThemeToggle's initial icon is correct
 *   - absent          → behave as "system"
 *
 * suppressHydrationWarning on <html> (set in layout) tolerates the class the
 * script adds before React hydrates.
 */
const SCRIPT = `
(function () {
  try {
    var stored = localStorage.getItem('bima-theme');
    var root = document.documentElement;
    root.classList.remove('light', 'dark');
    if (stored === 'dark') {
      root.classList.add('dark');
    } else if (stored === 'light') {
      root.classList.add('light');
    }
    // stored === 'system' or null: leave classes off so prefers-color-scheme
    // drives the CSS variables. Nothing to add.
  } catch (e) {}
})();
`;

export function ThemeScript() {
  // Static, hardcoded constant — no untrusted input, no injection surface.
  // This is the standard pre-hydration FOUC-guard pattern.
  return <script dangerouslySetInnerHTML={{ __html: SCRIPT }} />;
}
