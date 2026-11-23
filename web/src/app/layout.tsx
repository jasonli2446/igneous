import type { Metadata } from "next";
import Link from "next/link";
import NavLinks from "@/components/NavLinks";
import ThemeToggle from "@/components/ThemeToggle";
import { THEME_SCRIPT } from "@/lib/theme";
import "./globals.css";

export const metadata: Metadata = {
  title: { template: "%s · Igneous", default: "Igneous" },
  description: "A creation layer for personal knowledge graphs",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  // The head script sets data-theme on <html> before the first paint; suppressHydrationWarning lets React keep it.
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="flex min-h-dvh flex-col">
        <nav aria-label="Main"
          className="flex min-h-[49px] shrink-0 flex-wrap items-center gap-1 border-b border-line bg-panel px-3 py-1.5 text-sm sm:px-4">
          <Link href="/" className="mr-1 font-semibold tracking-tight sm:mr-3">Igneous</Link>
          <NavLinks />
          <ThemeToggle />
        </nav>
        {children}
      </body>
    </html>
  );
}
