import type { Metadata } from "next";
import Link from "next/link";
import NavLinks from "@/components/NavLinks";
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
        <nav className="flex h-[49px] shrink-0 items-center gap-1 border-b border-line bg-panel px-4 text-sm">
          <Link href="/" className="mr-3 font-semibold tracking-tight">Igneous</Link>
          <NavLinks />
          <ThemeToggle />
        </nav>
        {children}
      </body>
    </html>
  );
}
