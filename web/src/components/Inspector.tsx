"use client";

import { useEffect, useState } from "react";
import EvidenceList from "@/components/EvidenceList";
import TimelineExplorer from "@/components/TimelineExplorer";
import { api, NodeDetail, TYPE_COLORS } from "@/lib/api";

type Props = { id: number | null; onExpandAction: (id: number) => void };

const LABELS: Record<string, string> = {
  SENT: "Sent", RECEIVED: "Received", MEMBER_OF: "Organizations", IN_THREAD: "In conversations",
  REPLIES_TO: "Replies", ATTENDED: "Events attended", MENTIONS: "Mentioned", ABOUT: "Topics", SAME_AS: "Other addresses",
};

export default function Inspector({ id, onExpandAction }: Props) {
  const [node, setNode] = useState<NodeDetail | null>(null);

  useEffect(() => {
    if (id === null) return setNode(null);
    let live = true;
    api.node(id).then((n) => live && setNode(n)).catch(() => live && setNode(null));
    return () => { live = false; };
  }, [id]);

  if (!node) return <div className="p-4 text-sm text-muted">Click a dot to see who or what it is, and what links it to the rest.</div>;

  const src = node.source_item;
  const meta = (src?.raw_meta ?? {}) as { from?: { email: string }[]; to?: { email: string }[] };

  return (
    <div className="space-y-4 p-4 text-sm">
      <div>
        <div className="flex items-center gap-2 text-xs text-muted">
          <span className="h-2 w-2 rounded-full" style={{ background: TYPE_COLORS[node.type] }} />
          {node.type}
        </div>
        <h2 className="mt-1 text-base font-semibold break-words">{node.name}</h2>
        {node.key !== "me" && <div className="mt-0.5 text-xs break-all text-muted">{node.key}</div>}
      </div>

      <div>
        <div className="mb-1 font-medium">Connections</div>
        <ul className="space-y-0.5">
          {Object.entries(node.degree).map(([t, n]) => (
            <li key={t} className="flex justify-between text-xs text-soft"><span>{LABELS[t] ?? t}</span><span>{n}</span></li>
          ))}
        </ul>
        <button onClick={() => onExpandAction(node.id)} className="mt-2 rounded-md border border-line px-2 py-1 text-xs hover:bg-hover">
          Bring in their connections
        </button>
      </div>

      <div>
        <div className="mb-1 font-medium">What the graph knows</div>
        <EvidenceList id={node.id} />
      </div>

      <TimelineExplorer a={node.id} />

      {src && (
        <div>
          <div className="mb-1 font-medium">Original</div>
          <div className="space-y-0.5 text-xs text-soft">
            {src.occurred_at && <div>{new Date(src.occurred_at).toLocaleString()}</div>}
            {meta.from?.[0] && <div>From: {meta.from[0].email}</div>}
            {meta.to && meta.to.length > 0 && <div className="break-words">To: {meta.to.map((a) => a.email).join(", ")}</div>}
          </div>
          <pre className="mt-2 max-h-96 overflow-y-auto rounded-md bg-bg p-2.5 text-xs leading-relaxed whitespace-pre-wrap text-soft">{src.body_text}</pre>
        </div>
      )}
    </div>
  );
}
