"use client";

import { useEffect, useState } from "react";
import EvidenceList from "@/components/EvidenceList";
import { api, NodeDetail, TYPE_COLORS } from "@/lib/api";

type Props = { id: number | null; onExpandAction: (id: number) => void };

export default function Inspector({ id, onExpandAction }: Props) {
  const [node, setNode] = useState<NodeDetail | null>(null);

  useEffect(() => {
    if (id === null) return setNode(null);
    let live = true;
    api.node(id).then((n) => live && setNode(n)).catch(() => live && setNode(null));
    return () => { live = false; };
  }, [id]);

  if (!node) return <div className="p-4 text-sm text-zinc-500">Nothing selected.</div>;

  const src = node.source_item;
  const meta = (src?.raw_meta ?? {}) as { from?: { email: string }[]; to?: { email: string }[] };

  return (
    <div className="space-y-4 p-4 text-sm">
      <div>
        <div className="flex items-center gap-2 text-xs uppercase tracking-wide text-zinc-500">
          <span className="h-2 w-2 rounded-full" style={{ background: TYPE_COLORS[node.type] }} />
          {node.type}
        </div>
        <h2 className="mt-1 text-base font-semibold break-words">{node.name}</h2>
        <div className="mt-0.5 font-mono text-xs break-all text-zinc-500">{node.key}</div>
      </div>

      <div>
        <div className="mb-1 font-medium text-zinc-700">Connections</div>
        <ul className="space-y-0.5">
          {Object.entries(node.degree).map(([t, n]) => (
            <li key={t} className="flex justify-between font-mono text-xs"><span>{t}</span><span>{n}</span></li>
          ))}
        </ul>
        <button onClick={() => onExpandAction(node.id)} className="mt-2 rounded border border-zinc-300 px-2 py-1 text-xs hover:bg-zinc-50">
          Expand neighbors
        </button>
      </div>

      <div>
        <div className="mb-1 font-medium text-zinc-700">Evidence</div>
        <EvidenceList id={node.id} />
      </div>

      {src && (
        <div>
          <div className="mb-1 font-medium text-zinc-700">Source</div>
          <div className="space-y-0.5 text-xs text-zinc-600">
            {src.occurred_at && <div>{new Date(src.occurred_at).toLocaleString()}</div>}
            {meta.from?.[0] && <div>From: {meta.from[0].email}</div>}
            {meta.to && meta.to.length > 0 && <div className="break-words">To: {meta.to.map((a) => a.email).join(", ")}</div>}
          </div>
          <pre className="mt-2 max-h-96 overflow-y-auto rounded bg-zinc-50 p-2 text-xs whitespace-pre-wrap">{src.body_text}</pre>
        </div>
      )}
    </div>
  );
}
