import type { Metadata } from "next";
import AskPage from "@/components/AskPage";

export const metadata: Metadata = { title: "Ask" };

export default function Ask() {
  return (
    <main className="mx-auto w-full max-w-3xl px-4 py-8 sm:px-6">
      <h1 className="text-xl font-semibold tracking-tight">Ask</h1>
      <p className="mt-1 text-sm text-muted">Ask a question in plain language. Answers cite the passages they came from.</p>
      <AskPage />
    </main>
  );
}
