import type { Metadata } from "next";
import { Suspense } from "react";
import SearchPage from "@/components/SearchPage";
import { Skeleton } from "@/components/ui";

export const metadata: Metadata = { title: "Search" };

export default function Search() {
  return (
    <main className="mx-auto w-full max-w-3xl px-4 py-8 sm:px-6">
      <h1 className="text-xl font-semibold tracking-tight">Search</h1>
      <p className="mt-1 text-sm text-muted">
        Find messages, events, and people across everything you have imported. Put a minus before a word to leave it
        out (<span className="font-mono">gas -prices</span>); a question can narrow itself (&ldquo;from Jeff Skilling in May
        2001&rdquo;, &ldquo;in my calendar&rdquo;).
      </p>
      {/* The search lives in the URL (?q=), which only the browser knows: the form renders there. */}
      <Suspense fallback={<Skeleton className="mt-5 h-10 w-full" />}>
        <SearchPage />
      </Suspense>
    </main>
  );
}
