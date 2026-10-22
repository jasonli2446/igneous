"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { InlineError, Skeleton } from "@/components/ui";
import { api, ItemDetail, SourceCard } from "@/lib/api";

const SOURCE_LABEL: Record<string, string> = {
  gmail: "Gmail", email: "Email", gcal: "Calendar", gcontacts: "Contacts", maccontacts: "Contacts", drive: "Drive",
  github: "GitHub", fs: "Files", notes: "Notes", imessage: "Messages", ai_chats: "AI chat", takeout: "Takeout", linkedin: "LinkedIn",
};
export const sourceLabel = (s: string) =>
  SOURCE_LABEL[s] ?? (s.startsWith("takeout_") ? `Takeout ${s.slice(8)}` : s);

const day = (iso: string | null) =>
  iso ? new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "";

const BTN = "rounded border border-line px-1.5 py-0.5 text-[11px] text-soft hover:bg-hover hover:text-ink disabled:opacity-50";

/** Open the original: an external link, or (for a local file) ask the API to open it in the default app. */
export function OpenButton({ item }: { item: Pick<SourceCard, "id" | "open_url" | "open_label" | "local_file"> }) {
  const [state, setState] = useState<"idle" | "busy" | "failed">("idle");
  if (item.open_url) {
    return <a href={item.open_url} target="_blank" rel="noopener noreferrer" className={BTN}>Open ↗</a>;
  }
  if (!item.local_file) return null;
  return (
    <button disabled={state === "busy"} title={state === "failed" ? "Couldn't open the file" : item.open_label ?? "Open file"}
      onClick={async () => {
        setState("busy");
        try { await api.openItem(item.id); setState("idle"); } catch { setState("failed"); }
      }} className={BTN}>{state === "failed" ? "Can't open" : "Open"}</button>
  );
}

/** Items with their source, date, title and a one-line snippet, each with Open and View. */
export function SourceRows({ items, highlight = [], note }: {
  items: SourceCard[]; highlight?: string[]; note?: (i: SourceCard) => string | null;
}) {
  const [viewing, setViewing] = useState<number | null>(null);
  return (
    <>
      <ul className="space-y-1.5">
        {items.map((i) => (
          <li key={i.id} className="rounded-md border border-line p-2 text-xs">
            <div className="flex items-center gap-1.5 text-muted">
              <span className="rounded bg-hover px-1 py-px text-[10px] text-soft">{sourceLabel(i.source)}</span>
              <span>{day(i.date)}</span>
              {note?.(i) && <span className="truncate">· {note(i)}</span>}
              <span className="ml-auto flex shrink-0 gap-1">
                <OpenButton item={i} />
                <button onClick={() => setViewing(i.id)} className={BTN}>View</button>
              </span>
            </div>
            <button onClick={() => setViewing(i.id)} className="mt-1 block w-full truncate text-left font-medium hover:underline">
              {i.title || "(untitled)"}
            </button>
            {i.snippet && <p className="mt-0.5 line-clamp-1 text-muted">{i.snippet}</p>}
          </li>
        ))}
      </ul>
      {viewing !== null && <Reader id={viewing} highlight={highlight} onCloseAction={() => setViewing(null)} />}
    </>
  );
}

/** Which source items make up a node. Paged; renders nothing when there are none. */
export default function Sources({ id, highlight = [] }: { id: number; highlight?: string[] }) {
  const [items, setItems] = useState<SourceCard[] | null>(null);
  const [more, setMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const request = new AbortController();
    setItems(null);
    setError(null);
    api.nodeSources(id, 0, 6, request.signal)
      .then((r) => { setItems(r.items); setMore(r.more); })
      .catch((e) => { if (!request.signal.aborted) setError(`Couldn't load sources. (${String(e)})`); });
    return () => request.abort();
  }, [id, attempt]);

  if (error) return <InlineError message={error} onRetry={() => setAttempt((a) => a + 1)} />;
  if (!items) return <div className="space-y-1.5" role="status" aria-label="Loading sources"><Skeleton className="h-14 w-full" /><Skeleton className="h-14 w-full" /></div>;
  if (items.length === 0) return <p className="text-xs text-muted">No source items behind this yet.</p>;
  return (
    <div>
      <SourceRows items={items} highlight={highlight} />
      {more && (
        <button disabled={loading} className="mt-2 text-xs text-accent hover:underline disabled:opacity-50"
          onClick={async () => {
            setLoading(true);
            try {
              const r = await api.nodeSources(id, items.length, 8);
              setItems([...items, ...r.items]);
              setMore(r.more);
            } catch (e) { setError(`Couldn't load sources. (${String(e)})`); } finally { setLoading(false); }
          }}>{loading ? "Loading…" : "Show more"}</button>
      )}
    </div>
  );
}

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Full text of one item, with the terms that matched marked. */
export function Reader({ id, highlight, onCloseAction }: { id: number; highlight: string[]; onCloseAction: () => void }) {
  const [item, setItem] = useState<ItemDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const firstMark = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const request = new AbortController();
    setItem(null);
    setError(null);
    api.item(id, request.signal).then(setItem)
      .catch((e) => { if (!request.signal.aborted) setError(`Couldn't load this item. (${String(e)})`); });
    return () => request.abort();
  }, [id, attempt]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onCloseAction(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCloseAction]);

  useEffect(() => { firstMark.current?.scrollIntoView({ block: "center" }); }, [item]);

  const terms = [...new Set(highlight.map((t) => t.trim()).filter((t) => t.length > 2))];
  const parts = item && terms.length
    ? item.text.split(new RegExp(`(${terms.map(esc).join("|")})`, "gi")) : null;
  const lower = new Set(terms.map((t) => t.toLowerCase()));
  const firstIdx = parts ? parts.findIndex((p) => lower.has(p.toLowerCase())) : -1;
  const people = (v: unknown) => Array.isArray(v)
    ? v.map((x) => (typeof x === "object" && x ? ((x as { name?: string; email?: string }).name || (x as { email?: string }).email) : String(x))).filter(Boolean).join(", ")
    : "";

  // Portaled: the details panel is transformed, which would otherwise trap a fixed overlay inside it.
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onMouseDown={(e) => { if (e.target === e.currentTarget) onCloseAction(); }}>
      <div role="dialog" aria-modal="true" aria-label={item?.title ?? "Source item"}
        className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-lg border border-line bg-panel text-sm shadow-xl">
        <header className="flex items-start gap-3 border-b border-line p-4">
          <div className="min-w-0 flex-1">
            <div className="text-xs text-muted">
              {item ? <>{sourceLabel(item.source)}{item.occurred_at && ` · ${day(item.occurred_at)}`}</> : "Loading…"}
            </div>
            <h2 className="mt-0.5 break-words text-base font-semibold leading-snug">{item?.title || (item ? "(untitled)" : "")}</h2>
            {item && ["from", "to", "cc"].map((k) => people(item.meta[k]) && (
              <div key={k} className="mt-0.5 break-words text-xs text-soft"><span className="capitalize">{k}</span> {people(item.meta[k])}</div>
            ))}
          </div>
          {item && <OpenButton item={{ id: item.id, open_url: item.open_url, open_label: item.open_label, local_file: !!item.local_path }} />}
          <button onClick={onCloseAction} aria-label="Close" className="rounded px-1.5 text-lg leading-none text-muted hover:bg-hover">×</button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {error && <InlineError message={error} onRetry={() => setAttempt((a) => a + 1)} />}
          {!item && !error && <div className="space-y-2" role="status" aria-label="Loading item"><Skeleton className="h-3.5 w-full" /><Skeleton className="h-3.5 w-5/6" /><Skeleton className="h-3.5 w-2/3" /></div>}
          {item && !item.text && <p className="text-xs text-muted">This item has no readable text.</p>}
          {item && item.text && (
            <pre className="font-sans text-[13px] leading-relaxed whitespace-pre-wrap break-words text-soft">
              {parts ? parts.map((p, i) => lower.has(p.toLowerCase())
                ? <mark key={i} ref={i === firstIdx ? firstMark : undefined}
                    className="rounded bg-accent/25 px-0.5 text-ink">{p}</mark>
                : p) : item.text}
            </pre>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
