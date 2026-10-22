"use client";

import { useEffect, useState } from "react";
import EntityInspector from "@/components/EntityInspector";
import NodeSummary from "@/components/NodeSummary";
import Sources from "@/components/SourceList";
import { InlineError, Skeleton } from "@/components/ui";
import { typeLabel } from "@/lib/graph-style";
import { api, NodeDetail, TYPE_COLORS } from "@/lib/api";

type Props = {
  id: number | null; onExpandAction: (id: number) => void | Promise<void>; onCloseAction?: () => void;
  /** Whether this node's connections are currently shown on the graph (so the button says Show or Hide). */
  expanded?: boolean;
  // Projects are editable: selecting one of their members, and re-fetching the graph after a correction.
  onSelectAction?: (id: number) => void; onWhyAction?: (a: number, b: number) => void; onChangedAction?: (keep: number | null) => void;
};

const when = (iso: string) =>
  new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="border-t border-line pt-4">
      <h3 className="mb-2 text-xs font-medium text-muted">{title}</h3>
      {children}
    </section>
  );
}

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

export default function Inspector({ id, onExpandAction, onCloseAction, onSelectAction, onWhyAction, onChangedAction, expanded }: Props) {
  const [busy, setBusy] = useState(false);
  const [node, setNode] = useState<NodeDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [fullText, setFullText] = useState(false);
  const [showOriginal, setShowOriginal] = useState(false);

  useEffect(() => {
    if (id === null) return setNode(null);
    let live = true;
    setNode(null);
    setError(null);
    setFullText(false);
    setShowOriginal(false);
    api.node(id).then((n) => live && setNode(n)).catch((e) => live && setError(`Couldn't load details. (${String(e)})`));
    return () => { live = false; };
  }, [id, attempt]);

  if (error) return <div className="p-4"><InlineError message={error} onRetry={() => setAttempt((a) => a + 1)} /></div>;
  if (!node) return <InspectorSkeleton />;
  if (node.type === "Project" && onSelectAction) {
    return <EntityInspector id={node.id} onSelectAction={onSelectAction} onCloseAction={onCloseAction}
      onChangedAction={onChangedAction ?? (() => {})} />;
  }

  const src = node.source_item;
  const meta = (src?.raw_meta ?? {}) as { from?: { email: string }[]; to?: { email: string }[] };
  const body = src?.body_text ?? "";
  const long = body.length > 600;

  return (
    <div className="space-y-4 p-4 text-sm">
      <header>
        <div className="flex items-center gap-2 text-xs text-muted">
          <span className="h-2 w-2 rounded-full" style={{ background: TYPE_COLORS[node.type] }} />
          {typeLabel(node.type, node.props as { group?: boolean } | undefined)}
          {onCloseAction && (
            <button onClick={onCloseAction} aria-label="Close details"
              className="ml-auto rounded px-1.5 text-lg leading-none hover:bg-hover hover:text-ink">×</button>
          )}
        </div>
        <h2 className="mt-1 text-lg font-semibold leading-snug break-words">{node.name}</h2>
        {/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(node.key) && node.key.toLowerCase() !== node.name.toLowerCase() && (
          <div className="mt-0.5 text-xs break-all text-muted">{node.key}</div>)}
      </header>

      <NodeSummary id={node.id} onSelectAction={(x) => onSelectAction?.(x)} onWhyAction={onWhyAction} />

      <button disabled={busy} aria-pressed={expanded}
        onClick={async () => { setBusy(true); try { await onExpandAction(node.id); } finally { setBusy(false); } }}
        className={`inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs transition-colors disabled:opacity-60 ${
          expanded ? "border-accent/50 bg-accent/10 text-ink hover:bg-accent/15" : "border-line hover:bg-hover"}`}>
        {busy && <span aria-hidden className="h-3 w-3 animate-spin rounded-full border-2 border-accent border-t-transparent" />}
        {busy ? (expanded ? "Hiding…" : "Finding connections…")
          : expanded ? "Hide their connections" : "Show their connections on the graph"}
      </button>

      <Section title="Sources">
        <Sources id={node.id} highlight={[node.name]} />
      </Section>

      {src && (
        <Section title="Original">
          <button onClick={() => setShowOriginal((v) => !v)} aria-expanded={showOriginal}
            className="text-xs text-accent hover:underline">{showOriginal ? "Hide original" : "Show original"}</button>
          {showOriginal && <>
          <div className="space-y-0.5 text-xs text-soft">
            {src.occurred_at && <div>{when(src.occurred_at)}</div>}
            {meta.from?.[0] && <div className="break-words">From {meta.from[0].email}</div>}
            {meta.to && meta.to.length > 0 && <div className="break-words">To {meta.to.map((a) => a.email).join(", ")}</div>}
          </div>
          <pre className={`mt-2 overflow-y-auto rounded-md bg-bg p-2.5 text-xs leading-relaxed whitespace-pre-wrap text-soft ${fullText ? "max-h-96" : "max-h-32 overflow-hidden"}`}>{body}</pre>
          {long && (
            <button onClick={() => setFullText((v) => !v)} className="mt-1.5 text-xs text-accent hover:underline">
              {fullText ? "Show less" : "Show full text"}
            </button>
          )}
          </>}
        </Section>
      )}
    </div>
  );
}
