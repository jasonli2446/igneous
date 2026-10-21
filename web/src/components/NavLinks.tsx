"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Graph" },
  { href: "/ask", label: "Ask" },
  { href: "/search", label: "Search" },
  { href: "/import", label: "Import" },
  { href: "/settings", label: "Sources" },
];

export default function NavLinks() {
  const path = usePathname();
  return (
    <>
      {LINKS.map(({ href, label }) => {
        const active = href === "/" ? path === "/" : path.startsWith(href);
        return (
          <Link key={href} href={href} aria-current={active ? "page" : undefined}
            className={`rounded-md px-2.5 py-1 transition-colors ${active ? "bg-hover font-medium text-ink" : "text-muted hover:bg-hover hover:text-ink"}`}>
            {label}
          </Link>
        );
      })}
    </>
  );
}
