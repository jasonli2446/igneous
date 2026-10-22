"use client";

import { useEffect, useState } from "react";
import { SourceRows } from "@/components/SourceList";
import { InlineError, Skeleton } from "@/components/ui";
import { api, Explanation, WhyEdge } from "@/lib/api";

// Where a link came from, in plain words (instead of internal stage names).
const ORIGIN: Record<string, string> = {
  spine: "Straight from your data (who sent and received it, or the conversation it's in).",
  contacts: "From your contacts.",
  threads: "Suggested automatically from shared words and timing; not checked yet.",
  hierarchy: "Suggested automatically from the names; not checked yet.",
  "thread-resolution": "Suggested automatically; not checked yet.",
  er: "Matched automatically by name and contacts; not checked yet.",
  organizations: "Suggested from email domains and work history; not checked yet.",
};

const REL: Record<string, string> = {
  CORRESPONDS: "corresponds with", PART_OF: "is part of", INVOLVED_IN: "is involved in", RELATED_TO: "is related to",
  ATTENDED: "attended", MENTIONS: "mentions", ABOUT: "is about", INTRODUCED: "introduced", MEMBER_OF: "is a member of",
  SENT: "sent", RECEIVED: "received", IN_THREAD: "is in", REPLIES_TO: "replied to", AUTHORED: "authored", SAME_AS: "is the same as",
};

/** One link: who and how, the verifier's reason when it has judged it, and the items it came from. */
function EdgeCard({ e, terms, onWrong }: { e: WhyEdge; terms: string[]; onWrong?: (e: WhyEdge) => void }) {
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
        : <p className="mt-1 text-xs text-muted">{ORIGIN[e.extractor] ?? "Inferred automatically; not checked yet."}</p>}
      {e.evidence.length > 0 && <div className="mt-2"><SourceRows items={e.evidence} highlight={terms} /></div>}
      {e.extractor !== "spine" && onWrong && (
        <button onClick={() => onWrong(e)} className="mt-2 text-xs text-muted hover:text-danger hover:underline">
          This is wrong
        </button>
      )}
    </li>
  );
}

function range(a: string | null, b: string | null) {
  if (!a) return "";
  const f = (d: string) => new Date(d).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  return a.slice(0, 10) === b?.slice(0, 10) ? f(a) : `${f(a)} – ${f(b!)}`;
}

/** The evidence behind a link: what two people actually share. */
export default function WhyConnected({ a, b, onCloseAction, onOpenAction, onChangedAction }: {
  a: number; b: number; onCloseAction: () => void; onOpenAction: (id: number) => void;
  /** A link was removed: the page drops it (and anything only it was holding) from what's on screen. */
  onChangedAction?: (removed: { src: number; dst: number }) => void;
}) {
  const [r, setR] = useState<Explanation | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  // "This is wrong": your verdict removes the link now and keeps it gone on every rebuild.
  const wrong = (edge: WhyEdge) => {
    api.edgeIsWrong(edge.id).then(() => { setAttempt((n) => n + 1); onChangedAction?.({ src: edge.src, dst: edge.dst }); })
      .catch((e) => setErr(String(e)));
  };

  useEffect(() => {
    let live = true;
    setR(null);
    setErr(null);
    api.explain(a, b).then((x) => live && setR(x)).catch((e) => live && setErr(String(e)));
    return () => { live = false; };
  }, [a, b, attempt]);

  return (
    <div className="space-y-4 p-4 text-sm">
      <div className="flex items-start gap-2">
        <h2 className="text-base font-semibold leading-snug">
          {r ? <>Why {r.a.name} and {r.b.name} are connected</> : "Why they're connected"}
        </h2>
        <button onClick={onCloseAction} aria-label="Close"
          className="ml-auto rounded px-1.5 text-lg leading-none text-muted hover:bg-hover">×</button>
      </div>

      {err && <InlineError message={`Couldn't load the connection: ${err}`} onRetry={() => setAttempt((n) => n + 1)} />}
      {!r && !err && (
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
            {r.messages === 0 && r.events.length === 0 && !r.direct.length && !r.path && "No connection found within three steps."}
          </p>

          {r.direct.length > 0 && (
            <section aria-label="Why linked">
              <h3 className="mb-2 text-xs font-medium text-muted">Why they&apos;re linked</h3>
              <ul className="space-y-2">{r.direct.map((e) => <EdgeCard key={e.id} e={e} terms={[r.a.name, r.b.name]} onWrong={wrong} />)}</ul>
            </section>
          )}

          {r.direct.length === 0 && r.path && (
            <section aria-label="Path">
              <h3 className="mb-2 text-xs font-medium text-muted">
                {r.path.length <= 2 ? "How they're linked" : `Linked through ${r.path.length - 1} other thing${r.path.length === 2 ? "" : "s"}`}
              </h3>
              <ul className="space-y-2">{r.path.map((e) => <EdgeCard key={e.id} e={e} terms={[r.a.name, r.b.name]} onWrong={wrong} />)}</ul>
            </section>
          )}

          {r.shared.length > 0 && (
            <section aria-label="Latest messages together">
              <h3 className="mb-2 text-xs font-medium text-muted">Latest messages together</h3>
              <SourceRows items={r.shared} highlight={[r.a.name, r.b.name]} />
            </section>
          )}

          {r.conversations.length > 0 && (
            <section>
              <h3 className="mb-2 text-xs font-medium text-muted">Conversations</h3>
              <ul className="space-y-2">
                {r.conversations.map((c) => (
                  <li key={c.thread_id} className="rounded-md border border-line p-2.5">
                    <button onClick={() => onOpenAction(c.thread_id)} className="text-left font-medium hover:underline">
                      {c.thread || "Untitled conversation"}
                    </button>
                    <div className="mt-0.5 text-xs text-muted">
                      {c.messages} together{c.people > 2 ? `, ${c.people} people in it` : ""} ·{" "}
                      {range(c.first, c.last)} · {c.source === "imessage" ? "Messages" : c.source === "gmail" ? "Gmail" : "Email"}
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
            <section>
              <h3 className="mb-2 text-xs font-medium text-muted">Events together</h3>
              <ul className="space-y-1">
                {r.events.map((e) => (
                  <li key={e.id} className="flex gap-2">
                    <button onClick={() => onOpenAction(e.id)} className="truncate text-left hover:underline">{e.name}</button>
                    <span className="ml-auto shrink-0 text-xs text-muted">
                      {e.at ? new Date(e.at).toLocaleDateString() : ""}
                    </span>
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
