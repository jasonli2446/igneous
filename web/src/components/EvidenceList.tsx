"use client";

import { useEffect, useState } from "react";
import { api, Evidence, TYPE_COLORS } from "@/lib/api";

// "Why do you believe this": every model-derived edge on a node, with its source span highlighted.
export default function EvidenceList({ id }: { id: number }) {
  const [items, setItems] = useState<Evidence[] | null>(null);

  useEffect(() => {
    let live = true;
    setItems(null);
    api.evidence(id).then((r) => live && setItems(r.evidence)).catch(() => live && setItems([]));
    return () => { live = false; };
  }, [id]);

  if (items === null) return <p className="text-xs text-zinc-400">Loading evidence…</p>;
  if (items.length === 0) return <p className="text-xs text-zinc-500">No model-derived facts yet.</p>;

  return (
    <ul className="space-y-3">
      {items.map((e) => (
        <li key={e.id} className="rounded border border-zinc-200 p-2 text-xs">
          <div className="flex items-center gap-1.5">
            <span className="font-mono text-zinc-500">{e.type}</span>
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: TYPE_COLORS[e.other_type] }} />
            <span className="truncate font-medium">{e.other_name}</span>
            {e.confidence !== null && <span className="ml-auto text-zinc-400">{e.confidence.toFixed(2)}</span>}
          </div>
          {e.props.role && <div className="mt-0.5 text-zinc-500">{e.props.role}</div>}
          {e.type !== "ABOUT" && (
            <p className="mt-1.5 rounded bg-zinc-50 p-1.5 leading-relaxed whitespace-pre-wrap text-zinc-600">
              …{e.window.slice(Math.max(0, e.hl_start - 120), e.hl_start)}
              <mark className="rounded bg-yellow-200 px-0.5 text-zinc-900">{e.window.slice(e.hl_start, e.hl_end)}</mark>
              {e.window.slice(e.hl_end, e.hl_end + 120)}…
            </p>
          )}
          <div className="mt-1 truncate text-zinc-400">
            {e.title || "(no subject)"}{e.occurred_at && ` · ${new Date(e.occurred_at).toLocaleDateString()}`} · {e.model?.replace("us.anthropic.", "")} v{e.extractor_version}
          </div>
        </li>
      ))}
    </ul>
  );
}
