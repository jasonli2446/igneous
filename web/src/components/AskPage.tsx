"use client";

import Link from "next/link";
import { Fragment, useId, useRef, useState } from "react";
import AppliedFilters from "@/components/AppliedFilters";
import { InlineError, Skeleton } from "@/components/ui";
import { api, ApiError, type AskResult, type Hit } from "@/lib/api";
import { formatDay } from "@/lib/dates";
import { useRequest } from "@/lib/hooks";
import { useSourceLabel } from "@/lib/sources";

// How a graph neighbour was reached from the hit it follows.
const RELATION: Record<NonNullable<Hit["relation"]>, string> = {
  reply: "Reply in the chain of",
  thread: "Same thread as",
  correspondents: "Same people, same weeks as",
};

// Render "[c123]" markers in the answer as links to their evidence card.
function withCitations(answer: string, ids: Set<number>, onPick: (id: number) => void) {
  return answer.split(/(\[c\d+\])/g).map((part, i) => {
    const m = part.match(/^\[c(\d+)\]$/);
    if (!m) return <Fragment key={i}>{part}</Fragment>;
    const id = Number(m[1]);
    return ids.has(id) ? (
      <button type="button" key={i} onClick={() => onPick(id)}
        className="mx-0.5 rounded bg-accent/15 px-1 font-mono text-xs text-accent hover:bg-accent/25">
        c{id}
      </button>
    ) : null; // cited something it was not shown: the server already dropped it from citations
  });
}

/** Why the API sent nothing to the hosted model (its `answer` says how to change that), and where to change it. */
const NOT_ASKED: Record<NonNullable<AskResult["reason"]>, { title: string; link: string }> = {
  no_opted_in_sources: { title: "Nothing to ask yet", link: "Choose what the model may read" },
  model_not_set_up: { title: "The hosted model isn't set up", link: "Set up the hosted model" },
};

/** Why Ask failed, in words: the hosted model can't be used right now (503), it failed this time (502), or anything
 *  else. */
function AskFailure({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  if (error.status !== 502 && error.status !== 503) return <InlineError message={error.detail} onRetry={onRetry} />;
  return (
    <div className="rounded-md border border-danger/40 p-4" role="alert">
      <div className="mb-1 text-xs font-medium text-danger">
        {error.status === 503 ? "The hosted model can't be used right now" : "The model couldn't answer this time"}
      </div>
      <p className="break-words text-sm leading-relaxed text-soft">{error.detail}</p>
      <div className="mt-3 flex gap-4 text-sm font-medium">
        <button type="button" onClick={onRetry} className="text-accent underline underline-offset-4">Try again</button>
        {error.status === 503 && <Link href="/settings#model" className="text-accent underline underline-offset-4">Model settings</Link>}
      </div>
    </div>
  );
}

export default function AskPage() {
  const inputId = useId();
  const [q, setQ] = useState("");
  const [useGraph, setUseGraph] = useState(true);
  const [res, setRes] = useState<AskResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [focus, setFocus] = useState<number | null>(null);
  const request = useRef<AbortController | null>(null);
  const suggestions = useRequest("ask-suggestions", (signal) => api.askSuggestions(signal));
  const sourceLabel = useSourceLabel();

  const run = async (question: string) => {
    if (!question.trim()) return;
    request.current?.abort(); // a newer question replaces one still being answered
    const current = new AbortController();
    request.current = current;
    setQ(question);
    setBusy(true);
    setError(null);
    setFocus(null);
    try {
      // The API decides what may be asked: with nothing opted in, or no hosted model set up, it answers why.
      const answer = await api.ask(question, useGraph, current.signal);
      if (request.current === current) setRes(answer);
    } catch (e) {
      if (current.signal.aborted) return;
      setRes(null);
      setError(e instanceof ApiError ? e : new ApiError(0, String(e)));
    } finally {
      if (request.current === current) setBusy(false);
    }
  };

  const cited = new Set(res?.citations.map((c) => c.chunk_id) ?? []);
  const pick = (id: number) => {
    setFocus(id);
    document.getElementById(`ev-${id}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  };
  const starters = suggestions.data ?? [];

  return (
    <>
      <form onSubmit={(e) => { e.preventDefault(); void run(q); }} className="mt-5">
        <label htmlFor={inputId} className="text-sm font-medium text-ink">Your question</label>
        <div className="mt-1.5 flex gap-2">
          <input id={inputId} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask a question about your records…"
            className="min-w-0 flex-1 rounded-md border border-line bg-panel px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-accent" />
          <button type="submit" className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-bg disabled:opacity-50" disabled={busy}>
            {busy ? "Thinking…" : "Ask"}
          </button>
        </div>
      </form>
      <div className="mt-3 flex flex-col gap-1.5 text-xs text-muted">
        <label className="flex items-center gap-1.5">
          <input type="checkbox" className="accent-[var(--accent)]" checked={useGraph} onChange={(e) => setUseGraph(e.target.checked)} />
          Follow the graph to replies and related messages, and rerank them (turn off to compare with plain search)
        </label>
        {!res && !busy && starters.length > 0 && (
          <ul className="mt-2 space-y-1" aria-label="Questions to try">
            <li className="text-muted">Try one of these</li>
            {starters.map((ex) => (
              <li key={ex}>
                <button type="button" onClick={() => void run(ex)} className="text-left text-soft hover:text-ink hover:underline">{ex}</button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {res && !busy && <AppliedFilters filters={res.filters} scope="answer" />}
      {error && <div className="mt-6"><AskFailure error={error} onRetry={() => void run(q)} /></div>}

      {busy && (
        <div className="mt-6 space-y-2" role="status" aria-label="Thinking">
          <Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-11/12" /><Skeleton className="h-4 w-2/3" />
        </div>
      )}

      {res && !busy && res.reason && (
        <div className="mt-6 rounded-md border border-line bg-panel p-4" role="status">
          <div className="mb-1 text-xs font-medium text-muted">{NOT_ASKED[res.reason].title}</div>
          <p className="leading-relaxed text-soft">{res.answer}</p>
          <Link href="/settings#model" className="mt-3 inline-block text-sm font-medium text-accent underline underline-offset-4">
            {NOT_ASKED[res.reason].link}
          </Link>
        </div>
      )}

      {res && !busy && !res.reason && (
        <>
          <div className={`mt-6 rounded-md border p-4 ${res.answerable ? "border-line bg-panel" : "border-danger/40"}`}>
            {!res.answerable && <div className="mb-1 text-xs font-medium text-danger">Not answerable from the evidence</div>}
            <p className="leading-relaxed">{withCitations(res.answer, cited, pick)}</p>
            <div className="mt-3 text-xs text-muted">
              {res.citations.length} cited of {res.evidence.length} evidence passages · {res.timing.total_s ?? res.timing.retrieve_s}s
            </div>
          </div>

          {res.evidence.length > 0 && <h2 className="mt-8 mb-2 text-sm font-medium">Evidence</h2>}
          <ol className="space-y-3">
            {res.evidence.map((h) => (
              <li key={h.chunk_id} id={`ev-${h.chunk_id}`}
                className={`rounded-md border p-3 text-sm ${focus === h.chunk_id ? "border-accent ring-2 ring-accent/25" : cited.has(h.chunk_id) ? "border-accent/50" : "border-line opacity-70"}`}>
                <div className="flex items-baseline gap-2">
                  <span className="font-medium">{h.title || "(no subject)"}</span>
                  <span className="ml-auto shrink-0 font-mono text-[11px] text-muted">c{h.chunk_id}</span>
                </div>
                <div className="mt-0.5 text-xs text-muted">
                  {sourceLabel(h.source)}{h.occurred_at && ` · ${formatDay(h.occurred_at)}`}
                  {" · "}found by {h.matched.join(" + ")}
                  {h.relation && h.via_chunk_id !== undefined && (
                    <> · {RELATION[h.relation]}{" "}
                      <button type="button" onClick={() => pick(h.via_chunk_id!)} className="font-mono text-accent hover:underline">c{h.via_chunk_id}</button>
                    </>
                  )}
                </div>
                <p className="mt-2 line-clamp-6 whitespace-pre-wrap text-soft">{h.text}</p>
              </li>
            ))}
          </ol>
        </>
      )}
    </>
  );
}
