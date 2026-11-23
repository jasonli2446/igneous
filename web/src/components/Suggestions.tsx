"use client";

import { useState } from "react";
import { InlineError, Skeleton } from "@/components/ui";
import { api, type Suggestion } from "@/lib/api";
import type { Corrected } from "@/lib/corrections";
import { relationLabel } from "@/lib/graph-style";

/** The links the verifier was unsure about, most confident first, for you to answer. Each answer is a label the
 *  verifier learns from: "Right" keeps the link for good, "Wrong" removes it (with Undo), "Why" opens its evidence. */
export default function Suggestions({ items, error, onRetryAction, onCloseAction, onWhyAction, onCorrectedAction }: {
  items: Suggestion[] | undefined; error: unknown; onRetryAction: () => void; onCloseAction: () => void;
  onWhyAction: (a: number, b: number) => void;
  onCorrectedAction: (c: Corrected) => void;
}) {
  const [busy, setBusy] = useState<number | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  const answer = async (s: Suggestion, right: boolean) => {
    setBusy(s.edge);
    setFailed(null);
    const link = `${s.src_name} ${relationLabel(s.type)} ${s.dst_name}`;
    try {
      if (right) {
        onCorrectedAction({ text: `Kept: ${link}.`, answer: await api.edgeIsRight(s.edge) });
      } else {
        const res = await api.edgeIsWrong(s.edge);
        onCorrectedAction({ text: `Removed: ${link}.`, answer: res, undo: () => api.undoCorrection(res.override_id) });
      }
    } catch (e) {
      setFailed(`Couldn't save your answer. (${String(e)})`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-4 p-4 text-sm">
      <div className="flex items-start gap-2">
        <h2 className="text-base font-semibold leading-snug">Links to check</h2>
        <button type="button" onClick={onCloseAction} aria-label="Close" className="btn-close ml-auto">×</button>
      </div>
      <p className="text-xs leading-relaxed text-muted">
        The checks on each link couldn&apos;t tell from the records whether these hold. Your answer settles each one for
        good, and the checks learn from it.
      </p>
      {error !== undefined && !items && <InlineError message={`Couldn't load them. (${String(error)})`} onRetry={onRetryAction} />}
      {failed && <InlineError message={failed} />}
      {!items && error === undefined && (
        <div className="space-y-2" role="status" aria-label="Loading links to check">
          <Skeleton className="h-16 w-full" /><Skeleton className="h-16 w-full" />
        </div>
      )}
      {items?.length === 0 && <p className="text-xs text-muted">Nothing to check right now.</p>}
      {items && items.length > 0 && (
        <ul className="space-y-2">
          {items.map((s) => (
            <li key={s.edge} className="rounded-md border border-line p-2.5" aria-label={`${s.src_name} ${relationLabel(s.type)} ${s.dst_name}`}>
              <p className="text-sm font-medium break-words">
                {s.src_name} <span className="font-normal text-muted">{relationLabel(s.type)}</span> {s.dst_name}
              </p>
              {s.reason && <p className="mt-1 text-xs leading-relaxed text-soft">{s.reason}</p>}
              {s.confidence !== null && (
                <p className="mt-0.5 text-[11px] text-muted">The checks&apos; confidence: {Math.round(s.confidence * 100)}%</p>
              )}
              <div className="mt-2 flex gap-2 text-xs">
                <button type="button" disabled={busy !== null} onClick={() => void answer(s, true)} className="btn">Right</button>
                <button type="button" disabled={busy !== null} onClick={() => void answer(s, false)} className="btn">Wrong</button>
                <button type="button" onClick={() => onWhyAction(s.src, s.dst)} className="btn-link ml-auto">Why?</button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
