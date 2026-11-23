"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useId, useState } from "react";
import AppliedFilters from "@/components/AppliedFilters";
import { Reader } from "@/components/SourceList";
import { InlineError, Skeleton } from "@/components/ui";
import { api, ApiError, type SearchMode } from "@/lib/api";
import { formatDay } from "@/lib/dates";
import { useRequest } from "@/lib/hooks";
import { useSourceLabel } from "@/lib/sources";

const field = "rounded-md border border-line bg-panel px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-accent";
const MODES: { mode: SearchMode; label: string }[] = [
  { mode: "hybrid", label: "Hybrid" }, { mode: "lexical", label: "Keywords only" }, { mode: "dense", label: "Meaning only" },
];

/** What the URL asks for: the query, who it is from or to, and how to match. */
type Asked = { q: string; person: string; mode: SearchMode };

function asked(params: { get(name: string): string | null }): Asked {
  const mode = MODES.find((m) => m.mode === params.get("mode"))?.mode ?? "hybrid";
  return { q: params.get("q")?.trim() ?? "", person: params.get("person")?.trim() ?? "", mode };
}

function href({ q, person, mode }: Asked) {
  const params = new URLSearchParams({ q });
  if (person) params.set("person", person);
  if (mode !== "hybrid") params.set("mode", mode);
  return `/search?${params}`;
}

/** The words to mark in the reader: the query's terms, without negated ones or quotes. */
const terms = (q: string) => q.split(/\s+/).filter((t) => t && !t.startsWith("-")).map((t) => t.replaceAll('"', ""));

/** The search box, filled from the URL; a search goes to the URL, so Back, a reload or a shared link show it again. */
function SearchForm({ initial, busy, onSearch }: { initial: Asked; busy: boolean; onSearch: (a: Asked) => void }) {
  const ids = useId();
  const [q, setQ] = useState(initial.q);
  const [person, setPerson] = useState(initial.person);
  const [mode, setMode] = useState<SearchMode>(initial.mode);
  return (
    <form onSubmit={(e) => { e.preventDefault(); if (q.trim()) onSearch({ q: q.trim(), person: person.trim(), mode }); }}
      className="mt-5 flex flex-wrap gap-2">
      <label htmlFor={`${ids}-q`} className="sr-only">Search for</label>
      <input id={`${ids}-q`} type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search everything…"
        className={`${field} min-w-0 flex-1`} />
      <label htmlFor={`${ids}-person`} className="sr-only">From or to a person</label>
      <input id={`${ids}-person`} value={person} onChange={(e) => setPerson(e.target.value)} placeholder="From or to (name or address)"
        className={`${field} w-full sm:w-60`} />
      <label htmlFor={`${ids}-mode`} className="sr-only">Search mode</label>
      <select id={`${ids}-mode`} value={mode} onChange={(e) => setMode(MODES.find((m) => m.mode === e.target.value)?.mode ?? "hybrid")}
        className={field}>
        {MODES.map((m) => <option key={m.mode} value={m.mode}>{m.label}</option>)}
      </select>
      <button type="submit" className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-bg disabled:opacity-50" disabled={busy}>
        {busy ? "Searching…" : "Search"}
      </button>
    </form>
  );
}

export default function SearchPage() {
  const router = useRouter();
  const now = asked(useSearchParams());
  const key = now.q ? href(now) : null;
  // Each search in the URL runs once; a newer one aborts it, and its answer never lands.
  const found = useRequest(key, async (signal) => {
    const opts = { person: now.person || undefined, mode: now.mode };
    const [s, e] = await Promise.all([api.search(now.q, opts, signal), api.experts(now.q, opts, signal)]);
    return { hits: s.hits, experts: e.experts, filters: s.filters };
  });
  const sourceLabel = useSourceLabel();
  const [reading, setReading] = useState<number | null>(null);
  const result = found.data;
  const error = found.error === undefined ? null
    : found.error instanceof ApiError ? found.error.detail : `Search failed. (${String(found.error)})`;

  return (
    <>
      <SearchForm key={key ?? ""} initial={now} busy={found.loading} onSearch={(a) => router.push(href(a))} />

      {!error && <AppliedFilters filters={result?.filters} scope="search" />}
      {error && <div className="mt-6"><InlineError message={error} onRetry={found.retry} /></div>}

      {found.loading && !result && (
        <div className="mt-6 space-y-3" role="status" aria-label="Searching">
          <Skeleton className="h-20 w-full" /><Skeleton className="h-20 w-full" /><Skeleton className="h-20 w-full" />
        </div>
      )}

      {result && (
        <div className={`transition-opacity ${found.loading ? "opacity-50" : ""}`}>
          {result.experts.length > 0 && (
            <section className="mt-8" aria-labelledby="people-heading">
              <h2 id="people-heading" className="text-sm font-medium">People involved</h2>
              <p className="mt-0.5 text-xs text-muted">Ranked by how much query-relevant material they wrote, were mentioned in, or attended. Open one in the graph.</p>
              <ol className="mt-3 divide-y divide-line border-y border-line">
                {result.experts.map((expert) => (
                  <li key={expert.id} className="py-3">
                    <div className="flex items-baseline gap-2">
                      <Link href={`/?seed=${expert.id}`} className="font-medium hover:underline">{expert.name}</Link>
                      <span className="ml-auto text-xs text-muted">{expert.evidence_count} item{expert.evidence_count === 1 ? "" : "s"}</span>
                    </div>
                    <ul className="mt-2 space-y-2">
                      {expert.evidence.map((evidence) => (
                        <li key={evidence.source_item_id} className="border-l-2 border-line pl-3 text-xs text-soft">
                          <button type="button" onClick={() => setReading(evidence.source_item_id)} className="text-left hover:text-ink hover:underline">
                            {evidence.title || "(no subject)"}
                          </button>
                          <span className="text-muted"> · {sourceLabel(evidence.source)} · {evidence.involvement.join(", ")}</span>
                          <p className="mt-0.5 line-clamp-2 whitespace-pre-wrap text-muted">{evidence.excerpt}</p>
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ol>
            </section>
          )}

          {result.hits.length === 0 && result.experts.length === 0 && (
            <p className="mt-8 text-sm text-muted">
              No results. Try different words or a different mode{result.filters.from_question.length ? ", or say less about who, where or when" : ""}.
            </p>
          )}

          {result.hits.length > 0 && (
            <section className="mt-8" aria-labelledby="results-heading">
              <h2 id="results-heading" className="text-sm font-medium">Results</h2>
              <ol className="mt-3 divide-y divide-line border-y border-line">
                {result.hits.map((h) => (
                  <li key={h.chunk_id} className="py-3">
                    <div className="flex items-baseline gap-2">
                      <button type="button" onClick={() => setReading(h.source_item_id)} className="text-left font-medium hover:underline">
                        {h.title || "(no subject)"}
                      </button>
                      <span className="ml-auto shrink-0 text-xs text-muted">{h.matched.join(" + ")}</span>
                    </div>
                    <div className="mt-0.5 text-xs text-muted">
                      {sourceLabel(h.source)}{h.occurred_at && ` · ${formatDay(h.occurred_at)}`}
                    </div>
                    <p className="mt-2 line-clamp-4 text-sm whitespace-pre-wrap text-soft">{h.text}</p>
                  </li>
                ))}
              </ol>
            </section>
          )}
        </div>
      )}
      {reading !== null && <Reader id={reading} highlight={terms(now.q)} onCloseAction={() => setReading(null)} />}
    </>
  );
}
