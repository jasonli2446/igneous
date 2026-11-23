"use client";

import { useLayoutEffect, useSyncExternalStore } from "react";
import { applyTheme, chooseTheme, isDark, onThemeChange, OS_DARK, preferredTheme, savedTheme } from "@/lib/theme";

/** Whether the page is dark right now. Server rendering assumes light; the head script has already set the real
 *  theme by the time this hydrates, and React re-renders with it. */
export function useTheme() {
  return useSyncExternalStore(onThemeChange, isDark, () => false);
}

export default function ThemeToggle() {
  const dark = useTheme();

  useLayoutEffect(() => {
    // In development React remounts the page once and resets <html> to the attributes it renders, dropping the one
    // the head script set: put it back before paint. Until you choose, follow the OS when it switches.
    applyTheme(preferredTheme());
    const os = matchMedia(OS_DARK);
    const follow = () => { if (!savedTheme()) applyTheme(os.matches ? "dark" : "light"); };
    os.addEventListener("change", follow);
    return () => os.removeEventListener("change", follow);
  }, []);

  // The label comes from CSS (the dark: variant), so it is right from the first paint, before React hydrates.
  return (
    <button type="button" onClick={() => chooseTheme(dark ? "light" : "dark")}
      className="ml-auto rounded-md px-2 py-1 text-muted hover:bg-hover hover:text-ink">
      <span className="sr-only">Switch to </span>
      <span className="dark:hidden">Dark</span>
      <span className="hidden dark:inline">Light</span>
      <span className="sr-only"> mode</span>
    </button>
  );
}
