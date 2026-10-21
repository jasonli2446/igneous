"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { CATEGORY_COLOR, type Community } from "@/lib/graph-style";
import Inspector from "@/components/Inspector";
import { useTheme } from "@/components/ThemeToggle";
import { InlineError, TopProgress } from "@/components/ui";
import WhyConnected from "@/components/WhyConnected";
import { api, GNode, Subgraph, TYPE_COLORS } from "@/lib/api";

// WebGL libraries touch window at import time, so they can't render on the server.
const Graph3D = dynamic(() => import("@/components/Graph3D"), { ssr: false });

const EMPTY: Subgraph = { nodes: [], edges: [] };

export default function Explorer() {
  const dark = useTheme();
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<GNode[]>([]);
  const [data, setData] = useState<Subgraph>(EMPTY);
  const [canvasKey, setCanvasKey] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
  const [why, setWhy] = useState<{ a: number; b: number } | null>(null);
  // Which node brought each node onto the screen, so clicking an expanded node collapses what it added.
  const [introducedBy, setIntroducedBy] = useState<Map<number, number>>(new Map());
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [trail, setTrail] = useState<GNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The API answers 404 until a self node exists, i.e. until a source is connected and the graph is built.
  const [nothingConnected, setNothingConnected] = useState(false);

  const [active, setActive] = useState(0);
  const searchRef = useRef<HTMLInputElement>(null);

  // "/" jumps to search from anywhere that isn't a text field.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (e.key !== "/" || e.metaKey || e.ctrlKey || t.closest("input, textarea, select, [contenteditable]")) return;
      e.preventDefault();
      searchRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    setActive(0);
    if (query.trim().length < 2) return setMatches([]);
    const t = setTimeout(() => api.findNodes(query).then((r) => setMatches(r.nodes)).catch(() => {}), 200);
    return () => clearTimeout(t);
  }, [query]);

  // Stale responses must never land: each new seed bumps the generation and aborts in-flight requests
  // (a fast double search used to let the older reply overwrite the newer graph).
  const graphGeneration = useRef(0);
  const seedRequest = useRef<AbortController | null>(null);
  const expandRequest = useRef<AbortController | null>(null);

  useEffect(() => () => {
    seedRequest.current?.abort();
    expandRequest.current?.abort();
  }, []);

  const load = useCallback(async (node: GNode | null, push = true) => {
    const generation = ++graphGeneration.current;
    seedRequest.current?.abort();
    expandRequest.current?.abort();
    const request = new AbortController();
    seedRequest.current = request;
    setError(null);
    setNothingConnected(false);
    setLoading(true);
    try {
      const g = await api.view(node?.id, request.signal);
      if (graphGeneration.current !== generation || seedRequest.current !== request) return;
      if (!node && g.nodes.length === 0) setNothingConnected(true);
      setCanvasKey((k) => k + 1);
      setData(g);
      setIntroducedBy(new Map());
      setExpanded(new Set());
      setSelected(null);
      setWhy(null);
      setMatches([]);
      setQuery("");
      const seedNode = g.nodes.find((n) => n.id === g.seed) ?? node;
      if (push && seedNode) setTrail((t) => [...t.filter((x) => x.id !== seedNode.id), seedNode].slice(-6));
    } catch (e) {
      if (!request.signal.aborted && !node && String(e).startsWith("Error: 404")) setNothingConnected(true);
      else if (!request.signal.aborted) setError(`Couldn't load the graph. Check that the API is running on port 8000. (${String(e)})`);
    } finally {
      if (seedRequest.current === request) {
        seedRequest.current = null;
        setLoading(false);
      }
    }
  }, []);

  // Open on you: the graph starts from the account owner, no search needed.
  useEffect(() => { load(null); }, [load]);

  const retry = () => load(trail[trail.length - 1] ?? null, false);
  const seedId = trail[trail.length - 1]?.id;
  const dataRef = useRef(data);

  // The graph is long-lived: only fetch its full shape when the server change token moves.
  // Merge the refreshed seed view so locally expanded branches and the current selection stay put.
  useEffect(() => {
    if (!data.nodes.length) return;
    let cancelled = false;
    let version = "";
    const refresh = async () => {
      try {
        const next = await api.graphVersion();
        if (cancelled || next.version === version) return;
        version = next.version;
        const incoming = await api.view(seedId);
        if (cancelled) return;
        setData((previous) => {
          const nodes = new Map(previous.nodes.map((node) => [node.id, node]));
          incoming.nodes.forEach((node) => nodes.set(node.id, node));
          const edges = new Map(previous.edges.map((edge) => [`${edge.src}-${edge.dst}-${edge.type}`, edge]));
          incoming.edges.forEach((edge) => edges.set(`${edge.src}-${edge.dst}-${edge.type}`, edge));
          return { nodes: [...nodes.values()], edges: [...edges.values()] };
        });
      } catch { /* a transient API outage should not disrupt an open graph */ }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 30_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [seedId, data.nodes.length]);
  dataRef.current = data;
  const stateRef = useRef({ introducedBy, expanded, seedId });
  stateRef.current = { introducedBy, expanded, seedId };

  // Click a node: reveal its top connections; click it again to fold them back.
  const toggle = useCallback(async (id: number) => {
    const { introducedBy: ib, expanded: ex, seedId: seed } = stateRef.current;
    if (seed === undefined) return;
    if (ex.has(id)) {
      const drop = new Set<number>();
      const collect = (p: number) => ib.forEach((parent, child) => { if (parent === p && !drop.has(child)) { drop.add(child); collect(child); } });
      collect(id);
      setData((prev) => ({
        ...prev,
        nodes: prev.nodes.filter((n) => !drop.has(n.id)),
        edges: prev.edges.filter((e) => !drop.has(e.src) && !drop.has(e.dst)),
      }));
      setIntroducedBy((m) => { const n = new Map(m); drop.forEach((d) => n.delete(d)); return n; });
      setExpanded((s) => { const n = new Set(s); n.delete(id); drop.forEach((d) => n.delete(d)); return n; });
      return;
    }
    const generation = graphGeneration.current;
    expandRequest.current?.abort();
    const request = new AbortController();
    expandRequest.current = request;
    setError(null);
    try {
      const visible = dataRef.current.nodes.map((n) => n.id);
      const g = await api.expandNode(seed, id, visible, 0, request.signal);
      if (graphGeneration.current !== generation || expandRequest.current !== request || !g.nodes.length) return;
      setData((prev) => {
        const have = new Set(prev.nodes.map((n) => n.id));
        const edges = new Map(prev.edges.map((e) => [`${e.src}-${e.dst}-${e.type}`, e]));
        g.edges.forEach((e) => edges.set(`${e.src}-${e.dst}-${e.type}`, e));
        return { ...prev, nodes: [...prev.nodes, ...g.nodes.filter((n) => !have.has(n.id))], edges: [...edges.values()] };
      });
      setIntroducedBy((m) => { const n = new Map(m); g.nodes.forEach((x) => { if (!n.has(x.id)) n.set(x.id, id); }); return n; });
      setExpanded((s) => new Set(s).add(id));
    } catch (e) {
      if (!request.signal.aborted) setError(String(e));
    } finally {
      if (expandRequest.current === request) expandRequest.current = null;
    }
  }, []);

  const onLegend = useCallback((_c: Community[]) => {}, []); // communities color nodes; the legend lists categories
  // Categories aren't nodes: they color the dots, and the legend lists them (click to show more of one).
  const hubs = data.categories ?? [];
  const current = trail[trail.length - 1];
  const back = () => {
    if (trail.length < 2) return;
    const prev = trail[trail.length - 2];
    setTrail((t) => t.slice(0, -1));
    load(prev, false);
  };

  const detailId = selected !== null && selected > 0 ? selected : null;
  const panelOpen = why !== null || detailId !== null;
  const closePanel = () => { setWhy(null); setSelected(null); };

  const onSearchKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { setQuery(""); setMatches([]); (e.target as HTMLElement).blur(); }
    else if (e.key === "ArrowDown" && matches.length) { e.preventDefault(); setActive((i) => (i + 1) % matches.length); }
    else if (e.key === "ArrowUp" && matches.length) { e.preventDefault(); setActive((i) => (i - 1 + matches.length) % matches.length); }
    else if (e.key === "Enter" && matches[active]) { e.preventDefault(); load(matches[active]); }
  };

  return (
    <div className="relative h-[calc(100vh-49px)] overflow-hidden bg-bg text-ink">
      <TopProgress active={loading} />

      {data.nodes.length > 0 && (
        <Graph3D key={canvasKey} data={data} dark={dark} selected={selected}
          onSelectAction={(id) => { setSelected(id); setWhy(null); }}
          onExpandAction={toggle}
          onLinkAction={(a, b) => { if (a.id > 0 && b.id > 0) setWhy({ a: a.id, b: b.id }); }}
          onCommunitiesAction={onLegend} />
      )}

      {data.nodes.length === 0 && !loading && !error && nothingConnected && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center text-sm text-muted">
          <p className="text-base text-ink">Your graph is empty</p>
          <p className="max-w-sm">Connect a source, then build the graph. Setup takes a few minutes and everything stays on this computer.</p>
          <Link href="/settings" className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-bg hover:opacity-90">Connect your first source</Link>
        </div>
      )}

      {data.nodes.length === 0 && !loading && !error && !nothingConnected && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 px-6 text-center text-sm text-muted">
          <p className="text-base text-ink">Nothing to show yet</p>
          <p>Search for a person, conversation, or event to start. Press / to search.</p>
        </div>
      )}

      {/* Top row: where you are, and search. */}
      <div className="pointer-events-none absolute inset-x-3 top-3 z-20 flex items-start gap-2">
        {current && (
          <div className="pointer-events-auto flex min-w-0 max-w-[40%] shrink-0 items-center gap-1 rounded-md border border-line bg-panel/95 px-2 py-1.5 text-xs">
            <button onClick={back} disabled={trail.length < 2} aria-label="Back"
              className="rounded px-1 text-muted hover:bg-hover disabled:opacity-30">‹</button>
            {trail.map((t, i) => (
              <span key={t.id} className={`flex min-w-0 items-center gap-1 ${i < trail.length - 1 ? "max-sm:hidden" : ""}`}>
                {i > 0 && <span className="text-muted">/</span>}
                <button onClick={() => { setTrail((tr) => tr.slice(0, i + 1)); load(t, false); }}
                  className={`truncate ${i === trail.length - 1 ? "font-medium" : "text-muted hover:text-ink"}`}>
                  {t.name}
                </button>
              </span>
            ))}
          </div>
        )}

        <div className="pointer-events-auto relative mx-auto min-w-0 flex-1 max-w-md">
          <div className="flex items-center gap-2 rounded-md border border-line bg-panel/95 px-3 py-1.5 text-sm focus-within:border-accent">
            <label htmlFor="seed" className="sr-only">Search the graph</label>
            <input id="seed" ref={searchRef} value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={onSearchKey}
              role="combobox" aria-expanded={matches.length > 0} aria-controls="seed-results" autoComplete="off"
              placeholder="Jump to a person, conversation, or event"
              className="min-w-0 flex-1 bg-transparent outline-none placeholder:text-muted focus-visible:outline-none" />
            <kbd className="rounded border border-line px-1.5 text-[11px] text-muted">/</kbd>
          </div>
          {matches.length > 0 && (
            <ul id="seed-results" role="listbox" className="absolute z-30 mt-1 max-h-72 w-full overflow-y-auto rounded-md border border-line bg-panel shadow-lg">
              {matches.map((m, i) => (
                <li key={m.id} role="option" aria-selected={i === active}>
                  <button onClick={() => load(m)} onMouseEnter={() => setActive(i)}
                    className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm ${i === active ? "bg-hover" : ""}`}>
                    <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: TYPE_COLORS[m.type] }} />
                    <span className="truncate">{m.name}</span>
                    <span className="ml-auto text-xs text-muted">{m.type}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {error && (
        <div className="absolute left-1/2 top-16 z-20 w-[min(32rem,calc(100%-1.5rem))] -translate-x-1/2 rounded-md border border-line bg-panel px-3 py-2 shadow-sm">
          <InlineError message={error} onRetry={retry} />
        </div>
      )}

      {/* Bottom-left: compact legend and a one-line hint. */}
      {data.nodes.length > 0 && (
        <div className="absolute bottom-3 left-3 z-10 max-w-[calc(100%-1.5rem)] space-y-1.5 text-xs">
          {hubs.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {hubs.map((h) => (
                <button key={h.id} onClick={() => toggle(h.id)} title={expanded.has(h.id) ? `Show fewer ${h.name.toLowerCase()}` : `Show more ${h.name.toLowerCase()}`}
                  className="flex items-center gap-1.5 rounded-full border border-line bg-panel/95 px-2.5 py-1 hover:bg-hover">
                  <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: CATEGORY_COLOR[h.name] ?? "#a8aca3" }} />
                  <span className="text-soft">{h.name}</span>
                  <span className="tabular-nums text-muted">{h.total}</span>
                </button>
              ))}
            </div>
          )}
          <p className="px-1 text-muted max-sm:hidden">Click a dot to open it · drag to rotate · scroll to zoom · bigger dots are closer to you</p>
        </div>
      )}

      {/* Right: details, only while something is selected. */}
      <aside aria-hidden={!panelOpen} inert={!panelOpen}
        className={`absolute inset-y-0 right-0 z-20 w-full max-w-96 overflow-y-auto border-l border-line bg-panel transition-transform duration-200 ${panelOpen ? "translate-x-0" : "translate-x-full"}`}>
        {why ? (
          <WhyConnected a={why.a} b={why.b} onCloseAction={closePanel}
            onOpenAction={(id) => { setWhy(null); setSelected(id); }} />
        ) : (
          <Inspector id={detailId} onExpandAction={toggle} onCloseAction={closePanel}
            onSelectAction={(id) => setSelected(id)}
            onChangedAction={async (keep) => { await load(trail[trail.length - 1] ?? null, false); if (keep !== null) setSelected(keep); }} />
        )}
      </aside>
    </div>
  );
}
