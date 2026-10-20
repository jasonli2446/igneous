"use client";

import { useEffect, useState } from "react";

/** Follows the OS until you choose; your choice is remembered. Exposes the current theme to the graph. */
export function useTheme() {
  const [dark, setDark] = useState(false);
  useEffect(() => {
    const sync = () => setDark(document.documentElement.classList.contains("dark"));
    sync();
    const obs = new MutationObserver(sync);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);
  return dark;
}

export default function ThemeToggle() {
  const dark = useTheme();
  useEffect(() => {
    let saved: string | null = null;
    try { saved = localStorage.getItem("theme"); } catch {}
    const want = saved ? saved === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.classList.toggle("dark", want);
  }, []);
  const flip = () => {
    const next = !dark;
    document.documentElement.classList.toggle("dark", next);
    try { localStorage.setItem("theme", next ? "dark" : "light"); } catch {}
  };
  return (
    <button onClick={flip} aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
      className="ml-auto rounded-md px-2 py-1 text-muted hover:bg-hover hover:text-ink">
      {dark ? "Light" : "Dark"}
    </button>
  );
}
