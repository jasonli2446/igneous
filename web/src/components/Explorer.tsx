"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useEffectEvent, useId, useMemo, useRef, useState } from "react";
import Inspector, { type Corrected } from "@/components/Inspector";
import { InlineError, TopProgress } from "@/components/ui";
import WhyConnected from "@/components/WhyConnected";
import { API, ApiError, api, type GEdge, type GNode, type NodeDetail, type Subgraph, type Thing } from "@/lib/api";
import { nodeColor, sectionOf } from "@/lib/graph-style";
import { useLiveSearch } from "@/lib/hooks";

// WebGL libraries touch window at import time, so they can't render on the server.
const Graph3D = dynamic(() => import("@/components/Graph3D"), { ssr: false });

/** Something added on top of the centre's view, hanging off the node `by`: an expansion (`by`'s connections), a node
 *  opened from the panel, or a link you made. Layers apply in the order they were added. */
type Layer = { key: string; kind: "expand" | "open" | "link"; by: number; nodes: GNode[]; edges: GEdge[] };
/** The centre (the API's seed), its view, and the layers on top. */
type Shown = { seed: number; base: Subgraph; layers: Layer[] };
type Toast = { text: string; undo?: () => Promise<unknown> };
/** What went wrong, and what Retry does again. */
type Failure = { text: string; retry?: () => void };
/** How a new centre changes the trail: added to it, or back to the previous one. */
type Step = "push" | "back";

const EMPTY: Subgraph = { nodes: [], edges: [] };
const edgeKey = (e: GEdge) => `${e.src}-${e.dst}-${e.type}`;
/** A node the inspector read, as the graph draws it. */
const asNode = ({ id, type, name, key, category }: NodeDetail): GNode => ({ id, type, name, key, category });

/** What is on screen: the centre's view plus every layer whose node is still shown, each node and link once. */
function compose(base: Subgraph, layers: Layer[]) {
  const nodes = new Map(base.nodes.map((n) => [n.id, n]));
  const edges = new Map(base.edges.map((e) => [edgeKey(e), e]));
  const live: Layer[] = [];
  for (const layer of layers) {
    if (!nodes.has(layer.by)) continue;
    live.push(layer);
    for (const n of layer.nodes) if (!nodes.has(n.id)) nodes.set(n.id, n);
    for (const e of layer.edges) if (!edges.has(edgeKey(e))) edges.set(edgeKey(e), e);
  }
  const data = { nodes: [...nodes.values()], edges: [...edges.values()].filter((e) => nodes.has(e.src) && nodes.has(e.dst)) };
  return { data, live };
}

/** Hide what `id` revealed (its expansion and the nodes opened from its panel), then whatever hung off those. Links
 *  you made stay while both ends are shown. */
function fold(base: Subgraph, layers: Layer[], id: number) {
  let rest = layers.filter((l) => l.by !== id || l.kind === "link");
  for (;;) {
    const { live } = compose(base, rest);
    if (live.length === rest.length) return rest;
    rest = live;
  }
}

/** The replayed layers in the current order: a layer added while replaying stays as it is, one removed meanwhile
 *  stays removed, and one whose node left the graph is gone. */
function merge(current: Layer[], replayed: Layer[], before: Layer[]) {
  const fresh = new Map(replayed.map((l) => [l.key, l]));
  const had = new Set(before.map((l) => l.key));
  return current.flatMap((l) => fresh.get(l.key) ?? (had.has(l.key) ? [] : [l]));
}

export default function Explorer() {
  const router = useRouter();
  const inputId = useId();
  const listId = useId();
  const searchRef = useRef<HTMLInputElement>(null);

  const [shown, setShown] = useState<Shown | null>(null);
  // Where you've been centred, oldest first; the last is the current centre.
  const [trail, setTrail] = useState<GNode[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [why, setWhy] = useState<{ a: number; b: number } | null>(null);
  const [loading, setLoading] = useState(true); // the first view starts loading on mount
  const [error, setError] = useState<Failure | null>(null);
  // Why there is no one to open on, when the API's opening view is empty: nothing is built yet, or no source says
  // which person is you.
  const [empty, setEmpty] = useState<"nothing_built" | "no_owner" | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  // Bumped by every correction and undo, so the open panel shows the result.
  const [revision, setRevision] = useState(0);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);

  // A new centre bumps the generation and aborts every request in flight: no reply for an older centre ever lands.
  const generation = useRef(0);
  const inFlight = useRef<Record<"seed" | "expand" | "refresh" | "open", AbortController | null>>(
    { seed: null, expand: null, refresh: null, open: null });
  // The graph version the centre's view was built at; the poll below refreshes the view when the API's moves on.
  const builtAt = useRef<{ seed: number; version: string } | null>(null);
  const self = useRef<number | null>(null); // your own node: its graph lives at "/", every other centre at /?seed=
  const enterFor = useRef<string | null>(null); // Enter was pressed while this query's matches were on their way
  const started = useRef(false);

  const { data, live } = useMemo(() => (shown ? compose(shown.base, shown.layers) : { data: EMPTY, live: [] }), [shown]);
  const expanded = useMemo(() => new Set(live.filter((l) => l.kind === "expand").map((l) => l.by)), [live]);
  const seed = shown?.seed;
  const current = trail[trail.length - 1];
  const previous = trail[trail.length - 2];

  /** Centre the graph on `id` (null: you). Every caller sets the loading state first: see go(). */
  async function load(id: number | null, step: Step, open = false) {
    const mine = ++generation.current;
    for (const r of Object.values(inFlight.current)) r?.abort();
    const request = new AbortController();
    inFlight.current.seed = request;
    enterFor.current = null;
    try {
      const { version } = await api.graphVersion(request.signal);
      const g = await api.view(id ?? undefined, request.signal);
      if (mine !== generation.current) return;
      if (g.empty) {
        setShown(null);
        setEmpty(g.reason);
        return;
      }
      const next: Shown = { seed: g.seed, base: { nodes: g.nodes, edges: g.edges }, layers: [] };
      if (id === null) self.current = g.seed;
      builtAt.current = { seed: g.seed, version };
      setShown(next);
      setSelected(open ? g.seed : null);
      setWhy(null);
      setQuery("");
      // The API may centre on another identity of the same person (merged addresses): the trail names what it chose.
      const centre = g.nodes.find((n) => n.id === g.seed);
      if (centre) {
        setTrail((t) => (step === "push" ? [...t.filter((x) => x.id !== centre.id), centre].slice(-6) : [...t.slice(0, -2), centre]));
      }
      // A centre you searched for opens already showing what it's made of, and its panel.
      if (open) await expand(g.seed, next);
    } catch (e) {
      if (request.signal.aborted) return;
      setError({ text: `Couldn't load the graph. Check that the API is running at ${API}. (${String(e)})`, retry: () => go(id, step, open) });
    } finally {
      if (inFlight.current.seed === request) {
        inFlight.current.seed = null;
        setLoading(false);
      }
    }
  }

  /** load() from an event: shows the loading state at once. */
  function go(id: number | null, step: Step, open = false) {
    setLoading(true);
    setError(null);
    setEmpty(null);
    void load(id, step, open);
  }

  // Next keeps this page alive while you use the other tabs and runs its effects again when you come back. Only the
  // first mount loads (the centre the URL names, or you), so the graph is as you left it when you return.
  const openFirst = useEffectEvent(() => {
    const asked = Number(new URLSearchParams(window.location.search).get("seed"));
    void load(Number.isSafeInteger(asked) && asked > 0 ? asked : null, "push");
  });
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    openFirst();
  }, []);

  // The URL names the centre, so a reload or a shared link opens the same graph. It is written again when this
  // page is shown, since the Graph tab links to plain "/".
  useEffect(() => {
    if (seed === undefined) return;
    const url = seed === self.current ? "/" : `/?seed=${seed}`;
    if (window.location.pathname + window.location.search !== url) window.history.replaceState(null, "", url);
  }, [seed]);

  /** Reveal a node's top connections. `from` is what's shown when the caller can't wait for a render (a new centre). */
  async function expand(id: number, from: Shown | null = shown) {
    if (!from) return;
    const mine = generation.current;
    inFlight.current.expand?.abort();
    const request = new AbortController();
    inFlight.current.expand = request;
    setError(null);
    try {
      const visible = compose(from.base, from.layers).data.nodes.map((n) => n.id);
      const got = await api.expandNode(from.seed, id, visible, request.signal);
      // A group whose members are all on screen returns only links (no new nodes): keep them, they're the point.
      if (mine !== generation.current || (!got.nodes.length && !got.edges.length)) return;
      const layer: Layer = { key: `x${id}`, kind: "expand", by: id, nodes: got.nodes, edges: got.edges };
      setShown((s) => (s && s.seed === from.seed && !s.layers.some((l) => l.key === layer.key)
        ? { ...s, layers: [...s.layers, layer] } : s));
    } catch (e) {
      if (!request.signal.aborted) setError({ text: `Couldn't show the connections. (${String(e)})`, retry: () => void expand(id) });
    } finally {
      if (inFlight.current.expand === request) inFlight.current.expand = null;
    }
  }

  // Click a node: reveal its top connections; click it again to fold them back.
  async function toggle(id: number, mode: "open" | "toggle" = "toggle") {
    if (!expanded.has(id)) return expand(id);
    if (mode === "toggle") setShown((s) => (s ? { ...s, layers: fold(s.base, s.layers, id) } : s));
  }

  /** Bring what's on screen up to date in place: the centre's view again, then each layer in order (an expansion
   *  asks the API again; an opened node or a link you made stays while its node exists). The selection, the panel
   *  and every position stay. */
  async function refresh() {
    const from = shown;
    if (!from) return;
    const mine = generation.current;
    inFlight.current.refresh?.abort();
    const request = new AbortController();
    inFlight.current.refresh = request;
    try {
      const { version } = await api.graphVersion(request.signal);
      const view = await api.view(from.seed, request.signal);
      const base = { nodes: view.nodes, edges: view.edges };
      const layers: Layer[] = [];
      for (const layer of from.layers) {
        const on = compose(base, layers).data.nodes;
        if (!on.some((n) => n.id === layer.by)) continue;
        if (layer.kind === "expand") {
          const got = await api.expandNode(from.seed, layer.by, on.map((n) => n.id), request.signal);
          if (got.nodes.length || got.edges.length) layers.push({ ...layer, nodes: got.nodes, edges: got.edges });
        } else {
          const [node] = layer.nodes;
          const now = await api.node(node.id, request.signal).catch((e: unknown) => {
            if (e instanceof ApiError && e.status === 404) return null; // it left the graph
            throw e;
          });
          if (now) layers.push({ ...layer, nodes: [{ ...node, ...asNode(now) }] });
        }
      }
      if (mine !== generation.current) return;
      builtAt.current = { seed: from.seed, version };
      setShown((s) => (s && s.seed === from.seed ? { ...s, base, layers: merge(s.layers, layers, from.layers) } : s));
    } catch (e) {
      if (!request.signal.aborted) setError({ text: `Couldn't refresh the graph. (${String(e)})`, retry: () => void refresh() });
    } finally {
      if (inFlight.current.refresh === request) inFlight.current.refresh = null;
    }
  }

  // The graph is long-lived: check the API's change token now and every 30 s (only while this page is shown), and
  // refresh the graph and the open panel in place when it moved, e.g. after a rebuild on the Sources page.
  const checkVersion = useEffectEvent(async (signal: AbortSignal) => {
    const built = builtAt.current;
    if (!built || built.seed !== seed) return;
    const { version } = await api.graphVersion(signal);
    if (signal.aborted || builtAt.current !== built || version === built.version) return;
    setRevision((n) => n + 1);
    await refresh();
  });
  useEffect(() => {
    if (seed === undefined) return;
    const request = new AbortController();
    // A transient API outage must not disturb an open graph: the next check tries again.
    const check = () => { checkVersion(request.signal).catch(() => {}); };
    check();
    const timer = window.setInterval(check, 30_000);
    return () => { window.clearInterval(timer); request.abort(); };
  }, [seed]);

  // A connection picked in the panel joins the graph you're on, linked to what you were looking at: only a search
  // or Back changes the centre.
  async function openFromPanel(id: number, from: number | null = selected) {
    setWhy(null);
    if (data.nodes.some((n) => n.id === id)) return setSelected(id);
    const at = shown;
    if (!at) return;
    const anchor = from !== null && data.nodes.some((n) => n.id === from) ? from : at.seed;
    const mine = generation.current;
    inFlight.current.open?.abort();
    const request = new AbortController();
    inFlight.current.open = request;
    try {
      const node = asNode(await api.node(id, request.signal));
      if (mine !== generation.current) return;
      const layer: Layer = {
        key: `o${node.id}`, kind: "open", by: anchor, nodes: [{ ...node, expandable: true, strength: 0.5 }],
        edges: [{ id: -(700000 + node.id), src: anchor, dst: node.id, type: "RELATED", weight: 1, origin: "derived", stated: false }],
      };
      setShown((s) => (s && s.seed === at.seed ? { ...s, layers: [...s.layers.filter((l) => l.key !== layer.key), layer] } : s));
      setSelected(node.id);
    } catch (e) {
      if (!request.signal.aborted) setError({ text: `Couldn't open that. (${String(e)})`, retry: () => void openFromPanel(id, from) });
    } finally {
      if (inFlight.current.open === request) inFlight.current.open = null;
    }
  }

  // A connection you added shows up now: the other end joins the graph if it wasn't already there.
  function linked(node: GNode, edge: GEdge) {
    const by = edge.src === node.id ? edge.dst : edge.src;
    const layer: Layer = { key: `l${edge.id}`, kind: "link", by, nodes: [node], edges: [edge] };
    setShown((s) => (s ? { ...s, layers: [...s.layers.filter((l) => l.key !== layer.key), layer] } : s));
  }

  /** After a correction: say what changed (with Undo when it can be undone), show the result in the open panel, and
   *  bring the graph up to date in place. `keep` is the node to stay on when the correction moved it (a merge). */
  function corrected({ text, undo, keep }: Corrected) {
    setToast({ text, undo });
    if (keep !== undefined) setSelected(keep);
    setRevision((n) => n + 1);
    void refresh();
  }

  async function undoLast() {
    const t = toast;
    if (!t?.undo) return;
    setToast(null);
    try {
      await t.undo();
      setRevision((n) => n + 1);
      await refresh();
    } catch (e) {
      setError({ text: `Couldn't undo that. (${String(e)})` });
    }
  }

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 8000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  // Search: people, projects, groups, events and the like, best match first. Messages live behind the last option.
  const search = useLiveSearch(query, (q, signal) => api.findThings(q, { seed }, signal).then((r) => r.results), {
    delay: 150,
    onResults: (q, results) => {
      // Enter pressed while typing: open the best match for what was typed, now that it's known.
      if (enterFor.current !== q) return;
      enterFor.current = null;
      if (results.length) go(results[0].id, "push", true);
    },
  });
  const matches: Thing[] = search.results ?? [];
  const searching = search.active && search.results === null;
  const listOpen = search.active;
  const optionCount = matches.length + 1;
  const optionId = (i: number) => `${listId}-${i}`;
  const searchMessages = () => router.push(`/search?q=${encodeURIComponent(search.q)}`);
  const choose = (i: number) => (i < matches.length ? go(matches[i].id, "push", true) : searchMessages());
  const groups: { section: string; items: { m: Thing; i: number }[] }[] = [];
  matches.forEach((m, i) => {
    const section = sectionOf(m) ?? m.type;
    const last = groups[groups.length - 1];
    if (last?.section === section) last.items.push({ m, i });
    else groups.push({ section, items: [{ m, i }] });
  });

  const onSearchKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      enterFor.current = null;
      setQuery("");
      e.currentTarget.blur();
    } else if (e.key === "ArrowDown" && listOpen) {
      e.preventDefault();
      setActive((i) => (i + 1) % optionCount);
    } else if (e.key === "ArrowUp" && listOpen) {
      e.preventDefault();
      setActive((i) => (i - 1 + optionCount) % optionCount);
    } else if (e.key === "Enter" && listOpen) {
      e.preventDefault();
      if (searching) enterFor.current = search.q;
      else choose(Math.min(active, optionCount - 1));
    }
  };

  // "/" jumps to search and Escape closes the panel, unless a field or the Reader already took the key.
  const pageKey = useEffectEvent((e: KeyboardEvent) => {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target instanceof Element && e.target.closest("input, textarea, select, [contenteditable]")) return;
    if (e.key === "/") {
      e.preventDefault();
      searchRef.current?.focus();
    } else if (e.key === "Escape") {
      setWhy(null);
      setSelected(null);
    }
  });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => pageKey(e);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const back = () => { if (previous) go(previous.id, "back"); };
  const panelOpen = why !== null || selected !== null;
  const closePanel = () => { setWhy(null); setSelected(null); };
  // While the panel is open on a wide screen, the overlays keep clear of it.
  const besidePanel = panelOpen ? "md:right-[24.75rem]" : "";

  return (
    <main className="relative flex-1 overflow-hidden bg-bg text-ink">
      <h1 className="sr-only">Graph</h1>
      <p role="status" className="sr-only">
        {current && data.nodes.length > 0 ? `Centred on ${current.name}, ${data.nodes.length} on the graph.` : ""}
      </p>
      <TopProgress active={loading} />

      {seed !== undefined && data.nodes.length > 0 && (
        <Graph3D data={data} seed={seed} selected={selected}
          onSelectAction={(id) => { setSelected(id); setWhy(null); }}
          onExpandAction={toggle}
          onLinkAction={(a, b) => setWhy({ a: a.id, b: b.id })} />
      )}

      {data.nodes.length === 0 && !loading && !error && empty === "nothing_built" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center text-sm text-muted">
          <p className="text-base text-ink">Your graph is empty</p>
          <p className="max-w-sm">Connect a source, then build the graph. Setup takes a few minutes and everything stays on this computer.</p>
          <Link href="/settings" className="btn-primary">Connect your first source</Link>
        </div>
      )}

      {data.nodes.length === 0 && !loading && !error && empty === "no_owner" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center text-sm text-muted">
          <p className="text-base text-ink">Nobody to start from yet</p>
          <p className="max-w-sm">No source says which person is you, or nothing links you to anyone yet. Press / to search for someone to start from, or connect one of your own accounts and build the graph. Everything stays on this computer.</p>
          <Link href="/settings" className="btn-primary">Open Sources</Link>
        </div>
      )}

      {data.nodes.length === 0 && !loading && !error && empty === null && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 px-6 text-center text-sm text-muted">
          <p className="text-base text-ink">Nothing to show yet</p>
          <p>Search for a person, conversation, or event to start. Press / to search.</p>
        </div>
      )}

      {/* Top row: where you are, and search. */}
      <div className={`pointer-events-none absolute left-3 right-3 top-3 z-20 flex items-start gap-2 transition-[right] duration-200 ${besidePanel}`}>
        {current && (
          // Whose graph this is. Only searching changes it; when you're centred on someone else, one click goes
          // back to where you were.
          <div className="pointer-events-auto flex min-w-0 max-w-[40%] shrink-0 items-center gap-2 rounded-md border border-line bg-panel/95 px-2.5 py-1.5 text-xs">
            <span className="h-2 w-2 shrink-0 rounded-full bg-ink" aria-hidden />
            <span className="truncate"><span className="text-muted">Centred on </span><span className="font-medium">{current.name}</span></span>
            {previous && (
              <button type="button" onClick={back} aria-label={`Back to ${previous.name}`} title={`Back to ${previous.name}`}
                className="shrink-0 rounded px-1.5 py-0.5 text-muted hover:bg-hover hover:text-ink">
                Back to {previous.name.split(" ")[0]}
              </button>
            )}
          </div>
        )}

        <div className="pointer-events-auto relative mx-auto min-w-0 max-w-md flex-1">
          <div className="flex items-center gap-2 rounded-md border border-line bg-panel/95 px-3 py-1.5 text-sm focus-within:border-accent">
            <label htmlFor={inputId} className="sr-only">Search the graph</label>
            <input id={inputId} ref={searchRef} value={query} onKeyDown={onSearchKey}
              onChange={(e) => {
                setQuery(e.target.value);
                setActive(0);
                if (enterFor.current !== e.target.value.trim()) enterFor.current = null;
              }}
              role="combobox" aria-expanded={listOpen} aria-controls={listId} aria-autocomplete="list"
              aria-activedescendant={listOpen ? optionId(Math.min(active, optionCount - 1)) : undefined}
              autoComplete="off" placeholder="Find a person, project, group or event"
              className="min-w-0 flex-1 bg-transparent outline-none placeholder:text-muted" />
            <kbd className="rounded border border-line px-1.5 text-[11px] text-muted">/</kbd>
          </div>
          {listOpen && (
            <div className="absolute z-30 mt-1 max-h-96 w-full overflow-y-auto rounded-md border border-line bg-panel py-1 shadow-lg">
              {matches.length === 0 && (
                <p role="status" className="px-3 py-2 text-xs text-muted">
                  {searching ? "Searching…" : search.failed ? "Search failed. Check that the API is running."
                    : `No people, projects or groups match “${search.q}”.`}
                </p>
              )}
              {/* Options keep focus in the box (mouse down doesn't take it); the arrow keys move between them. */}
              <div id={listId} role="listbox" aria-label="Matches">
                {groups.map((g) => (
                  <div key={`${g.section}-${g.items[0].i}`} role="group" aria-labelledby={`${listId}-g${g.items[0].i}`}>
                    <div role="presentation" id={`${listId}-g${g.items[0].i}`} className="px-3 pt-1.5 pb-0.5 text-[11px] font-medium text-muted">
                      {g.section}
                    </div>
                    {g.items.map(({ m, i }) => (
                      <div key={m.id} id={optionId(i)} role="option" aria-selected={i === active}
                        onMouseDown={(e) => e.preventDefault()} onClick={() => choose(i)} onMouseEnter={() => setActive(i)}
                        className={`flex cursor-pointer items-center gap-2.5 px-3 py-1.5 ${i === active ? "bg-hover" : ""}`}>
                        <span aria-hidden className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: nodeColor(m) }} />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm">{m.name}</span>
                          <span className="block truncate text-xs text-muted">{m.context}</span>
                        </span>
                      </div>
                    ))}
                  </div>
                ))}
                <div id={optionId(matches.length)} role="option" aria-selected={active === matches.length}
                  onMouseDown={(e) => e.preventDefault()} onClick={searchMessages} onMouseEnter={() => setActive(matches.length)}
                  className={`flex cursor-pointer items-center gap-2 px-3 py-1.5 text-sm ${matches.length ? "mt-1 border-t border-line pt-1.5" : ""} ${active === matches.length ? "bg-hover" : ""}`}>
                  <span aria-hidden className="text-muted">&#8981;</span>
                  <span className="truncate">Search messages for &ldquo;{search.q}&rdquo;</span>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {error && (
        <div className={`pointer-events-none absolute left-3 right-3 top-16 z-20 flex justify-center ${besidePanel}`}>
          <div className="pointer-events-auto w-full max-w-lg rounded-md border border-line bg-panel px-3 py-2 shadow-sm">
            <InlineError message={error.text} onRetry={error.retry && (() => { setError(null); error.retry?.(); })} />
          </div>
        </div>
      )}

      {/* Bottom-left: a one-line hint. Sections show in each dot's tooltip (color + name), not as pills. */}
      {data.nodes.length > 0 && (
        <p className="absolute bottom-3 left-3 z-10 max-w-[calc(100%-1.5rem)] px-1 text-xs text-muted max-sm:hidden">
          Click a dot to open it · drag to rotate · scroll to zoom · bigger dots are closer to the centre
        </p>
      )}

      {/* What a correction just changed, with Undo. Outside the panel, so it stays put while the panel scrolls. */}
      <div role="status" className={`pointer-events-none absolute bottom-12 left-3 right-3 z-30 flex justify-center ${besidePanel}`}>
        {toast && (
          <div className="pointer-events-auto flex items-center gap-3 rounded-md border border-line bg-panel px-3 py-2 text-xs shadow-lg">
            <span className="text-soft">{toast.text}</span>
            {toast.undo && <button type="button" onClick={undoLast} className="btn-link font-medium">Undo</button>}
          </div>
        )}
      </div>

      {/* Right: details, only while something is selected. Below the md breakpoint it covers the graph. */}
      <aside aria-label="Details" inert={!panelOpen}
        className={`absolute inset-y-0 right-0 z-20 w-full overflow-y-auto border-l border-line bg-panel transition-transform duration-200 md:max-w-96 ${panelOpen ? "translate-x-0" : "translate-x-full"}`}>
        {why ? (
          <WhyConnected key={`${why.a}-${why.b}`} a={why.a} b={why.b} revision={revision} onCloseAction={closePanel}
            onOpenAction={(id) => openFromPanel(id)}
            onCorrectedAction={corrected}
            onRestoredAction={() => { setRevision((n) => n + 1); void refresh(); }} />
        ) : selected !== null ? (
          <Inspector key={selected} id={selected} revision={revision} expanded={expanded.has(selected)}
            onExpandAction={toggle} onCloseAction={closePanel}
            onSelectAction={(id) => openFromPanel(id)}
            onWhyAction={(a, b) => setWhy({ a, b })}
            onLinkedAction={linked}
            onCorrectedAction={corrected} />
        ) : null}
      </aside>
    </main>
  );
}
