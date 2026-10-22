"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import NodeSummary from "@/components/NodeSummary";
import Sources from "@/components/SourceList";
import { InlineError, Skeleton } from "@/components/ui";
import { api, Correction, EntityDetail, GNode, TYPE_COLORS } from "@/lib/api";

type Props = {
  id: number;
  onSelectAction: (id: number) => void;
  onCloseAction?: () => void;
  /** Called after any correction or undo so the graph can re-fetch; `keep` is the node to stay on, if any. */
  onChangedAction: (keep: number | null) => void;
};

const ANCHOR_SENTENCES: [RegExp, (m: RegExpMatchArray) => string][] = [
  [/^event series \((\d+)\)$/, (m) => `A recurring calendar event, ${m[1]} times`],
  [/^repeated event$/, () => "Calendar events with this title"],
  [/^group chat$/, () => "A group chat with this name"],
  [/^folder$/, () => "A folder with this name"],
  [/^repo$/, () => "A code repository with this name"],
];
const anchorSentence = (a: string) => {
  for (const [re, f] of ANCHOR_SENTENCES) { const m = a.match(re); if (m) return f(m); }
  return a;
};

const WHY: Record<string, string> = {
  "event series": "Part of the series", "repeated event": "Same title", "group chat": "Group chat",
  folder: "In the folder", repo: "In the repo", mentions: "Mentions it", "chat mentions it": "Chat talks about it",
};

const day = (iso: string | null) =>
  iso ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(iso)) : null;

function Section({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  return (
    <section className="border-t border-line pt-4">
      <h3 className="mb-2 text-xs font-medium text-muted">{title}{count !== undefined && ` (${count})`}</h3>
      {children}
    </section>
  );
}

function RemoveButton({ label, onClick, disabled }: { label: string; onClick: () => void; disabled: boolean }) {
  return (
    <button onClick={onClick} disabled={disabled} aria-label={label} title="Remove from project"
      className="shrink-0 rounded px-1.5 text-base leading-none text-muted hover:bg-hover hover:text-danger disabled:opacity-40">×</button>
  );
}

function MergePicker({ self, onPick, onCancel }: { self: number; onPick: (n: GNode) => void; onCancel: () => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<GNode[]>([]);
  useEffect(() => {
    if (q.trim().length < 2) return setHits([]);
    const t = setTimeout(() => {
      api.findNodes(q, "Project").then((r) => setHits(r.nodes.filter((n) => n.type === "Project" && n.id !== self))).catch(() => {});
    }, 200);
    return () => clearTimeout(t);
  }, [q, self]);
  return (
    <div className="mt-2 rounded-md border border-line p-2">
      <div className="flex items-center gap-2">
        <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find the project to fold in"
          aria-label="Search projects to merge" onKeyDown={(e) => e.key === "Escape" && onCancel()}
          className="min-w-0 flex-1 rounded border border-line bg-bg px-2 py-1 text-xs outline-none focus:border-accent" />
        <button onClick={onCancel} className="text-xs text-muted hover:text-ink">Cancel</button>
      </div>
      {q.trim().length >= 2 && (
        <ul className="mt-2 space-y-0.5">
          {hits.map((n) => (
            <li key={n.id}>
              <button onClick={() => onPick(n)} className="flex w-full justify-between rounded px-1.5 py-1 text-left text-xs hover:bg-hover">
                <span className="truncate">{n.name}</span><span className="text-muted tabular-nums">{n.degree}</span>
              </button>
            </li>
          ))}
          {hits.length === 0 && <li className="px-1.5 py-1 text-xs text-muted">No other project matches.</li>}
        </ul>
      )}
    </div>
  );
}

export default function EntityInspector({ id, onSelectAction, onCloseAction, onChangedAction }: Props) {
  const [e, setE] = useState<EntityDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [merging, setMerging] = useState(false);
  const [toast, setToast] = useState<{ text: string; undo: number } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let live = true;
    setE(null); setError(null); setEditing(false); setMerging(false); setToast(null);
    api.entity(id).then((r) => live && setE(r)).catch((x) => live && setError(`Couldn't load this project. (${String(x)})`));
    return () => { live = false; };
  }, [id, attempt]);
  useEffect(() => () => { if (toastTimer.current) clearTimeout(toastTimer.current); }, []);

  const reload = useCallback(() => api.entity(id).then(setE), [id]);

  const act = async (text: string, run: () => Promise<Correction>, keep: number | null = id) => {
    setBusy(true);
    setError(null);
    try {
      const c = await run();
      setToast({ text, undo: c.override_id });
      if (toastTimer.current) clearTimeout(toastTimer.current);
      toastTimer.current = setTimeout(() => setToast(null), 8000);
      await reload().catch(() => {});
      onChangedAction(keep);
    } catch (x) {
      setError(`That didn't work. (${String(x)})`);
    } finally {
      setBusy(false);
    }
  };

  const undo = async () => {
    if (!toast) return;
    setBusy(true);
    try {
      await api.undoCorrection(toast.undo);
      setToast(null);
      await reload().catch(() => {});
      onChangedAction(id);
    } catch (x) {
      setError(`Couldn't undo. (${String(x)})`);
    } finally {
      setBusy(false);
    }
  };

  if (error && !e) return <div className="p-4"><InlineError message={error} onRetry={() => setAttempt((a) => a + 1)} /></div>;
  if (!e) return (
    <div className="space-y-5 p-4" role="status" aria-label="Loading project">
      <div className="space-y-2"><Skeleton className="h-3 w-16" /><Skeleton className="h-5 w-3/4" /></div>
      <Skeleton className="h-14 w-full" /><Skeleton className="h-20 w-full" />
    </div>
  );

  const saveName = () => {
    const name = draft.trim();
    setEditing(false);
    if (name && name !== e.name) act(`Renamed to "${name}"`, () => api.renameEntity(id, name));
  };

  return (
    <div className="relative space-y-4 p-4 pb-20 text-sm">
      <header>
        <div className="flex items-center gap-2 text-xs text-muted">
          <span className="h-2 w-2 rounded-full" style={{ background: TYPE_COLORS.Project }} />
          Project
          {onCloseAction && (
            <button onClick={onCloseAction} aria-label="Close details"
              className="ml-auto rounded px-1.5 text-lg leading-none hover:bg-hover hover:text-ink">×</button>
          )}
        </div>
        {editing ? (
          <input autoFocus value={draft} onChange={(x) => setDraft(x.target.value)} onBlur={saveName} onFocus={(x) => x.target.select()} aria-label="Project name"
            onKeyDown={(x) => { if (x.key === "Enter") saveName(); if (x.key === "Escape") setEditing(false); }}
            className="mt-1 w-full rounded border border-accent bg-bg px-2 py-1 text-lg font-semibold outline-none" />
        ) : (
          <h2 className="group mt-1 flex items-baseline gap-2 text-lg font-semibold leading-snug break-words">
            {e.name}
            <button onClick={() => { setDraft(e.name); setEditing(true); }}
              className="text-xs font-normal text-muted hover:text-accent">Rename</button>
          </h2>
        )}
        {e.aliases.length > 0 && <div className="mt-0.5 text-xs text-muted">Also known as {e.aliases.join(", ")}</div>}
        {e.hidden && <div className="mt-1 text-xs text-danger">Hidden from the graph. Undo to bring it back.</div>}
        {error && <div className="mt-2"><InlineError message={error} /></div>}
      </header>

      <NodeSummary id={id} onSelectAction={onSelectAction} showConnections={false} />

      {e.anchors.length > 0 && (
        <Section title="Known from">
          <ul className="space-y-1 text-xs text-soft">{e.anchors.map((a) => <li key={a}>{anchorSentence(a)}</li>)}</ul>
        </Section>
      )}

      <Section title="People" count={e.counts.people}>
        {e.people.length === 0 ? <p className="text-xs text-muted">No one shows up in two or more of its items.</p> : (
          <ul className="space-y-0.5">
            {e.people.map((p) => (
              <li key={p.id} className="flex items-center gap-1">
                <button onClick={() => onSelectAction(p.id)} className="flex min-w-0 flex-1 justify-between gap-2 rounded px-1.5 py-1 text-left text-xs hover:bg-hover">
                  <span className="truncate">{p.name}</span><span className="shrink-0 text-muted tabular-nums">{p.items} items</span>
                </button>
                <RemoveButton label={`Remove ${p.name} from this project`} disabled={busy}
                  onClick={() => act(`Removed ${p.name}`, () => api.unlinkFromEntity(id, p.id))} />
              </li>
            ))}
          </ul>
        )}
      </Section>

      {e.groups.length > 0 && (
        <Section title="Group chats" count={e.counts.groups}>
          <ul className="space-y-0.5">
            {e.groups.map((g) => (
              <li key={g.id} className="flex items-center gap-1">
                <button onClick={() => onSelectAction(g.id)} className="min-w-0 flex-1 truncate rounded px-1.5 py-1 text-left text-xs hover:bg-hover">{g.name}</button>
                <RemoveButton label={`Remove ${g.name} from this project`} disabled={busy}
                  onClick={() => act(`Removed ${g.name}`, () => api.unlinkFromEntity(id, g.id))} />
              </li>
            ))}
          </ul>
        </Section>
      )}

      {e.events.length > 0 && (
        <Section title="Events" count={e.counts.events}>
          <ul className="space-y-0.5">
            {e.events.map((v) => (
              <li key={v.id} className="flex items-center gap-1">
                <button onClick={() => onSelectAction(v.id)} className="flex min-w-0 flex-1 justify-between gap-2 rounded px-1.5 py-1 text-left text-xs hover:bg-hover">
                  <span className="truncate">{v.name}</span>
                  {v.date && <span className="shrink-0 text-muted">{day(v.date)}</span>}
                </button>
                <RemoveButton label={`Remove ${v.name} from this project`} disabled={busy}
                  onClick={() => act(`Removed ${v.name}`, () => api.unlinkFromEntity(id, v.id))} />
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title="Items" count={e.counts.items}>
        {e.items.length === 0 ? <p className="text-xs text-muted">No messages or files mention it yet.</p> : (
          <ul className="space-y-2">
            {e.items.map((i) => (
              <li key={i.id} className="rounded-md border border-line p-2 text-xs">
                <div className="flex items-center gap-1.5">
                  <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: TYPE_COLORS[i.type] }} />
                  <button onClick={() => onSelectAction(i.id)} className="min-w-0 flex-1 truncate text-left font-medium hover:underline">{i.name}</button>
                  <RemoveButton label={`Remove ${i.name} from this project`} disabled={busy}
                    onClick={() => act("Removed 1 item", () => api.unlinkFromEntity(id, i.id))} />
                </div>
                <div className="mt-0.5 text-muted">
                  {[i.why ? WHY[i.why] ?? i.why : null, i.source, day(i.date)].filter(Boolean).join(" · ")}
                </div>
                {i.quote && <blockquote className="mt-1 border-l-2 border-line pl-2 leading-relaxed break-words text-soft">{i.quote.trim()}</blockquote>}
              </li>
            ))}
          </ul>
        )}
        {e.counts.items > e.items.length && <p className="mt-2 text-xs text-muted">Showing the {e.items.length} most recent.</p>}
      </Section>

      <Section title="Sources">
        <Sources id={id} highlight={[e.name, ...e.aliases]} />
      </Section>

      <Section title="Fix this project">
        <div className="flex flex-wrap gap-2">
          <button disabled={busy || e.hidden} onClick={() => act(`Hid "${e.name}"`, () => api.hideEntity(id))}
            className="rounded-md border border-line px-2.5 py-1 text-xs hover:bg-hover disabled:opacity-40">Hide</button>
          <button disabled={busy} onClick={() => setMerging((v) => !v)}
            className="rounded-md border border-line px-2.5 py-1 text-xs hover:bg-hover disabled:opacity-40">Merge with…</button>
        </div>
        {merging && (
          <MergePicker self={id} onCancel={() => setMerging(false)} onPick={(n) => {
            setMerging(false);
            act(`Merged "${n.name}" into "${e.name}"`, () => api.mergeEntity(id, n.id));
          }} />
        )}
      </Section>

      {toast && (
        <div role="status" className="fixed right-4 bottom-4 z-30 flex max-w-80 items-center gap-3 rounded-md border border-line bg-panel px-3 py-2 text-xs shadow-lg">
          <span className="min-w-0 break-words">{toast.text}</span>
          <button onClick={undo} disabled={busy} className="shrink-0 font-medium text-accent hover:underline disabled:opacity-40">Undo</button>
        </div>
      )}
    </div>
  );
}
