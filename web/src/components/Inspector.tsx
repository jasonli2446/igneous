"use client";

import { useState } from "react";
import ConnectTo from "@/components/ConnectTo";
import EntityInspector from "@/components/EntityInspector";
import NodeSummary from "@/components/NodeSummary";
import Sources, { participants } from "@/components/SourceList";
import { InlineError, Section, Skeleton } from "@/components/ui";
import { api, type Identity, type LinkAdded, type Thing } from "@/lib/api";
import type { Corrected } from "@/lib/corrections";
import { formatDayTime } from "@/lib/dates";
import { nodeColor, typeLabel } from "@/lib/graph-style";
import { useLiveSearch, useRequest } from "@/lib/hooks";

type Props = {
  id: number;
  /** Bumped after every correction: the panel loads again to show its result. */
  revision: number;
  onExpandAction: (id: number) => void | Promise<void>;
  onCloseAction: () => void;
  /** Whether this node's connections are currently shown on the graph (so the button says Show or Hide). */
  expanded: boolean;
  onSelectAction: (id: number) => void;
  onWhyAction: (a: number, b: number) => void;
  onLinkedAction: (from: number, other: Thing, answer: LinkAdded) => void;
  onCorrectedAction: (c: Corrected) => void;
};

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** An identity's key as a person reads it: an address or a number as is, a profile by where it lives. */
function identityLabel(key: string) {
  const colon = key.indexOf(":");
  const [kind, rest] = colon > 0 ? [key.slice(0, colon), key.slice(colon + 1)] : [key, ""];
  if (kind === "linkedin" && rest) return `LinkedIn profile ${rest}`;
  if (kind === "github" && rest) return `GitHub account ${rest}`;
  return key;
}

function InspectorSkeleton() {
  return (
    <div className="space-y-5 p-4" role="status" aria-label="Loading details">
      <div className="space-y-2">
        <Skeleton className="h-3 w-16" />
        <Skeleton className="h-5 w-3/4" />
        <Skeleton className="h-3 w-1/2" />
      </div>
      <div className="space-y-2 border-t border-line pt-4">
        <Skeleton className="h-3 w-full" /><Skeleton className="h-3 w-5/6" /><Skeleton className="h-3 w-2/3" />
      </div>
      <Skeleton className="h-20 w-full" />
    </div>
  );
}

export default function Inspector({ id, revision, onExpandAction, onCloseAction, onSelectAction, onWhyAction,
  onCorrectedAction, onLinkedAction, expanded }: Props) {
  const { data: node, error, retry } = useRequest(id, (signal) => api.node(id, signal), revision);
  const [busy, setBusy] = useState(false);
  const [fullText, setFullText] = useState(false);
  const [showOriginal, setShowOriginal] = useState(false);

  if (error && !node) return <div className="p-4"><InlineError message={`Couldn't load details. (${String(error)})`} onRetry={retry} /></div>;
  if (!node) return <InspectorSkeleton />;
  if (node.type === "Project") {
    return <EntityInspector id={node.id} revision={revision} onSelectAction={onSelectAction} onCloseAction={onCloseAction}
      onCorrectedAction={onCorrectedAction} onLinkedAction={onLinkedAction} />;
  }

  const src = node.source_item;
  const body = src?.text ?? "";
  const long = body.length > 600;

  return (
    <div className="space-y-4 p-4 text-sm">
      <header>
        <div className="flex items-center gap-2 text-xs text-muted">
          <span aria-hidden className="h-2 w-2 rounded-full" style={{ background: nodeColor(node) }} />
          {typeLabel(node.type, node.props)}
          <button type="button" onClick={onCloseAction} aria-label="Close details" className="btn-close ml-auto">×</button>
        </div>
        <h2 className="mt-1 text-lg font-semibold leading-snug break-words">{node.name}</h2>
        {EMAIL.test(node.key) && node.key.toLowerCase() !== node.name.toLowerCase() && (
          <div className="mt-0.5 text-xs break-all text-muted">{node.key}</div>)}
      </header>

      <NodeSummary id={node.id} revision={revision} onSelectAction={onSelectAction} onWhyAction={onWhyAction} />

      <button type="button" disabled={busy} aria-pressed={expanded}
        onClick={async () => { setBusy(true); try { await onExpandAction(node.id); } finally { setBusy(false); } }}
        className={`btn inline-flex items-center gap-2 ${expanded ? "border-accent/50 bg-accent/10 hover:bg-accent/15" : ""}`}>
        {busy && <span aria-hidden className="h-3 w-3 animate-spin rounded-full border-2 border-accent border-t-transparent" />}
        {busy ? (expanded ? "Hiding…" : "Finding connections…")
          : expanded ? "Hide their connections" : "Show their connections on the graph"}
      </button>

      <div className="flex flex-wrap items-start gap-2">
        <ConnectTo id={node.id} name={node.name} type={node.type}
          onLinkedAction={(other, answer) => onLinkedAction(node.id, other, answer)} />
        {node.type === "Person" && <SamePerson id={node.id} name={node.name} onMergedAction={onCorrectedAction} />}
      </div>

      {node.identities.length > 0 && (
        <Section title="Also known as" count={node.identities.length}>
          <ul className="space-y-1">
            {node.identities.map((i) => (
              <IdentityRow key={i.key} person={node.id} name={node.name} identity={i} onSplitAction={onCorrectedAction} />
            ))}
          </ul>
        </Section>
      )}

      <Section title="Sources">
        <Sources id={node.id} revision={revision} highlight={[node.name]} />
      </Section>

      {src && (
        <Section title="Original">
          <button type="button" onClick={() => setShowOriginal((v) => !v)} aria-expanded={showOriginal} className="btn-link">
            {showOriginal ? "Hide original" : "Show original"}
          </button>
          {showOriginal && <>
            <div className="space-y-0.5 text-xs text-soft">
              {src.occurred_at && <div>{formatDayTime(src.occurred_at)}</div>}
              {participants(src.meta, "from") && <div className="break-words">From {participants(src.meta, "from")}</div>}
              {participants(src.meta, "to") && <div className="break-words">To {participants(src.meta, "to")}</div>}
            </div>
            <pre className={`mt-2 overflow-y-auto rounded-md bg-bg p-2.5 text-xs leading-relaxed whitespace-pre-wrap text-soft ${fullText ? "max-h-96" : "max-h-32 overflow-hidden"}`}>{body}</pre>
            {long && (
              <button type="button" onClick={() => setFullText((v) => !v)} className="btn-link mt-1.5">
                {fullText ? "Show less" : "Show full text"}
              </button>
            )}
          </>}
        </Section>
      )}
    </div>
  );
}

/** One identity merged into this person, with "Not the same person": after you confirm, it becomes a person of its
 *  own on every rebuild, whatever joined them. */
function IdentityRow({ person, name, identity, onSplitAction }: {
  person: number; name: string; identity: Identity; onSplitAction: (c: Corrected) => void;
}) {
  const [state, setState] = useState<"idle" | "confirm" | "busy">("idle");
  const [error, setError] = useState<string | null>(null);
  const label = identityLabel(identity.key);
  const split = () => {
    setState("busy");
    setError(null);
    api.splitPerson(person, identity.key).then((answer) => {
      setState("idle");
      onSplitAction({ text: `${label} is no longer ${name}.`, answer, undo: () => api.undoCorrection(answer.override_id) });
    }).catch((e) => { setState("confirm"); setError(`Couldn't separate them. (${String(e)})`); });
  };
  return (
    <li className="rounded-md border border-line p-2 text-xs">
      <div className="flex items-start gap-2">
        <span className="min-w-0 flex-1 break-words">
          <span className="text-soft">{label}</span>
          {identity.name && identity.name !== name && <span className="text-muted"> · {identity.name}</span>}
        </span>
        {state === "idle" && (
          <button type="button" onClick={() => setState("confirm")} className="shrink-0 text-muted hover:text-danger hover:underline">
            Not the same person
          </button>
        )}
      </div>
      {state !== "idle" && (
        <div role="group" aria-label={`Separate ${label}`} className="mt-2 space-y-1.5">
          <p className="text-soft">Separate {label} from {name}? It becomes a person of its own on every rebuild.</p>
          {error && <InlineError message={error} />}
          <div className="flex gap-2">
            <button type="button" disabled={state === "busy"} onClick={split} className="btn">Separate them</button>
            <button type="button" disabled={state === "busy"} onClick={() => { setState("idle"); setError(null); }}
              className="text-muted hover:text-ink">Keep together</button>
          </div>
        </div>
      )}
    </li>
  );
}

/** "Same person as…": fold another person (a nickname contact, an old address) into this one, for good, once you
 *  confirm. "Not the same person" on the merged identity, or Undo, separates them again. */
function SamePerson({ id, name, onMergedAction }: { id: number; name: string; onMergedAction: (c: Corrected) => void }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<Thing | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const people = useLiveSearch(open && !picked ? q : "",
    (text, signal) => api.findThings(text, { types: ["Person"] }, signal).then((r) => r.results.filter((n) => n.id !== id)));
  const close = () => { setOpen(false); setQ(""); setPicked(null); setError(null); };

  if (!open) return <button type="button" onClick={() => setOpen(true)} className="btn">Same person as…</button>;

  const merge = (other: Thing) => {
    setSaving(true);
    setError(null);
    api.mergePeople(id, other.id).then((answer) => {
      close();
      onMergedAction({ text: `${other.name} and ${name} are one person now.`, answer, keep: answer.person,
        undo: () => api.undoCorrection(answer.override_id) });
    }).catch((e) => setError(`Couldn't merge them. (${String(e)})`)).finally(() => setSaving(false));
  };

  return (
    <div className="w-full rounded-md border border-line p-2 text-xs">
      {picked ? (
        <div role="group" aria-label="Confirm same person" className="space-y-2">
          <p className="text-soft">
            Is <span className="font-medium text-ink">{picked.name}</span> the same person as{" "}
            <span className="font-medium text-ink">{name}</span>? They become one person on every rebuild.
          </p>
          {error && <InlineError message={error} />}
          <div className="flex gap-2">
            <button type="button" disabled={saving} onClick={() => merge(picked)} className="btn">Yes, merge them</button>
            <button type="button" disabled={saving} onClick={() => setPicked(null)} className="text-muted hover:text-ink">Back</button>
          </div>
        </div>
      ) : (
        <>
          <div className="flex items-center gap-2">
            <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find the other name or address"
              aria-label="Search people to merge" onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); close(); } }}
              className="field flex-1" />
            <button type="button" onClick={close} className="text-muted hover:text-ink">Cancel</button>
          </div>
          {people.active && (
            <ul className="mt-2 space-y-0.5">
              {people.results?.map((n) => (
                <li key={n.id}>
                  <button type="button" className="btn-option" onClick={() => setPicked(n)}>
                    <span className="truncate">{n.name}</span><span className="shrink-0 text-muted">{n.context}</span>
                  </button>
                </li>
              ))}
              {people.results?.length === 0 && (
                <li className="px-1.5 py-1 text-muted">{people.failed ? "Search failed." : "No one else matches."}</li>
              )}
              {people.results === null && <li className="px-1.5 py-1 text-muted">Searching…</li>}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
