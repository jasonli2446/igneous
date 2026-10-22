"use client";

import { useEffect, useState } from "react";
import { api, type GEdge, type GNode } from "@/lib/api";

type Option = { label: string; type: string; src: number; dst: number };

/** "+ Connect to…": tell the graph about a link it missed. Pick the other thing, say how they're connected in
 *  plain words, optionally why. Stored as yours, so the AI never second-guesses or drops it. */
export default function ConnectTo({ id, name, onLinkedAction }: {
  id: number; name: string; onLinkedAction?: (node: GNode, edge: GEdge) => void;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<GNode[]>([]);
  const [other, setOther] = useState<GNode | null>(null);
  const [options, setOptions] = useState<Option[]>([]);
  const [choice, setChoice] = useState<Option | null>(null);
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    if (other || q.trim().length < 2) return setHits([]);
    const t = setTimeout(() => {
      api.findNodes(q).then((r) => setHits(r.nodes.filter((n) => n.id !== id).slice(0, 8))).catch(() => {});
    }, 200);
    return () => clearTimeout(t);
  }, [q, id, other]);

  useEffect(() => {
    if (!other) return setOptions([]);
    api.linkOptions(id, other.id).then((r) => { setOptions(r.options); setChoice(r.options[0] ?? null); })
      .catch((e) => setMsg(String(e)));
  }, [id, other]);

  const reset = () => { setOpen(false); setQ(""); setOther(null); setChoice(null); setNote(""); };

  if (!open) {
    return (
      <div>
        <button onClick={() => { setOpen(true); setMsg(null); }}
          className="rounded-md border border-line px-2.5 py-1 text-xs hover:bg-hover">+ Connect to…</button>
        {msg && <p className="mt-1 text-xs text-muted">{msg}</p>}
      </div>
    );
  }

  const save = () => {
    if (!other || !choice) return;
    api.addLink(choice.src, choice.dst, choice.type, note.trim() || undefined).then((e) => {
      onLinkedAction?.(other, { id: e.id, src: e.src, dst: e.dst, type: e.type, extractor: "user", confidence: 1 });
      setMsg(`Connected "${name}" to "${other.name}".`);
      reset();
    }).catch((err) => setMsg(String(err)));
  };

  return (
    <div className="space-y-2 rounded-md border border-line p-2 text-xs">
      {!other ? (
        <>
          <div className="flex items-center gap-2">
            <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find a person, project, place…"
              aria-label="Search for the thing to connect" onKeyDown={(e) => e.key === "Escape" && reset()}
              className="min-w-0 flex-1 rounded border border-line bg-bg px-2 py-1 outline-none focus:border-accent" />
            <button onClick={reset} className="text-muted hover:text-ink">Cancel</button>
          </div>
          {q.trim().length >= 2 && (
            <ul className="space-y-0.5">
              {hits.map((n) => (
                <li key={n.id}>
                  <button onClick={() => setOther(n)}
                    className="flex w-full justify-between gap-2 rounded px-1.5 py-1 text-left hover:bg-hover">
                    <span className="truncate">{n.name}</span><span className="shrink-0 text-muted">{n.type}</span>
                  </button>
                </li>
              ))}
              {hits.length === 0 && <li className="px-1.5 py-1 text-muted">Nothing matches.</li>}
            </ul>
          )}
        </>
      ) : (
        <>
          <p>How is <span className="font-medium">{name}</span> connected to <span className="font-medium">{other.name}</span>?</p>
          <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Kind of connection">
            {options.map((o) => (
              <button key={`${o.type}-${o.src}`} role="radio" aria-checked={choice === o} onClick={() => setChoice(o)}
                className={`rounded-full border px-2 py-0.5 ${choice === o ? "border-accent bg-accent/10" : "border-line hover:bg-hover"}`}>
                {o.label}
              </button>
            ))}
          </div>
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Why (optional, shown as the reason)"
            aria-label="Why they are connected" onKeyDown={(e) => e.key === "Enter" && save()}
            className="w-full rounded border border-line bg-bg px-2 py-1 outline-none focus:border-accent" />
          <div className="flex justify-end gap-2">
            <button onClick={() => setOther(null)} className="text-muted hover:text-ink">Back</button>
            <button onClick={save} disabled={!choice}
              className="rounded-md border border-accent/60 bg-accent/10 px-2.5 py-1 hover:bg-accent/20 disabled:opacity-50">Connect</button>
          </div>
        </>
      )}
    </div>
  );
}
