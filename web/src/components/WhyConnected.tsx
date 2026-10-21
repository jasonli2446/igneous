"use client";

import { useEffect, useState } from "react";
import TimelineExplorer from "@/components/TimelineExplorer";
import { InlineError, Skeleton } from "@/components/ui";
import { api, Explanation } from "@/lib/api";

function range(a: string | null, b: string | null) {
  if (!a) return "";
  const f = (d: string) => new Date(d).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  return a.slice(0, 10) === b?.slice(0, 10) ? f(a) : `${f(a)} – ${f(b!)}`;
}

/** The evidence behind a link: what two people actually share. */
export default function WhyConnected({ a, b, onCloseAction, onOpenAction }: {
  a: number; b: number; onCloseAction: () => void; onOpenAction: (id: number) => void;
}) {
  const [r, setR] = useState<Explanation | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

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
            {r.messages === 0 && r.events.length === 0 && "They're linked through a group they're both in, but share no direct messages."}
          </p>

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
          <TimelineExplorer a={a} b={b} title="Relationship timeline" />
        </>
      )}
    </div>
  );
}
