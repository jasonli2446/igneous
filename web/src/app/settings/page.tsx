import type { Metadata } from "next";
import { Suspense } from "react";
import SourcesPage from "@/components/SourcesPage";
import { Skeleton } from "@/components/ui";

export const metadata: Metadata = { title: "Sources" };

export default function Sources() {
  return (
    <main className="mx-auto w-full max-w-3xl px-4 py-8 sm:px-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight text-ink">Sources</h1>
        <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">
          Connect an account, upload an export, or point Igneous at something on this computer. Everything stays on
          this computer, except text from the sources you opt in to the hosted model (at the bottom). Work down the
          list, then build the graph.
        </p>
      </header>
      {/* Google sign-in comes back with what happened in the URL, which only the browser knows: the list renders there. */}
      <Suspense fallback={
        <div className="mt-6 space-y-3"><Skeleton className="h-24 w-full" /><Skeleton className="h-24 w-full" /></div>
      }>
        <SourcesPage />
      </Suspense>
    </main>
  );
}
