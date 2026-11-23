"use client";

import { useState } from "react";
import ConnectTo from "@/components/ConnectTo";
import EntityInspector from "@/components/EntityInspector";
import NodeSummary from "@/components/NodeSummary";
import Sources from "@/components/SourceList";
import { InlineError, Section, Skeleton } from "@/components/ui";
import { api, type GEdge, type GNode, type Thing } from "@/lib/api";
import { formatDayTime } from "@/lib/dates";
import { nodeColor, typeLabel } from "@/lib/graph-style";
import { useLiveSearch, useRequest } from "@/lib/hooks";

/** A correction made in a panel, for the page to announce and show: what changed, how to undo it (when it can be
 *  undone), and the node to stay on when the correction moved it (a merge). */
export type Corrected = { text: string; undo?: () => Promise<unknown>; keep?: number };

type Props = {
  id: number;
  /** Bumped after every correction: the panel loads again to show its result. */
  revision: number;
  onExpandAction: (id: number) => void | Promise<void>;
  onCloseAction: () => void;
  /** Whether this node's connections are currently shown on the graph (so the button says Show or Hide). */
  expanded: boolean;
  onSelectAction: (id: number) => void;
  onWhyAction: (a: number, b: number) => void;
  onLinkedAction: (node: GNode, edge: GEdge) => void;
  onCorrectedAction: (c: Corrected) => void;
};

function InspectorSkeleton() {
  return (
    <div className="space-y-5 p-4" role="status" aria-label="Loading details">
      <div className="space-y-2">
        <Skeleton className="h-3 w-16" />
        <Skeleton className="h-5 w-3/4" />
        <Skeleton className="h-3 w-1/2" />
      </div>
      <div className="space-y-2 border-t border-line pt-4">
        <Skeleton className="h-3 w-full" /><Skeleton className="h-3 w-5/6" /><Skeleton className="h-3 w-2/3" />
      </div>
      <Skeleton className="h-20 w-full" />
    </div>
  );
}

export default function Inspector({ id, revision, onExpandAction, onCloseAction, onSelectAction, onWhyAction,
  onCorrectedAction, onLinkedAction, expanded }: Props) {
  const { data: node, error, retry } = useRequest(id, (signal) => api.node(id, signal), revision);
  const [busy, setBusy] = useState(false);
  const [fullText, setFullText] = useState(false);
  const [showOriginal, setShowOriginal] = useState(false);

  if (error && !node) return <div className="p-4"><InlineError message={`Couldn't load details. (${String(error)})`} onRetry={retry} /></div>;
  if (!node) return <InspectorSkeleton />;
  if (node.type === "Project") {
    return <EntityInspector id={node.id} revision={revision} onSelectAction={onSelectAction} onCloseAction={onCloseAction}
      onCorrectedAction={onCorrectedAction} onLinkedAction={onLinkedAction} />;
  }

  const src = node.source_item;
  const meta = (src?.meta ?? {}) as { from?: { email: string }[]; to?: { email: string }[] };
  const body = src?.text ?? "";
  const long = body.length > 600;

  return (
    <div className="space-y-4 p-4 text-sm">
      <header>
        <div className="flex items-center gap-2 text-xs text-muted">
          <span aria-hidden className="h-2 w-2 rounded-full" style={{ background: nodeColor(node) }} />
          {typeLabel(node.type, node.props as { group?: boolean } | undefined)}
          <button type="button" onClick={onCloseAction} aria-label="Close details" className="btn-close ml-auto">×</button>
        </div>
        <h2 className="mt-1 text-lg font-semibold leading-snug break-words">{node.name}</h2>
        {/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(node.key) && node.key.toLowerCase() !== node.name.toLowerCase() && (
          <div className="mt-0.5 text-xs break-all text-muted">{node.key}</div>)}
      </header>

      <NodeSummary id={node.id} revision={revision} onSelectAction={onSelectAction} onWhyAction={onWhyAction} />

      <button type="button" disabled={busy} aria-pressed={expanded}
        onClick={async () => { setBusy(true); try { await onExpandAction(node.id); } finally { setBusy(false); } }}
        className={`btn inline-flex items-center gap-2 ${expanded ? "border-accent/50 bg-accent/10 hover:bg-accent/15" : ""}`}>
        {busy && <span aria-hidden className="h-3 w-3 animate-spin rounded-full border-2 border-accent border-t-transparent" />}
        {busy ? (expanded ? "Hiding…" : "Finding connections…")
          : expanded ? "Hide their connections" : "Show their connections on the graph"}
      </button>

      <ConnectTo id={node.id} name={node.name} onLinkedAction={onLinkedAction} />

      {node.type === "Person" && <SamePerson id={node.id} onMergedAction={onCorrectedAction} />}

      <Section title="Sources">
        <Sources id={node.id} revision={revision} highlight={[node.name]} />
      </Section>

      {src && (
        <Section title="Original">
          <button type="button" onClick={() => setShowOriginal((v) => !v)} aria-expanded={showOriginal} className="btn-link">
            {showOriginal ? "Hide original" : "Show original"}
          </button>
          {showOriginal && <>
            <div className="space-y-0.5 text-xs text-soft">
              {src.occurred_at && <div>{formatDayTime(src.occurred_at)}</div>}
              {meta.from?.[0] && <div className="break-words">From {meta.from[0].email}</div>}
              {meta.to && meta.to.length > 0 && <div className="break-words">To {meta.to.map((a) => a.email).join(", ")}</div>}
            </div>
            <pre className={`mt-2 overflow-y-auto rounded-md bg-bg p-2.5 text-xs leading-relaxed whitespace-pre-wrap text-soft ${fullText ? "max-h-96" : "max-h-32 overflow-hidden"}`}>{body}</pre>
            {long && (
              <button type="button" onClick={() => setFullText((v) => !v)} className="btn-link mt-1.5">
                {fullText ? "Show less" : "Show full text"}
              </button>
            )}
          </>}
        </Section>
      )}
    </div>
  );
}

/** "Same person as…": fold another identity (a nickname contact, an old address) into this person, for good. */
function SamePerson({ id, onMergedAction }: { id: number; onMergedAction: (c: Corrected) => void }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [error, setError] = useState<string | null>(null);
  const people = useLiveSearch(open ? q : "",
    (text, signal) => api.findThings(text, { types: ["Person"] }, signal).then((r) => r.results.filter((n) => n.id !== id)));

  if (!open) {
    return (
      <div>
        <button type="button" onClick={() => { setOpen(true); setError(null); }} className="btn">Same person as…</button>
        {error && <div className="mt-1"><InlineError message={error} /></div>}
      </div>
    );
  }
  const merge = (other: Thing) => api.mergePeople(id, other.id).then((r) => {
    setOpen(false);
    setQ("");
    onMergedAction({ text: `Merged "${other.name}" into this person.`, keep: r.person });
  }).catch((e) => { setOpen(false); setError(`Couldn't merge them. (${String(e)})`); });

  return (
    <div className="rounded-md border border-line p-2">
      <div className="flex items-center gap-2">
        <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find the other name or address"
          aria-label="Search people to merge" onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); setOpen(false); } }}
          className="field flex-1" />
        <button type="button" onClick={() => setOpen(false)} className="text-xs text-muted hover:text-ink">Cancel</button>
      </div>
      {people.active && (
        <ul className="mt-2 space-y-0.5">
          {people.results?.map((n) => (
            <li key={n.id}>
              <button type="button" className="btn-option" onClick={() => void merge(n)}>
                <span className="truncate">{n.name}</span><span className="shrink-0 text-muted">{n.context}</span>
              </button>
            </li>
          ))}
          {people.results?.length === 0 && (
            <li className="px-1.5 py-1 text-xs text-muted">{people.failed ? "Search failed." : "No one else matches."}</li>
          )}
          {people.results === null && <li className="px-1.5 py-1 text-xs text-muted">Searching…</li>}
        </ul>
      )}
    </div>
  );
}
