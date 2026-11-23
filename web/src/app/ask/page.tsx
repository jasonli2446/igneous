"use client";

import Link from "next/link";
import { Fragment, useEffect, useState } from "react";
import { sourceLabel } from "@/components/SourceList";
import { InlineError, Skeleton } from "@/components/ui";
import { api, ApiError, AskResult, Hit } from "@/lib/api";

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
      <button key={i} onClick={() => onPick(id)} className="mx-0.5 rounded bg-accent/15 px-1 font-mono text-xs text-accent hover:bg-accent/25">
        c{id}
      </button>
    ) : null; // cited something it was not shown: the server already dropped it from citations
  });
}

/** Why Ask failed, in words: the model isn't set up (503), it failed this time (502), or anything else. */
function AskFailure({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  if (error.status !== 502 && error.status !== 503) return <InlineError message={error.detail} onRetry={onRetry} />;
  const unavailable = error.status === 503;
  return (
    <div className="rounded-md border border-danger/40 p-4" role="alert">
      <div className="mb-1 text-xs font-medium text-danger">
        {unavailable ? "The hosted model isn't available" : "The model couldn't answer this time"}
      </div>
      <p className="break-words text-sm leading-relaxed text-soft">{error.detail}</p>
      <div className="mt-3 flex gap-4 text-sm font-medium">
        {unavailable
          ? <Link href="/settings#model" className="text-accent underline underline-offset-4">Open Sources</Link>
          : <button onClick={onRetry} className="text-accent underline underline-offset-4">Try again</button>}
      </div>
    </div>
  );
}

export default function AskPage() {
  const [q, setQ] = useState("");
  const [useGraph, setUseGraph] = useState(true);
  const [res, setRes] = useState<AskResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [focus, setFocus] = useState<number | null>(null);
  const [suggestions, setSuggestions] = useState<string[]>([]);

  useEffect(() => {
    api.askSuggestions().then(setSuggestions).catch(() => setSuggestions([]));
  }, []);

  const run = async (question: string) => {
    if (!question.trim()) return;
    setQ(question);
    setBusy(true);
    setError(null);
    setFocus(null);
    try {
      setRes(await api.ask(question, useGraph));
    } catch (e) {
      setRes(null);
      setError(e instanceof ApiError ? e : new ApiError(0, String(e)));
    } finally {
      setBusy(false);
    }
  };

  const cited = new Set(res?.citations.map((c) => c.chunk_id) ?? []);
  const pick = (id: number) => {
    setFocus(id);
    document.getElementById(`ev-${id}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-8 sm:px-6">
      <h1 className="text-xl font-semibold tracking-tight">Ask</h1>
      <p className="mt-1 text-sm text-muted">Ask a question in plain language. Answers cite the passages they came from.</p>
      <form onSubmit={(e) => { e.preventDefault(); run(q); }} className="mt-5 flex gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask a question about your records…" aria-label="Question"
          className="min-w-0 flex-1 rounded-md border border-line bg-panel px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-accent" />
        <button className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-bg disabled:opacity-50" disabled={busy}>
          {busy ? "Thinking…" : "Ask"}
        </button>
      </form>
      <div className="mt-3 flex flex-col gap-1.5 text-xs text-muted">
        <label className="flex items-center gap-1.5">
          <input type="checkbox" className="accent-[var(--accent)]" checked={useGraph} onChange={(e) => setUseGraph(e.target.checked)} />
          Follow the graph to replies and related messages, and rerank them (turn off to compare with plain search)
        </label>
        {!res && !busy && suggestions.length > 0 && (
          <ul className="mt-2 space-y-1">
            <li className="text-muted">Try one of these</li>
            {suggestions.map((ex) => (
              <li key={ex}><button onClick={() => run(ex)} className="text-left text-soft hover:text-ink hover:underline">{ex}</button></li>
            ))}
          </ul>
        )}
      </div>

      {error && <div className="mt-6"><AskFailure error={error} onRetry={() => run(q)} /></div>}

      {busy && (
        <div className="mt-6 space-y-2" role="status" aria-label="Thinking">
          <Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-11/12" /><Skeleton className="h-4 w-2/3" />
        </div>
      )}

      {res && !busy && res.reason === "no_opted_in_sources" && (
        <div className="mt-6 rounded-md border border-line bg-panel p-4" role="status">
          <div className="mb-1 text-xs font-medium text-muted">Nothing to ask yet</div>
          <p className="leading-relaxed text-soft">{res.answer}</p>
          <Link href="/settings#model" className="mt-3 inline-block text-sm font-medium text-accent underline underline-offset-4">
            Open Sources
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

          {res.evidence.length > 0 && <h3 className="mt-8 mb-2 text-sm font-medium">Evidence</h3>}
          <ol className="space-y-3">
            {res.evidence.map((h: Hit) => (
              <li key={h.chunk_id} id={`ev-${h.chunk_id}`}
                className={`rounded-md border p-3 text-sm ${focus === h.chunk_id ? "border-accent ring-2 ring-accent/25" : cited.has(h.chunk_id) ? "border-accent/50" : "border-line opacity-70"}`}>
                <div className="flex items-baseline gap-2">
                  <span className="font-medium">{h.title || "(no subject)"}</span>
                  <span className="ml-auto shrink-0 font-mono text-[11px] text-muted">c{h.chunk_id}</span>
                </div>
                <div className="mt-0.5 text-xs text-muted">
                  {sourceLabel(h.source)}{h.occurred_at && ` · ${new Date(h.occurred_at).toLocaleDateString()}`}
                  {" · "}found by {h.matched.join(" + ")}
                  {h.relation && h.via_chunk_id !== undefined && (
                    <> · {RELATION[h.relation]}{" "}
                      <button onClick={() => pick(h.via_chunk_id!)} className="font-mono text-accent hover:underline">c{h.via_chunk_id}</button>
                    </>
                  )}
                </div>
                <p className="mt-2 line-clamp-6 whitespace-pre-wrap text-soft">{h.text}</p>
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  );
}
