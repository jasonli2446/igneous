"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import AppliedFilters from "@/components/AppliedFilters";
import { Reader, sourceLabel } from "@/components/SourceList";
import { InlineError, Skeleton } from "@/components/ui";
import { api, ApiError, AppliedFilters as Filters, Expert, Hit } from "@/lib/api";

const field = "rounded-md border border-line bg-panel px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-accent";

/** The words to mark in the reader: the query's terms, without negated ones or quotes. */
const terms = (q: string) => q.split(/\s+/).filter((t) => t && !t.startsWith("-")).map((t) => t.replaceAll('"', ""));

export default function SearchPage() {
  const [q, setQ] = useState("");
  const [person, setPerson] = useState("");
  const [mode, setMode] = useState("hybrid");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [experts, setExperts] = useState<Expert[] | null>(null);
  const [filters, setFilters] = useState<Filters | undefined>(undefined);
  const [searched, setSearched] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reading, setReading] = useState<number | null>(null);
  const request = useRef<AbortController | null>(null);

  const run = async (e?: React.FormEvent, text = q) => {
    e?.preventDefault();
    if (!text.trim()) return;
    request.current?.abort(); // a newer search replaces one still running
    const current = new AbortController();
    request.current = current;
    setBusy(true);
    setError(null);
    try {
      const opts = { person: person || undefined, mode };
      const [searchResult, expertResult] = await Promise.all([
        api.search(text, opts, current.signal),
        api.experts(text, opts, current.signal),
      ]);
      setHits(searchResult.hits);
      setExperts(expertResult.experts);
      setFilters(searchResult.filters);
      setSearched(text);
    } catch (err) {
      if (current.signal.aborted) return;
      setError(err instanceof ApiError ? err.detail : `Search failed. (${String(err)})`);
    } finally {
      if (request.current === current) setBusy(false);
    }
  };

  // The explorer's "Search messages for ..." option lands here with ?q=.
  useEffect(() => {
    const initial = new URLSearchParams(window.location.search).get("q");
    if (initial) { setQ(initial); void run(undefined, initial); }
    return () => request.current?.abort();
  }, []);

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-8 sm:px-6">
      <h1 className="text-xl font-semibold tracking-tight">Search</h1>
      <p className="mt-1 text-sm text-muted">
        Find messages, events, and people across everything you have imported. Put a minus before a word to leave it
        out (<span className="font-mono">gas -prices</span>); a question can narrow itself (&ldquo;from Jeff Skilling in May
        2001&rdquo;, &ldquo;in my calendar&rdquo;).
      </p>

      <form onSubmit={run} className="mt-5 flex flex-wrap gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search everything…" aria-label="Search query"
          className={`${field} min-w-0 flex-1`} />
        <input value={person} onChange={(e) => setPerson(e.target.value)} placeholder="From or to (name or address)"
          aria-label="From or to a person" className={`${field} w-full sm:w-60`} />
        <select value={mode} onChange={(e) => setMode(e.target.value)} aria-label="Search mode" className={field}>
          <option value="hybrid">Hybrid</option>
          <option value="lexical">Keywords only</option>
          <option value="dense">Meaning only</option>
        </select>
        <button className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-bg disabled:opacity-50" disabled={busy}>
          {busy ? "Searching…" : "Search"}
        </button>
      </form>

      {!error && <AppliedFilters filters={filters} scope="search" />}
      {error && <div className="mt-6"><InlineError message={error} onRetry={() => run()} /></div>}

      {busy && hits === null && (
        <div className="mt-6 space-y-3" role="status" aria-label="Searching">
          <Skeleton className="h-20 w-full" /><Skeleton className="h-20 w-full" /><Skeleton className="h-20 w-full" />
        </div>
      )}

      <div className={`transition-opacity ${busy && hits !== null ? "opacity-50" : ""}`}>
        {experts && experts.length > 0 && (
          <section className="mt-8" aria-labelledby="people-heading">
            <h2 id="people-heading" className="text-sm font-medium">People involved</h2>
            <p className="mt-0.5 text-xs text-muted">Ranked by how much query-relevant material they wrote, were mentioned in, or attended. Open one in the graph.</p>
            <ol className="mt-3 divide-y divide-line border-y border-line">
              {experts.map((expert) => (
                <li key={expert.id} className="py-3">
                  <div className="flex items-baseline gap-2">
                    <Link href={`/?seed=${expert.id}`} className="font-medium hover:underline">{expert.name}</Link>
                    <span className="ml-auto text-xs text-muted">{expert.evidence_count} item{expert.evidence_count === 1 ? "" : "s"}</span>
                  </div>
                  <ul className="mt-2 space-y-2">
                    {expert.evidence.map((evidence) => (
                      <li key={evidence.source_item_id} className="border-l-2 border-line pl-3 text-xs text-soft">
                        <button onClick={() => setReading(evidence.source_item_id)} className="text-left hover:text-ink hover:underline">
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

        {hits && hits.length === 0 && experts?.length === 0 && (
          <p className="mt-8 text-sm text-muted">
            No results. Try different words or a different mode{filters?.from_question.length ? ", or say less about who, where or when" : ""}.
          </p>
        )}

        {hits && hits.length > 0 && (
          <section className="mt-8" aria-labelledby="results-heading">
            <h2 id="results-heading" className="text-sm font-medium">Results</h2>
            <ol className="mt-3 divide-y divide-line border-y border-line">
              {hits.map((h) => (
                <li key={h.chunk_id} className="py-3">
                  <div className="flex items-baseline gap-2">
                    <button onClick={() => setReading(h.source_item_id)} className="text-left font-medium hover:underline">
                      {h.title || "(no subject)"}
                    </button>
                    <span className="ml-auto shrink-0 text-xs text-muted">{h.matched.join(" + ")}</span>
                  </div>
                  <div className="mt-0.5 text-xs text-muted">
                    {sourceLabel(h.source)}{h.occurred_at && ` · ${new Date(h.occurred_at).toLocaleDateString(undefined, { dateStyle: "medium" })}`}
                  </div>
                  <p className="mt-2 line-clamp-4 text-sm whitespace-pre-wrap text-soft">{h.text}</p>
                </li>
              ))}
            </ol>
          </section>
        )}
      </div>
      {reading !== null && <Reader id={reading} highlight={terms(searched)} onCloseAction={() => setReading(null)} />}
    </div>
  );
}
