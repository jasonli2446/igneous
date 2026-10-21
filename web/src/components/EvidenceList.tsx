"use client";

import { useEffect, useState } from "react";
import { InlineError, Skeleton } from "@/components/ui";
import { api, Evidence, TYPE_COLORS } from "@/lib/api";

// "Why do you believe this": every model-derived edge on a node, with its source span highlighted.
export default function EvidenceList({ id }: { id: number }) {
  const [items, setItems] = useState<Evidence[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    setItems(null);
    setError(null);
    api.evidence(id).then((r) => live && setItems(r.evidence)).catch((e) => live && setError(`Couldn't load evidence. (${String(e)})`));
    return () => { live = false; };
  }, [id, attempt]);

  if (error) return <InlineError message={error} onRetry={() => setAttempt((a) => a + 1)} />;
  if (items === null) return (
    <div className="space-y-2" role="status" aria-label="Loading evidence">
      <Skeleton className="h-16 w-full" /><Skeleton className="h-16 w-full" />
    </div>
  );
  if (items.length === 0) return <p className="text-xs text-muted">Nothing extracted yet. Run the extract step to find mentions and topics.</p>;

  return (
    <ul className="space-y-3">
      {items.map((e) => (
        <li key={e.id} className="rounded-md border border-line p-2 text-xs">
          <div className="flex items-center gap-1.5">
            <span className="font-mono text-muted">{e.type}</span>
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: TYPE_COLORS[e.other_type] }} />
            <span className="truncate font-medium">{e.other_name}</span>
            {e.confidence !== null && <span className="ml-auto text-muted">{e.confidence.toFixed(2)}</span>}
          </div>
          {e.props.role && <div className="mt-0.5 text-muted">{e.props.role}</div>}
          {e.type !== "ABOUT" && (
            <p className="mt-1.5 rounded bg-bg p-1.5 leading-relaxed whitespace-pre-wrap text-soft">
              …{e.window.slice(Math.max(0, e.hl_start - 120), e.hl_start)}
              <mark className="rounded bg-accent/25 px-0.5 text-ink">{e.window.slice(e.hl_start, e.hl_end)}</mark>
              {e.window.slice(e.hl_end, e.hl_end + 120)}…
            </p>
          )}
          <div className="mt-1 truncate text-muted">
            {e.title || "(no subject)"}{e.occurred_at && ` · ${new Date(e.occurred_at).toLocaleDateString()}`} · {e.model?.replace("us.anthropic.", "")} v{e.extractor_version}
          </div>
        </li>
      ))}
    </ul>
  );
}
