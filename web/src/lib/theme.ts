// The color theme is `data-theme` on <html>: "light" or "dark". THEME_SCRIPT sets it before the first paint (the
// root layout runs it in <head>); the toggle changes it and remembers the choice. With no choice saved, the theme
// follows the OS. No React here, so the root layout (a Server Component) can import the script.

export type Theme = "light" | "dark";

const KEY = "theme";
export const OS_DARK = "(prefers-color-scheme: dark)";

/** Runs in <head> while the page is parsed. Keep it in step with preferredTheme below. */
export const THEME_SCRIPT = `(function(){var t;try{t=localStorage.getItem("${KEY}")}catch(e){}`
  + `if(t!=="light"&&t!=="dark")t=matchMedia("${OS_DARK}").matches?"dark":"light";`
  + `document.documentElement.setAttribute("data-theme",t)})()`;

/** The theme you chose, if any (storage can be unavailable, e.g. blocked cookies). */
export function savedTheme(): Theme | null {
  try {
    const t = localStorage.getItem(KEY);
    return t === "light" || t === "dark" ? t : null;
  } catch {
    return null;
  }
}

export function preferredTheme(): Theme {
  return savedTheme() ?? (matchMedia(OS_DARK).matches ? "dark" : "light");
}

export function applyTheme(theme: Theme) {
  document.documentElement.setAttribute("data-theme", theme);
}

export function chooseTheme(theme: Theme) {
  applyTheme(theme);
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // Not remembered: this page still switches.
  }
}

/** useSyncExternalStore's subscribe: calls back whenever data-theme changes. */
export function onThemeChange(callback: () => void) {
  const observer = new MutationObserver(callback);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return () => observer.disconnect();
}

export const isDark = () => document.documentElement.getAttribute("data-theme") === "dark";
