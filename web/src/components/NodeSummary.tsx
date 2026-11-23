"use client";

import { useId } from "react";
import { InlineError, Skeleton } from "@/components/ui";
import { api, type NodeSummary as Summary } from "@/lib/api";
import { nodeColor, sectionOf } from "@/lib/graph-style";
import { useRequest } from "@/lib/hooks";

/** How a node connects to the rest of the graph, in a few plain sentences, then its top connections by section. */
export default function NodeSummary({ id, revision = 0, onSelectAction, onWhyAction, showConnections = true }: {
  id: number; revision?: number; onSelectAction: (id: number) => void; onWhyAction?: (a: number, b: number) => void;
  showConnections?: boolean;
}) {
  const { data, error, retry } = useRequest(id, (signal) => api.nodeSummary(id, signal), revision);
  const headingId = useId();

  if (error && !data) return <InlineError message={`Couldn't summarize this. (${String(error)})`} onRetry={retry} />;
  if (!data) {
    return (
      <div className="space-y-2" role="status" aria-label="Summarizing connections">
        <Skeleton className="h-3.5 w-full" /><Skeleton className="h-3.5 w-11/12" /><Skeleton className="h-3.5 w-3/5" />
      </div>
    );
  }

  const groups = new Map<string, Summary["highlights"]>();
  for (const h of data.highlights) {
    const key = sectionOf(h) ?? "Other";
    groups.set(key, [...(groups.get(key) ?? []), h]);
  }

  return (
    <div className="space-y-4">
      <section aria-label="Summary">
        <p className="text-sm leading-relaxed">{data.summary}</p>
        {data.generated_by === "model" && <p className="mt-1 text-[11px] text-muted">Summarized by AI from this item&apos;s connections.</p>}
      </section>
      {showConnections && groups.size > 0 && (
        <section aria-labelledby={headingId} className="border-t border-line pt-4">
          <h3 id={headingId} className="mb-2 text-xs font-medium text-muted">Top connections</h3>
          <div className="space-y-3">
            {[...groups].map(([section, items]) => (
              <div key={section}>
                <div className="mb-0.5 flex items-center gap-1.5 text-[11px] text-muted">
                  <span aria-hidden className="h-1.5 w-1.5 rounded-full" style={{ background: nodeColor(items[0]) }} />{section}
                </div>
                <ul>
                  {items.map((h) => (
                    <li key={h.id} className="flex items-center gap-1">
                      <button type="button" onClick={() => onSelectAction(h.id)}
                        className="flex min-w-0 flex-1 flex-col rounded px-1.5 py-1 text-left hover:bg-hover">
                        <span className="truncate text-sm">{h.name}</span>
                        <span className="truncate text-xs text-muted">{h.why}</span>
                      </button>
                      {onWhyAction && (
                        <button type="button" onClick={() => onWhyAction(id, h.id)} aria-label={`Why is ${h.name} connected?`}
                          title={`Why is ${h.name} connected?`} className="btn-chip shrink-0">Why</button>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
