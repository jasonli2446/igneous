"use client";

import { useState } from "react";
import { InlineError } from "@/components/ui";
import { api, type GEdge, type GNode, type Thing } from "@/lib/api";
import { useLiveSearch, useRequest } from "@/lib/hooks";

type Option = { label: string; type: string; src: number; dst: number };

/** "+ Connect to…": tell the graph about a link it missed. Pick the other thing, say how they're connected in
 *  plain words, optionally why. Stored as yours, so the AI never second-guesses or drops it. */
export default function ConnectTo({ id, name, onLinkedAction }: {
  id: number; name: string; onLinkedAction: (node: GNode, edge: GEdge) => void;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [other, setOther] = useState<Thing | null>(null);
  const [choice, setChoice] = useState<Option | null>(null);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const hits = useLiveSearch(open && !other ? q : "",
    (text, signal) => api.findThings(text, {}, signal).then((r) => r.results.filter((n) => n.id !== id).slice(0, 8)));
  // The kinds of link the API allows between these two; the first is picked until you pick another.
  const options = useRequest(other ? `${id}-${other.id}` : null, (signal) => api.linkOptions(id, other!.id, signal));
  const offered: Option[] = options.data?.options ?? [];
  const chosen = choice && offered.includes(choice) ? choice : offered[0] ?? null;

  const reset = () => { setOpen(false); setQ(""); setOther(null); setChoice(null); setNote(""); };

  if (!open) {
    return (
      <div>
        <button type="button" onClick={() => { setOpen(true); setDone(null); setError(null); }} className="btn">+ Connect to…</button>
        {done && <p role="status" className="mt-1 text-xs text-muted">{done}</p>}
      </div>
    );
  }

  const save = () => {
    if (!other || !chosen || saving) return;
    setSaving(true);
    setError(null);
    api.addLink(chosen.src, chosen.dst, chosen.type, note.trim() || undefined).then((e) => {
      onLinkedAction({ id: other.id, type: other.type, name: other.name, key: "", category: other.category },
        { id: e.id, src: e.src, dst: e.dst, type: e.type, origin: "user", stated: true });
      setDone(`Connected "${name}" to "${other.name}".`);
      reset();
    }).catch((err) => setError(`Couldn't connect them. (${String(err)})`)).finally(() => setSaving(false));
  };

  return (
    <div className="space-y-2 rounded-md border border-line p-2 text-xs">
      {!other ? (
        <>
          <div className="flex items-center gap-2">
            <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find a person, project, place…"
              aria-label="Search for the thing to connect" onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); reset(); } }}
              className="field flex-1" />
            <button type="button" onClick={reset} className="text-muted hover:text-ink">Cancel</button>
          </div>
          {hits.active && (
            <ul className="space-y-0.5">
              {hits.results?.map((n) => (
                <li key={n.id}>
                  <button type="button" onClick={() => setOther(n)} className="btn-option">
                    <span className="truncate">{n.name}</span><span className="shrink-0 text-muted">{n.type}</span>
                  </button>
                </li>
              ))}
              {hits.results?.length === 0 && <li className="px-1.5 py-1 text-muted">{hits.failed ? "Search failed." : "Nothing matches."}</li>}
              {hits.results === null && <li className="px-1.5 py-1 text-muted">Searching…</li>}
            </ul>
          )}
        </>
      ) : (
        <>
          <p>How is <span className="font-medium">{name}</span> connected to <span className="font-medium">{other.name}</span>?</p>
          {options.error !== undefined && <InlineError message={`Couldn't load the kinds of link. (${String(options.error)})`} onRetry={options.retry} />}
          {options.loading && <p className="text-muted">Loading…</p>}
          <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Kind of connection">
            {offered.map((o) => (
              <button type="button" key={`${o.type}-${o.src}`} role="radio" aria-checked={chosen === o} onClick={() => setChoice(o)}
                className={`rounded-full border px-2 py-0.5 ${chosen === o ? "border-accent bg-accent/10" : "border-line hover:bg-hover"}`}>
                {o.label}
              </button>
            ))}
          </div>
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Why (optional, shown as the reason)"
            aria-label="Why they are connected" onKeyDown={(e) => { if (e.key === "Enter") save(); }}
            className="field w-full" />
          {error && <InlineError message={error} />}
          <div className="flex justify-end gap-2">
            <button type="button" onClick={() => setOther(null)} className="text-muted hover:text-ink">Back</button>
            <button type="button" onClick={save} disabled={!chosen || saving}
              className="rounded-md border border-accent/60 bg-accent/10 px-2.5 py-1 hover:bg-accent/20 disabled:opacity-50">Connect</button>
          </div>
        </>
      )}
    </div>
  );
}
