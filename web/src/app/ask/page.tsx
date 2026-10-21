"use client";

import { Fragment, useState } from "react";
import { InlineError, Skeleton } from "@/components/ui";
import { api, AskResult, Hit } from "@/lib/api";

const EXAMPLES = [
  "Why was Ken Lay asked to donate his stock sale proceeds?",
  "What did people say about the California power crisis and price caps?",
  "What was Jeff Skilling's favorite pizza topping?",
];

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

export default function AskPage() {
  const [q, setQ] = useState("");
  const [useGraph, setUseGraph] = useState(true);
  const [res, setRes] = useState<AskResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [focus, setFocus] = useState<number | null>(null);

  const run = async (question: string) => {
    if (!question.trim()) return;
    setQ(question);
    setBusy(true);
    setError(null);
    setFocus(null);
    try {
      setRes(await api.ask(question, useGraph));
    } catch (e) {
      setError(String(e));
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
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask a question about the corpus…"
          className="min-w-0 flex-1 rounded-md border border-line bg-panel px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-accent" />
        <button className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-bg disabled:opacity-50" disabled={busy}>
          {busy ? "Thinking…" : "Ask"}
        </button>
      </form>
      <div className="mt-3 flex flex-col gap-1.5 text-xs text-muted">
        <label className="flex items-center gap-1.5">
          <input type="checkbox" className="accent-[var(--accent)]" checked={useGraph} onChange={(e) => setUseGraph(e.target.checked)} />
          Use the graph to find related context (turn off for plain retrieval)
        </label>
        {!res && !busy && (
          <ul className="mt-2 space-y-1">
            <li className="text-muted">Try one of these</li>
            {EXAMPLES.map((ex) => (
              <li key={ex}><button onClick={() => run(ex)} className="text-left text-soft hover:text-ink hover:underline">{ex}</button></li>
            ))}
          </ul>
        )}
      </div>

      {error && <div className="mt-6"><InlineError message={error} onRetry={() => run(q)} /></div>}

      {busy && (
        <div className="mt-6 space-y-2" role="status" aria-label="Thinking">
          <Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-11/12" /><Skeleton className="h-4 w-2/3" />
        </div>
      )}

      {res && !busy && (
        <>
          <div className={`mt-6 rounded-md border p-4 ${res.answerable ? "border-line bg-panel" : "border-danger/40"}`}>
            {!res.answerable && <div className="mb-1 text-xs font-medium text-danger">Not answerable from the evidence</div>}
            <p className="leading-relaxed">{withCitations(res.answer, cited, pick)}</p>
            <div className="mt-3 text-xs text-muted">
              {res.citations.length} cited of {res.evidence.length} evidence chunks · {res.timing.total_s ?? res.timing.retrieve_s}s
            </div>
          </div>

          <h3 className="mt-8 mb-2 text-sm font-medium">Evidence</h3>
          <ol className="space-y-3">
            {res.evidence.map((h: Hit) => (
              <li key={h.chunk_id} id={`ev-${h.chunk_id}`}
                className={`rounded-md border p-3 text-sm ${focus === h.chunk_id ? "border-accent ring-2 ring-accent/25" : cited.has(h.chunk_id) ? "border-accent/50" : "border-line opacity-70"}`}>
                <div className="flex items-baseline gap-2">
                  <span className="font-medium">{h.title || "(no subject)"}</span>
                  <span className="ml-auto shrink-0 text-xs text-muted">{h.matched.join(" + ")}</span>
                </div>
                <div className="mt-0.5 text-xs text-muted">
                  {sourceLabel(h.source)}{h.occurred_at && ` · ${new Date(h.occurred_at).toLocaleDateString()}`}
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

const SOURCE_LABELS: Record<string, string> = {
  gmail: "Email", email: "Email", imessage: "Messages", gcal: "Calendar", drive: "Drive", fs: "Files",
  ai_chats: "AI chat", apple_notes: "Notes", github: "GitHub", takeout_activity: "Google activity",
  takeout_chrome: "Browsing", takeout_meet: "Meet", takeout_chat: "Google Chat", takeout_tasks: "Tasks",
};

function sourceLabel(source: string) {
  return SOURCE_LABELS[source] ?? source.replaceAll("_", " ");
}
