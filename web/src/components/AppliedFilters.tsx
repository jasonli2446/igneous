"use client";

import { sourceLabel } from "@/components/SourceList";
import type { AppliedFilters as Filters } from "@/lib/api";

const DAY_MS = 86_400_000;
const day = (d: Date) => d.toLocaleDateString(undefined, { dateStyle: "medium" });

function dates(since: string | null, until: string | null): string | null {
  if (!since && !until) return null;
  const last = until ? new Date(new Date(until).getTime() - DAY_MS) : null; // `until` is exclusive
  if (since && last) {
    const first = day(new Date(since));
    return first === day(last) ? `On ${first}` : `${first} to ${day(last)}`;
  }
  return since ? `Since ${day(new Date(since))}` : `Before ${day(new Date(until!))}`;
}

/** What narrowed a search or an answer, as chips. Filters the question stated are marked, so a surprising result
 *  ("why only calendar events?") explains itself. Renders nothing when nothing was filtered. */
export default function AppliedFilters({ filters, scope }: { filters: Filters | undefined; scope: "search" | "answer" }) {
  if (!filters) return null;
  const asked = new Set<string>(filters.from_question);
  const chips: { label: string; fromQuestion: boolean }[] = [];
  if (filters.sources.length) {
    chips.push({ label: `In ${[...new Set(filters.sources.map(sourceLabel))].join(", ")}`, fromQuestion: asked.has("sources") });
  }
  if (filters.person) {
    chips.push({ label: `From or to ${filters.person.name ?? "a person"}`, fromQuestion: asked.has("person") });
  }
  if (filters.mentions) {
    chips.push({ label: `Mentioning ${filters.mentions.name ?? "a person"}`, fromQuestion: asked.has("mentions") });
  }
  const range = dates(filters.since, filters.until);
  if (range) chips.push({ label: range, fromQuestion: asked.has("since") || asked.has("until") });
  if (!chips.length) return null;
  return (
    <div className="mt-3 flex flex-wrap items-center gap-1.5 text-xs" role="note" aria-label={`Filters this ${scope} used`}>
      <span className="text-muted">{scope === "answer" ? "Answered from records" : "Showing records"}</span>
      {chips.map((c) => (
        <span key={c.label} title={c.fromQuestion ? "Read from your question" : "From the search options"}
          className={`rounded-full border px-2 py-0.5 ${c.fromQuestion ? "border-accent/50 bg-accent/10 text-ink" : "border-line text-soft"}`}>
          {c.label}{c.fromQuestion && <span className="sr-only"> (from your question)</span>}
        </span>
      ))}
      {chips.some((c) => c.fromQuestion) && <span className="text-muted">· highlighted: read from your question</span>}
    </div>
  );
}
