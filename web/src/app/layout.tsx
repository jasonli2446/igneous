import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Igneous",
  description: "A creation layer for personal knowledge graphs",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="flex min-h-full flex-col bg-white text-zinc-900">
        <nav className="flex h-[49px] shrink-0 items-center gap-6 border-b border-zinc-200 px-4 text-sm">
          <span className="font-semibold">Igneous</span>
          <Link href="/" className="text-zinc-600 hover:text-zinc-900">Explorer</Link>
          <Link href="/ask" className="text-zinc-600 hover:text-zinc-900">Ask</Link>
          <Link href="/search" className="text-zinc-600 hover:text-zinc-900">Search</Link>
        </nav>
        {children}
      </body>
    </html>
  );
}
