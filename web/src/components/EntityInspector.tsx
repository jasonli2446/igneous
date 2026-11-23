"use client";

import { useState } from "react";
import ConnectTo from "@/components/ConnectTo";
import NodeSummary from "@/components/NodeSummary";
import Sources from "@/components/SourceList";
import { InlineError, Section, Skeleton } from "@/components/ui";
import { api, type EntityCorrected, type EntityDetail, type LinkAdded, type Rejected, type Thing } from "@/lib/api";
import type { Corrected } from "@/lib/corrections";
import { formatDay } from "@/lib/dates";
import { nodeColor } from "@/lib/graph-style";
import { useLiveSearch, useRequest } from "@/lib/hooks";
import { useSourceLabel } from "@/lib/sources";

type Props = {
  id: number;
  /** Bumped after every correction: the panel loads again to show its result. */
  revision: number;
  onSelectAction: (id: number) => void;
  onCloseAction: () => void;
  /** A correction went through: the page says what changed and when it applies, offers Undo and shows the result. */
  onCorrectedAction: (c: Corrected) => void;
  /** A connection you added: shown on the graph in place. */
  onLinkedAction: (from: number, other: Thing, answer: LinkAdded) => void;
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

function RemoveButton({ label, onClick, disabled }: { label: string; onClick: () => void; disabled: boolean }) {
  return (
    <button type="button" onClick={onClick} disabled={disabled} aria-label={label} title="Remove from project"
      className="shrink-0 rounded px-1.5 text-base leading-none text-muted hover:bg-hover hover:text-danger disabled:opacity-40">×</button>
  );
}

function MergePicker({ self, onPick, onCancel }: { self: number; onPick: (n: Thing) => void; onCancel: () => void }) {
  const [q, setQ] = useState("");
  const projects = useLiveSearch(q,
    (text, signal) => api.findThings(text, { types: ["Project"] }, signal).then((r) => r.results.filter((n) => n.id !== self)));
  return (
    <div className="mt-2 rounded-md border border-line p-2">
      <div className="flex items-center gap-2">
        <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find the project to fold in"
          aria-label="Search projects to merge" onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); onCancel(); } }}
          className="field flex-1" />
        <button type="button" onClick={onCancel} className="text-xs text-muted hover:text-ink">Cancel</button>
      </div>
      {projects.active && (
        <ul className="mt-2 space-y-0.5" aria-label="Projects to merge">
          {projects.results?.map((n) => (
            <li key={n.id}>
              <button type="button" onClick={() => onPick(n)} className="btn-option">
                <span className="truncate">{n.name}</span><span className="shrink-0 text-muted">{n.context}</span>
              </button>
            </li>
          ))}
          {projects.results?.length === 0 && (
            <li className="px-1.5 py-1 text-xs text-muted">{projects.failed ? "Search failed." : "No other project matches."}</li>
          )}
          {projects.results === null && <li className="px-1.5 py-1 text-xs text-muted">Searching…</li>}
        </ul>
      )}
    </div>
  );
}

/** Your corrections that shape this project, each with its undo, for as long as it stands: not only in the notice
 *  that follows it. */
function YourCorrections({ e, locked, onUndo }: {
  e: EntityDetail; locked: boolean; onUndo: (text: string, overrideIds: number[]) => void;
}) {
  const renames = e.corrections.filter((c) => c.op === "rename");
  const hides = e.corrections.filter((c) => c.op === "hide");
  const merges = e.corrections.filter((c) => c.op === "merge");
  const rename = renames[renames.length - 1]; // the latest rename is the name it has; undoing it shows the one before
  if (!rename && !hides.length && !merges.length) return null;
  return (
    <ul aria-label="Your corrections" className="mt-2 space-y-1 text-xs">
      {rename && (
        <li className="flex items-center gap-2 text-muted">
          <span className="min-w-0 flex-1">You named it “{rename.value}” on {formatDay(rename.created_at)}.</span>
          <button type="button" disabled={locked} className="btn-link shrink-0"
            onClick={() => onUndo("Name restored.", [rename.override_id])}>Undo rename</button>
        </li>
      )}
      {hides.length > 0 && (
        <li className="flex items-center gap-2 text-danger">
          <span className="min-w-0 flex-1">Hidden from the graph.</span>
          <button type="button" disabled={locked} className="btn-link shrink-0"
            onClick={() => onUndo(`"${e.name}" is on the graph again.`, hides.map((c) => c.override_id))}>Unhide</button>
        </li>
      )}
      {merges.map((m) => (
        <li key={m.override_id} className="flex items-center gap-2 text-muted">
          <span className="min-w-0 flex-1">You merged “{m.value ?? "a project"}” into it.</span>
          <button type="button" disabled={locked} className="btn-link shrink-0"
            onClick={() => onUndo(`"${m.value ?? "The project"}" is its own project again.`, [m.override_id])}>Separate</button>
        </li>
      ))}
    </ul>
  );
}

export default function EntityInspector({ id, revision, onSelectAction, onCloseAction, onCorrectedAction, onLinkedAction }: Props) {
  const { data: e, error: loadError, loading, retry } = useRequest(id, (signal) => api.entity(id, signal), revision);
  const sourceLabel = useSourceLabel();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [merging, setMerging] = useState(false);

  /** Run one correction; the page says what changed and when it applies, offers Undo and refreshes this panel and
   *  the graph. */
  const act = async (text: string, run: () => Promise<EntityCorrected | Rejected>, keep: number = id) => {
    setBusy(true);
    setError(null);
    try {
      const answer = await run();
      onCorrectedAction({ text, answer, undo: () => api.undoCorrection(answer.override_id), keep });
    } catch (x) {
      setError(`That didn't work. (${String(x)})`);
    } finally {
      setBusy(false);
    }
  };

  /** Undo corrections listed in the panel (every hide, to unhide). */
  const undo = async (text: string, overrideIds: number[]) => {
    setBusy(true);
    setError(null);
    try {
      let answer = null;
      for (const o of overrideIds) answer = await api.undoCorrection(o);
      if (answer) onCorrectedAction({ text, answer, keep: id });
    } catch (x) {
      setError(`Couldn't undo that. (${String(x)})`);
    } finally {
      setBusy(false);
    }
  };

  if (loadError && !e) return <div className="p-4"><InlineError message={`Couldn't load this project. (${String(loadError)})`} onRetry={retry} /></div>;
  if (!e) return (
    <div className="space-y-5 p-4" role="status" aria-label="Loading project">
      <div className="space-y-2"><Skeleton className="h-3 w-16" /><Skeleton className="h-5 w-3/4" /></div>
      <Skeleton className="h-14 w-full" /><Skeleton className="h-20 w-full" />
    </div>
  );

  // While a correction runs or its result loads, the panel shows the old state: nothing in it can be changed yet.
  const locked = busy || loading;
  const saveName = () => {
    const name = draft.trim();
    setEditing(false);
    if (name && name !== e.name) void act(`Renamed to "${name}".`, () => api.renameEntity(id, name));
  };

  return (
    <div className="space-y-4 p-4 text-sm">
      <header>
        <div className="flex items-center gap-2 text-xs text-muted">
          <span aria-hidden className="h-2 w-2 rounded-full" style={{ background: nodeColor({ type: "Project" }) }} />
          Project
          <button type="button" onClick={onCloseAction} aria-label="Close details" className="btn-close ml-auto">×</button>
        </div>
        {editing ? (
          <input autoFocus value={draft} onChange={(x) => setDraft(x.target.value)} onBlur={saveName} onFocus={(x) => x.target.select()}
            aria-label="Project name"
            onKeyDown={(x) => {
              if (x.key === "Enter") saveName();
              if (x.key === "Escape") { x.preventDefault(); setEditing(false); }
            }}
            className="mt-1 w-full rounded border border-accent bg-bg px-2 py-1 text-lg font-semibold outline-none" />
        ) : (
          <h2 className="mt-1 flex items-baseline gap-2 text-lg font-semibold leading-snug break-words">
            {e.name}
            <button type="button" disabled={locked} onClick={() => { setDraft(e.name); setEditing(true); }}
              className="text-xs font-normal text-muted hover:text-accent disabled:opacity-40">Rename</button>
          </h2>
        )}
        {e.aliases.length > 0 && <div className="mt-0.5 text-xs text-muted">Also known as {e.aliases.join(", ")}</div>}
        <YourCorrections e={e} locked={locked} onUndo={(text, ids) => void undo(text, ids)} />
        {e.hidden && !e.corrections.some((c) => c.op === "hide") && (
          <div className="mt-1 text-xs text-muted">Not on the graph: it repeats one of your organizations.</div>
        )}
        {error && <div className="mt-2"><InlineError message={error} /></div>}
      </header>

      <NodeSummary id={id} revision={revision} onSelectAction={onSelectAction} showConnections={false} />

      <ConnectTo id={id} name={e.name} type="Project" onLinkedAction={(other, answer) => onLinkedAction(id, other, answer)} />

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
                <button type="button" onClick={() => onSelectAction(p.id)} className="btn-option min-w-0 flex-1">
                  <span className="truncate">{p.name}</span><span className="shrink-0 text-muted tabular-nums">{p.items} items</span>
                </button>
                <RemoveButton label={`Remove ${p.name} from this project`} disabled={locked}
                  onClick={() => void act(`Removed ${p.name} from "${e.name}".`, () => api.unlinkFromEntity(id, p.id))} />
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
                <button type="button" onClick={() => onSelectAction(g.id)} className="btn-option min-w-0 flex-1">
                  <span className="truncate">{g.name}</span>
                </button>
                <RemoveButton label={`Remove ${g.name} from this project`} disabled={locked}
                  onClick={() => void act(`Removed ${g.name} from "${e.name}".`, () => api.unlinkFromEntity(id, g.id))} />
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
                <button type="button" onClick={() => onSelectAction(v.id)} className="btn-option min-w-0 flex-1">
                  <span className="truncate">{v.name}</span>
                  {v.date && <span className="shrink-0 text-muted">{formatDay(v.date)}</span>}
                </button>
                <RemoveButton label={`Remove ${v.name} from this project`} disabled={locked}
                  onClick={() => void act(`Removed ${v.name} from "${e.name}".`, () => api.unlinkFromEntity(id, v.id))} />
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
                  <span aria-hidden className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: nodeColor(i) }} />
                  <button type="button" onClick={() => onSelectAction(i.id)} className="min-w-0 flex-1 truncate text-left font-medium hover:underline">{i.name}</button>
                  <RemoveButton label={`Remove ${i.name} from this project`} disabled={locked}
                    onClick={() => void act(`Removed 1 item from "${e.name}".`, () => api.unlinkFromEntity(id, i.id))} />
                </div>
                <div className="mt-0.5 text-muted">
                  {[i.why ? WHY[i.why] ?? i.why : null, i.source && sourceLabel(i.source), formatDay(i.date)].filter(Boolean).join(" · ")}
                </div>
                {i.quote && <blockquote className="mt-1 border-l-2 border-line pl-2 leading-relaxed break-words text-soft">{i.quote.trim()}</blockquote>}
              </li>
            ))}
          </ul>
        )}
        {e.counts.items > e.items.length && <p className="mt-2 text-xs text-muted">Showing the {e.items.length} most recent.</p>}
      </Section>

      <Section title="Sources">
        <Sources id={id} revision={revision} highlight={[e.name, ...e.aliases]} />
      </Section>

      <Section title="Fix this project">
        <div className="flex flex-wrap gap-2">
          <button type="button" disabled={locked || e.hidden} onClick={() => void act(`Hid "${e.name}".`, () => api.hideEntity(id))}
            className="btn">Hide</button>
          <button type="button" disabled={locked} onClick={() => setMerging((v) => !v)} className="btn">Merge with…</button>
        </div>
        {merging && (
          <MergePicker self={id} onCancel={() => setMerging(false)} onPick={(n) => {
            setMerging(false);
            void act(`Merged "${n.name}" into "${e.name}".`, () => api.mergeEntity(id, n.id));
          }} />
        )}
      </Section>
    </div>
  );
}
