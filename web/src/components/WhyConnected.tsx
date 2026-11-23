"use client";

import { useState } from "react";
import type { Corrected } from "@/components/Inspector";
import { SourceRows } from "@/components/SourceList";
import { InlineError, Skeleton } from "@/components/ui";
import { api, type WhyEdge } from "@/lib/api";
import { formatDay, formatDayRange } from "@/lib/dates";
import { useRequest } from "@/lib/hooks";

const REL: Record<string, string> = {
  CORRESPONDS: "corresponds with", PART_OF: "is part of", INVOLVED_IN: "is involved in", RELATED_TO: "is related to",
  ATTENDED: "attended", MENTIONS: "mentions", ABOUT: "is about", INTRODUCED: "introduced", MEMBER_OF: "is a member of",
  SENT: "sent", RECEIVED: "received", IN_THREAD: "is in", REPLIES_TO: "replied to", AUTHORED: "authored", SAME_AS: "is the same as",
};

/** One link: who and how, where it came from (the API says, in plain words), the verifier's reason when it has
 *  judged it, and the items it came from. Only an inferred link can be called wrong. */
function EdgeCard({ e, terms, onWrong, busy }: { e: WhyEdge; terms: string[]; onWrong?: (e: WhyEdge) => void; busy?: boolean }) {
  return (
    <li className="rounded-md border border-line p-2.5">
      <div className="flex items-start gap-2">
        <span className="min-w-0 flex-1 text-sm font-medium break-words">
          {e.src_name} <span className="font-normal text-muted">{REL[e.type] ?? e.type.toLowerCase()}</span> {e.dst_name}
        </span>
        {e.verdict === "unsure" && <span className="shrink-0 rounded bg-hover px-1.5 py-px text-[10px] text-muted">Suggestion</span>}
      </div>
      {e.reason
        ? <p className="mt-1 text-xs leading-relaxed text-soft">{e.reason}</p>
        : <p className="mt-1 text-xs text-muted">
            {e.why && <span className="text-soft">{e.why[0].toUpperCase() + e.why.slice(1)}. </span>}
            {e.how}
          </p>}
      {e.evidence.length > 0 && <div className="mt-2"><SourceRows items={e.evidence} highlight={terms} /></div>}
      {!e.stated && onWrong && (
        <button type="button" disabled={busy} onClick={() => onWrong(e)}
          className="mt-2 text-xs text-muted hover:text-danger hover:underline disabled:opacity-50">
          This is wrong
        </button>
      )}
    </li>
  );
}

/** The evidence behind a link: what two people actually share. */
export default function WhyConnected({ a, b, revision, onCloseAction, onOpenAction, onCorrectedAction, onRestoredAction }: {
  a: number; b: number;
  /** Bumped after every correction: the panel loads again to show its result. */
  revision: number;
  onCloseAction: () => void; onOpenAction: (id: number) => void;
  /** A link was rejected: the page says so, offers Undo and shows the graph without it. */
  onCorrectedAction: (c: Corrected) => void;
  /** An earlier rejection was undone here: the page shows the link again. */
  onRestoredAction: () => void;
}) {
  const { data: r, error: loadError, retry } = useRequest(`${a}-${b}`, (signal) => api.explain(a, b, signal), revision);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // "This is wrong": your verdict removes the link now and keeps it gone on every rebuild.
  const wrong = (edge: WhyEdge) => {
    if (saving) return;
    setSaving(true);
    setError(null);
    api.edgeIsWrong(edge.id).then((res) => {
      onCorrectedAction({
        text: `Removed ${res.edges.length} inferred connection${res.edges.length === 1 ? "" : "s"}.`,
        undo: () => api.restoreEdges(res.edges),
      });
    }).catch((e) => setError(`Couldn't remove it. (${String(e)})`)).finally(() => setSaving(false));
  };

  const undo = (overrideId: number) => {
    if (saving) return;
    setSaving(true);
    setError(null);
    api.undoCorrection(overrideId).then(onRestoredAction)
      .catch((e) => setError(`Couldn't undo the removal. (${String(e)})`)).finally(() => setSaving(false));
  };

  return (
    <div className="space-y-4 p-4 text-sm">
      <div className="flex items-start gap-2">
        <h2 className="text-base font-semibold leading-snug">
          {r ? <>Why {r.a.name} and {r.b.name} are connected</> : "Why they're connected"}
        </h2>
        <button type="button" onClick={onCloseAction} aria-label="Close" className="btn-close ml-auto">×</button>
      </div>

      {loadError !== undefined && !r && <InlineError message={`Couldn't load the connection. (${String(loadError)})`} onRetry={retry} />}
      {error && <InlineError message={error} />}
      {!r && loadError === undefined && (
        <div className="space-y-2" role="status" aria-label="Loading connection">
          <Skeleton className="h-4 w-full" /><Skeleton className="h-16 w-full" /><Skeleton className="h-16 w-full" />
        </div>
      )}

      {r && (
        <>
          <p className="text-muted">
            {r.messages > 0 && <>{r.messages} message{r.messages === 1 ? "" : "s"} together
              across {r.conversations.length} conversation{r.conversations.length === 1 ? "" : "s"}</>}
            {r.messages > 0 && r.events.length > 0 && ", and "}
            {r.events.length > 0 && <>{r.events.length} shared event{r.events.length === 1 ? "" : "s"}</>}
            {r.messages === 0 && r.events.length === 0 && !r.direct.length && !r.path && !r.corrections?.length && "No connection found within three steps."}
          </p>

          {r.corrections?.length > 0 && (
            <section aria-label="Earlier corrections">
              <h3 className="mb-2 text-xs font-medium text-muted">Earlier corrections</h3>
              <ul className="space-y-2">
                {r.corrections.map((c) => (
                  <li key={c.override_id ?? `${c.src}-${c.dst}`} className="rounded-md border border-line p-2.5">
                    <p className="text-xs leading-relaxed text-soft">
                      You rejected inferred connections between {c.src_name} and {c.dst_name} on{" "}
                      {formatDay(c.updated_at)}. This also blocks later inferred links between them.
                    </p>
                    {c.reason && <p className="mt-1 text-xs text-muted">{c.reason}</p>}
                    {c.override_id !== null && (
                      <button type="button" disabled={saving} onClick={() => undo(c.override_id!)} className="btn-link mt-2">
                        Undo removal
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {r.direct.length > 0 && (
            <section aria-label="Why linked">
              <h3 className="mb-2 text-xs font-medium text-muted">Why they&apos;re linked</h3>
              <ul className="space-y-2">{r.direct.map((e) => <EdgeCard key={e.id} e={e} terms={[r.a.name, r.b.name]} onWrong={wrong} busy={saving} />)}</ul>
            </section>
          )}

          {r.direct.length === 0 && r.path && (
            <section aria-label="Path">
              <h3 className="mb-2 text-xs font-medium text-muted">
                {r.path.length <= 2 ? "How they're linked" : `Linked through ${r.path.length - 1} other thing${r.path.length === 2 ? "" : "s"}`}
              </h3>
              <ul className="space-y-2">{r.path.map((e) => <EdgeCard key={e.id} e={e} terms={[r.a.name, r.b.name]} onWrong={wrong} busy={saving} />)}</ul>
            </section>
          )}

          {r.shared.length > 0 && (
            <section aria-label="Latest messages together">
              <h3 className="mb-2 text-xs font-medium text-muted">Latest messages together</h3>
              <SourceRows items={r.shared} highlight={[r.a.name, r.b.name]} />
            </section>
          )}

          {r.conversations.length > 0 && (
            <section aria-label="Conversations">
              <h3 className="mb-2 text-xs font-medium text-muted">Conversations</h3>
              <ul className="space-y-2">
                {r.conversations.map((c) => (
                  <li key={c.thread_id ?? `${c.thread}-${c.first}`} className="rounded-md border border-line p-2.5">
                    {c.thread_id !== null ? (
                      <button type="button" onClick={() => onOpenAction(c.thread_id!)} className="text-left font-medium hover:underline">
                        {c.thread || "Untitled conversation"}
                      </button>
                    ) : <span className="font-medium">{c.thread || "Untitled conversation"}</span>}
                    <div className="mt-0.5 text-xs text-muted">
                      {c.messages} together{c.people > 2 ? `, ${c.people} people in it` : ""} ·{" "}
                      {formatDayRange(c.first, c.last)} · {c.source === "imessage" ? "Messages" : c.source === "gmail" ? "Gmail" : "Email"}
                    </div>
                    {c.excerpt && (
                      <p className="mt-1.5 line-clamp-3 whitespace-pre-wrap text-xs leading-relaxed text-soft">
                        {c.excerpt.trim()}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {r.events.length > 0 && (
            <section aria-label="Events together">
              <h3 className="mb-2 text-xs font-medium text-muted">Events together</h3>
              <ul className="space-y-1">
                {r.events.map((e) => (
                  <li key={e.id} className="flex gap-2">
                    <button type="button" onClick={() => onOpenAction(e.id)} className="truncate text-left hover:underline">{e.name}</button>
                    <span className="ml-auto shrink-0 text-xs text-muted">{formatDay(e.at)}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  );
}
