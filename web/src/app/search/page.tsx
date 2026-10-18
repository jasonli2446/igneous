"use client";

import { useState } from "react";
import { api, Hit } from "@/lib/api";

export default function SearchPage() {
  const [q, setQ] = useState("");
  const [person, setPerson] = useState("");
  const [mode, setMode] = useState("hybrid");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!q.trim()) return;
    setBusy(true);
    try {
      setHits((await api.search(q, { person: person || undefined, mode })).hits);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-3xl p-6">
      <form onSubmit={run} className="flex flex-wrap gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search everything…"
          className="min-w-0 flex-1 rounded border border-zinc-300 px-3 py-2 outline-none focus:border-zinc-500" />
        <input value={person} onChange={(e) => setPerson(e.target.value)} placeholder="person@email (optional)"
          className="w-56 rounded border border-zinc-300 px-3 py-2 text-sm outline-none focus:border-zinc-500" />
        <select value={mode} onChange={(e) => setMode(e.target.value)} className="rounded border border-zinc-300 px-2 text-sm">
          <option value="hybrid">Hybrid</option>
          <option value="lexical">Lexical only</option>
          <option value="dense">Dense only</option>
        </select>
        <button className="rounded bg-zinc-900 px-4 py-2 text-sm text-white disabled:opacity-50" disabled={busy}>
          {busy ? "…" : "Search"}
        </button>
      </form>

      {hits && hits.length === 0 && <p className="mt-6 text-sm text-zinc-500">No results.</p>}
      <ol className="mt-6 space-y-4">
        {hits?.map((h) => (
          <li key={h.chunk_id} className="rounded border border-zinc-200 p-3">
            <div className="flex items-baseline gap-2">
              <span className="font-medium">{h.title || "(no subject)"}</span>
              <span className="ml-auto shrink-0 text-xs text-zinc-400">
                {h.matched.join(" + ")} · {h.score.toFixed(4)}
              </span>
            </div>
            <div className="mt-0.5 text-xs text-zinc-500">
              {h.source} · {h.external_id}{h.occurred_at && ` · ${new Date(h.occurred_at).toLocaleDateString()}`}
            </div>
            <p className="mt-2 line-clamp-4 text-sm whitespace-pre-wrap text-zinc-700">{h.text}</p>
          </li>
        ))}
      </ol>
    </div>
  );
}
