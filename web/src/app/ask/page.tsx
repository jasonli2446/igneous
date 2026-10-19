"use client";

import { Fragment, useState } from "react";
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
      <button key={i} onClick={() => onPick(id)} className="mx-0.5 rounded bg-blue-50 px-1 font-mono text-xs text-blue-700 hover:bg-blue-100">
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
    <div className="mx-auto max-w-3xl p-6">
      <form onSubmit={(e) => { e.preventDefault(); run(q); }} className="flex gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask a question about the corpus…"
          className="min-w-0 flex-1 rounded border border-zinc-300 px-3 py-2 outline-none focus:border-zinc-500" />
        <button className="rounded bg-zinc-900 px-4 py-2 text-sm text-white disabled:opacity-50" disabled={busy}>
          {busy ? "Thinking…" : "Ask"}
        </button>
      </form>
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-zinc-500">
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={useGraph} onChange={(e) => setUseGraph(e.target.checked)} />
          Graph expansion (off = baseline)
        </label>
        {EXAMPLES.map((ex) => (
          <button key={ex} onClick={() => run(ex)} className="underline decoration-dotted hover:text-zinc-800">{ex}</button>
        ))}
      </div>

      {error && <p className="mt-6 text-sm text-red-600">{error}</p>}

      {res && (
        <>
          <div className={`mt-6 rounded border p-4 ${res.answerable ? "border-zinc-200" : "border-amber-200 bg-amber-50"}`}>
            {!res.answerable && <div className="mb-1 text-xs font-medium uppercase text-amber-700">Not answerable from the evidence</div>}
            <p className="leading-relaxed">{withCitations(res.answer, cited, pick)}</p>
            <div className="mt-3 text-xs text-zinc-400">
              {res.citations.length} cited of {res.evidence.length} evidence chunks · {res.timing.total_s ?? res.timing.retrieve_s}s
            </div>
          </div>

          <h3 className="mt-6 mb-2 text-sm font-medium text-zinc-700">Evidence</h3>
          <ol className="space-y-3">
            {res.evidence.map((h: Hit) => (
              <li key={h.chunk_id} id={`ev-${h.chunk_id}`}
                className={`rounded border p-3 text-sm ${focus === h.chunk_id ? "border-blue-400 ring-2 ring-blue-100" : cited.has(h.chunk_id) ? "border-blue-200" : "border-zinc-200 opacity-70"}`}>
                <div className="flex items-baseline gap-2">
                  <span className="font-mono text-xs text-zinc-400">c{h.chunk_id}</span>
                  <span className="font-medium">{h.title || "(no subject)"}</span>
                  <span className="ml-auto shrink-0 text-xs text-zinc-400">{h.matched.join(" + ")}</span>
                </div>
                <div className="mt-0.5 text-xs text-zinc-500">
                  {h.external_id}{h.occurred_at && ` · ${new Date(h.occurred_at).toLocaleDateString()}`}
                </div>
                <p className="mt-2 line-clamp-6 whitespace-pre-wrap text-zinc-700">{h.text}</p>
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  );
}
