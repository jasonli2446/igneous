"use client";

import { useEffect, useMemo, useState } from "react";
import { api, Timeline } from "@/lib/api";

type Props = {
  a: number;
  b?: number;
  title?: string;
};

function dateLabel(value: string | null) {
  if (!value) return "Undated";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(new Date(value));
}

function intervalLabel(from: string | null, to: string | null) {
  if (!from && !to) return "Undated";
  if (!to || from?.slice(0, 10) === to.slice(0, 10)) return dateLabel(from ?? to);
  return `${dateLabel(from)} – ${dateLabel(to)}`;
}

/** Metadata-only dated activity. Raw source content intentionally stays out of this view. */
export default function TimelineExplorer({ a, b, title = "Activity timeline" }: Props) {
  const [result, setResult] = useState<Timeline | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setResult(null);
    setError(null);
    api.timeline(a, b, 50, controller.signal)
      .then(setResult)
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "Unable to load timeline");
      });
    return () => controller.abort();
  }, [a, b]);

  const edges = useMemo(() => [...(result?.edges ?? [])].reverse(), [result]);

  return (
    <section aria-labelledby={`timeline-${a}-${b ?? "activity"}`} className="border-t border-line pt-4">
      <div className="flex items-baseline justify-between gap-2">
        <h3 id={`timeline-${a}-${b ?? "activity"}`} className="font-medium">{title}</h3>
        {result && <span className="shrink-0 text-xs text-muted">{result.mode === "relationship" ? "Direct evidence" : "Dated graph activity"}</span>}
      </div>
      <p className="mt-1 text-xs leading-relaxed text-muted">
        Evidence metadata only — no message contents or source titles are shown here.
      </p>

      {!result && !error && <p role="status" className="mt-3 text-xs text-muted">Loading dated activity…</p>}
      {error && <p role="alert" className="mt-3 text-xs leading-relaxed text-danger">Couldn&apos;t load the timeline: {error}</p>}
      {result && edges.length === 0 && (
        <p className="mt-3 rounded-md border border-dashed border-line p-3 text-xs leading-relaxed text-muted">
          No dated evidence is available for this {result.mode === "relationship" ? "relationship" : "person or entity"}.
        </p>
      )}
      {edges.length > 0 && (
        <ol className="mt-3 space-y-0" aria-label={`${title}, oldest first`}>
          {edges.map((edge) => (
            <li key={edge.id} className="relative border-l border-line py-3 pl-4 first:pt-0 last:pb-0">
              <span aria-hidden="true" className="absolute -left-[4px] top-4 h-2 w-2 rounded-full bg-accent first:top-1" />
              <time className="block text-xs font-medium tabular-nums text-soft">{intervalLabel(edge.interval.from, edge.interval.to)}</time>
              <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs">
                <span className="font-medium">{edge.type.replaceAll("_", " ")}</span>
                <span className="text-muted">{edge.direction}</span>
                <span className="text-muted">·</span>
                <span className="text-soft">{edge.counterpart.type} #{edge.counterpart.id}</span>
              </div>
              <dl className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[11px] leading-relaxed text-muted">
                <div><dt className="sr-only">Source</dt><dd>{edge.provenance.evidence.source}</dd></div>
                {edge.provenance.confidence !== null && <div><dt className="sr-only">Confidence</dt><dd>confidence {edge.provenance.confidence.toFixed(2)}</dd></div>}
                <div><dt className="sr-only">Extraction</dt><dd>{edge.provenance.extractor} v{edge.provenance.extractor_version}</dd></div>
                {edge.provenance.model && <div><dt className="sr-only">Model</dt><dd>{edge.provenance.model.replace("us.anthropic.", "")}</dd></div>}
                <div><dt className="sr-only">Evidence span</dt><dd>{edge.provenance.evidence.span ? "source span recorded" : "no source span"}</dd></div>
              </dl>
            </li>
          ))}
        </ol>
      )}
      {result?.truncated && <p className="mt-3 text-xs text-muted">Showing the first 50 dated records. Refine the selected relationship to narrow the evidence.</p>}
    </section>
  );
}
