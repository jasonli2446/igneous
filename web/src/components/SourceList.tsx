"use client";

import { useEffect, useEffectEvent, useId, useLayoutEffect, useRef, useState } from "react";
import { InlineError, Skeleton } from "@/components/ui";
import { api, type SourceCard } from "@/lib/api";
import { formatDay } from "@/lib/dates";
import { useRequest } from "@/lib/hooks";

const SOURCE_LABEL: Record<string, string> = {
  gmail: "Gmail", email: "Email", gcal: "Calendar", gcontacts: "Contacts", maccontacts: "Contacts", vcard: "Contacts",
  drive: "Drive",
  github: "GitHub", fs: "Files", notes: "Notes", imessage: "Messages", ai_chats: "AI chat", takeout: "Takeout", linkedin: "LinkedIn",
};
export const sourceLabel = (s: string) =>
  SOURCE_LABEL[s] ?? (s.startsWith("takeout_") ? `Takeout ${s.slice(8)}` : s);

/** Open the original: an external link, or (for a local file) ask the API to open it in the default app. */
export function OpenButton({ item }: { item: Pick<SourceCard, "id" | "open_url" | "open_label" | "local_file"> }) {
  const [state, setState] = useState<"idle" | "busy" | "failed">("idle");
  if (item.open_url) {
    return <a href={item.open_url} target="_blank" rel="noopener noreferrer" className="btn-chip">Open ↗</a>;
  }
  if (!item.local_file) return null;
  return (
    <button type="button" disabled={state === "busy"} title={state === "failed" ? "Couldn't open the file" : item.open_label ?? "Open file"}
      onClick={async () => {
        setState("busy");
        try { await api.openItem(item.id); setState("idle"); } catch { setState("failed"); }
      }} className="btn-chip">{state === "failed" ? "Can't open" : "Open"}</button>
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
              <span>{formatDay(i.date)}</span>
              {note?.(i) && <span className="truncate">· {note(i)}</span>}
              <span className="ml-auto flex shrink-0 gap-1">
                <OpenButton item={i} />
                <button type="button" onClick={() => setViewing(i.id)} aria-label={`View ${i.title || "this item"}`} className="btn-chip">View</button>
              </span>
            </div>
            <button type="button" onClick={() => setViewing(i.id)} className="mt-1 block w-full truncate text-left font-medium hover:underline">
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

/** Which source items make up a node. Paged; says so when there are none. */
export default function Sources({ id, revision = 0, highlight = [] }: { id: number; revision?: number; highlight?: string[] }) {
  const first = useRequest(id, (signal) => api.nodeSources(id, 0, 6, signal), revision);
  // Pages after the first, for the first page they continue (a reloaded first page starts over).
  const [later, setLater] = useState<{ after: SourceCard[]; items: SourceCard[]; more: boolean } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);

  if (first.error && !first.data) {
    return <InlineError message={`Couldn't load sources. (${String(first.error)})`} onRetry={first.retry} />;
  }
  if (!first.data) {
    return <div className="space-y-1.5" role="status" aria-label="Loading sources"><Skeleton className="h-14 w-full" /><Skeleton className="h-14 w-full" /></div>;
  }
  if (first.data.items.length === 0) return <p className="text-xs text-muted">No source items behind this yet.</p>;
  const base = first.data.items;
  const pages = later?.after === base ? later : null;
  const items = pages ? [...base, ...pages.items] : base;
  const more = pages ? pages.more : first.data.more;

  const showMore = async () => {
    setLoadingMore(true);
    setMoreError(null);
    try {
      const r = await api.nodeSources(id, items.length, 8);
      setLater({ after: base, items: [...(pages?.items ?? []), ...r.items], more: r.more });
    } catch (e) {
      setMoreError(`Couldn't load more sources. (${String(e)})`);
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <div>
      <SourceRows items={items} highlight={highlight} />
      {more && (
        <button type="button" disabled={loadingMore} className="btn-link mt-2" onClick={() => void showMore()}>
          {loadingMore ? "Loading…" : "Show more"}
        </button>
      )}
      {moreError && <div className="mt-1"><InlineError message={moreError} /></div>}
    </div>
  );
}

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Full text of one item, with the terms that matched marked. A modal dialog: the browser keeps focus inside it,
 *  closes it on Escape and returns focus to what opened it. */
export function Reader({ id, highlight, onCloseAction }: { id: number; highlight: string[]; onCloseAction: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const firstMark = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const { data: item, error, retry } = useRequest(id, (signal) => api.item(id, signal));
  const closed = useEffectEvent(onCloseAction);

  // Opens on mount. The cleanup (unmount, or Next hiding this page) closes it without reporting a close: when the
  // page is shown again, it opens again. A close event that arrives while the dialog is open again is stale.
  useLayoutEffect(() => {
    const d = dialog.current!;
    const onClose = () => { if (!d.open) closed(); };
    d.addEventListener("close", onClose);
    d.showModal();
    closeButton.current?.focus();
    return () => {
      d.removeEventListener("close", onClose);
      if (d.open) d.close();
    };
  }, []);

  useEffect(() => { firstMark.current?.scrollIntoView({ block: "center" }); }, [item]);

  const terms = [...new Set(highlight.map((t) => t.trim()).filter((t) => t.length > 2))];
  const parts = item && terms.length
    ? item.text.split(new RegExp(`(${terms.map(esc).join("|")})`, "gi")) : null;
  const lower = new Set(terms.map((t) => t.toLowerCase()));
  const firstIdx = parts ? parts.findIndex((p) => lower.has(p.toLowerCase())) : -1;
  const people = (v: unknown) => Array.isArray(v)
    ? v.map((x) => (typeof x === "object" && x ? ((x as { name?: string; email?: string }).name || (x as { email?: string }).email) : String(x))).filter(Boolean).join(", ")
    : "";

  // Keys pressed in the Reader are its own: Escape closes it, not the panel behind it.
  return (
    <dialog ref={dialog} aria-labelledby={titleId} onKeyDown={(e) => e.stopPropagation()}
      onMouseDown={(e) => { if (e.target === e.currentTarget) dialog.current?.close(); }}
      className="m-auto hidden max-h-[85vh] w-[min(42rem,calc(100%-2rem))] flex-col rounded-lg border border-line bg-panel p-0 text-sm text-ink shadow-xl backdrop:bg-black/40 open:flex">
      <header className="flex items-start gap-3 border-b border-line p-4">
        <div className="min-w-0 flex-1">
          <div className="text-xs text-muted">
            {item ? <>{sourceLabel(item.source)}{item.occurred_at && ` · ${formatDay(item.occurred_at)}`}</> : "Loading…"}
          </div>
          <h2 id={titleId} className="mt-0.5 break-words text-base font-semibold leading-snug">
            {item ? item.title || "(untitled)" : "Source item"}
          </h2>
          {item && ["from", "to", "cc"].map((k) => people(item.meta[k]) && (
            <div key={k} className="mt-0.5 break-words text-xs text-soft"><span className="capitalize">{k}</span> {people(item.meta[k])}</div>
          ))}
        </div>
        {item && <OpenButton item={{ id: item.id, open_url: item.open_url, open_label: item.open_label, local_file: !!item.local_path }} />}
        <button ref={closeButton} type="button" onClick={() => dialog.current?.close()} aria-label="Close" className="btn-close">×</button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {error !== undefined && !item && <InlineError message={`Couldn't load this item. (${String(error)})`} onRetry={retry} />}
        {!item && error === undefined && <div className="space-y-2" role="status" aria-label="Loading item"><Skeleton className="h-3.5 w-full" /><Skeleton className="h-3.5 w-5/6" /><Skeleton className="h-3.5 w-2/3" /></div>}
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
    </dialog>
  );
}
