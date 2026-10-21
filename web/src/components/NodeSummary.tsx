"use client";

import { useEffect, useState } from "react";
import { InlineError, Skeleton } from "@/components/ui";
import { api, NodeSummary as Summary } from "@/lib/api";
import { CATEGORY_COLOR, TYPE_CATEGORY } from "@/lib/graph-style";

const colorOf = (h: { type: string; category: string | null }) =>
  CATEGORY_COLOR[h.category ?? TYPE_CATEGORY[h.type] ?? ""] ?? "#a8aca3";

/** How a node connects to the rest of the graph, in a few plain sentences, then its top connections by category. */
export default function NodeSummary({ id, onSelectAction, showConnections = true }: {
  id: number; onSelectAction: (id: number) => void; showConnections?: boolean;
}) {
  const [data, setData] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const request = new AbortController();
    setData(null);
    setError(null);
    api.nodeSummary(id, request.signal)
      .then(setData)
      .catch((e) => { if (!request.signal.aborted) setError(`Couldn't summarize this. (${String(e)})`); });
    return () => request.abort();
  }, [id, attempt]);

  if (error) return <InlineError message={error} onRetry={() => setAttempt((a) => a + 1)} />;
  if (!data) {
    return (
      <div className="space-y-2" role="status" aria-label="Summarizing connections">
        <Skeleton className="h-3.5 w-full" /><Skeleton className="h-3.5 w-11/12" /><Skeleton className="h-3.5 w-3/5" />
      </div>
    );
  }

  const groups = new Map<string, Summary["highlights"]>();
  for (const h of data.highlights) {
    const key = h.category ?? TYPE_CATEGORY[h.type] ?? "Other";
    groups.set(key, [...(groups.get(key) ?? []), h]);
  }

  return (
    <div className="space-y-4">
      <section aria-label="Summary">
        <p className="text-sm leading-relaxed">{data.summary}</p>
        {data.generated_by === "model" && <p className="mt-1 text-[11px] text-muted">Summarized by AI from this item&apos;s connections.</p>}
      </section>
      {showConnections && groups.size > 0 && (
        <section aria-label="Top connections" className="border-t border-line pt-4">
          <h3 className="mb-2 text-xs font-medium text-muted">Top connections</h3>
          <div className="space-y-3">
            {[...groups].map(([cat, items]) => (
              <div key={cat}>
                <div className="mb-0.5 flex items-center gap-1.5 text-[11px] text-muted">
                  <span className="h-1.5 w-1.5 rounded-full" style={{ background: colorOf(items[0]) }} />{cat}
                </div>
                <ul>
                  {items.map((h) => (
                    <li key={h.id}>
                      <button onClick={() => onSelectAction(h.id)}
                        className="flex w-full flex-col rounded px-1.5 py-1 text-left hover:bg-hover">
                        <span className="truncate text-sm">{h.name}</span>
                        <span className="truncate text-xs text-muted">{h.why}</span>
                      </button>
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
