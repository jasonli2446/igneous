import type { Metadata } from "next";
import Link from "next/link";
import ThemeToggle from "@/components/ThemeToggle";
import "./globals.css";

export const metadata: Metadata = {
  title: "Igneous",
  description: "A creation layer for personal knowledge graphs",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full" suppressHydrationWarning>
      <body className="flex min-h-full flex-col">
        <nav className="flex h-[49px] shrink-0 items-center gap-5 border-b border-line bg-panel px-4 text-sm">
          <span className="font-semibold tracking-tight">Igneous</span>
          <Link href="/" className="text-muted hover:text-ink">Explorer</Link>
          <Link href="/ask" className="text-muted hover:text-ink">Ask</Link>
          <Link href="/search" className="text-muted hover:text-ink">Search</Link>
          <Link href="/import" className="text-muted hover:text-ink">Import</Link>
          <Link href="/settings" className="text-muted hover:text-ink">Settings</Link>
          <ThemeToggle />
        </nav>
        {children}
      </body>
    </html>
  );
}
