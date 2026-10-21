"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useRef, useState } from "react";
import { CATEGORY_COLOR, type Community } from "@/lib/graph-style";
import Inspector from "@/components/Inspector";
import { useTheme } from "@/components/ThemeToggle";
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
  const [inspected, setInspected] = useState<number | null>(null);
  const [why, setWhy] = useState<{ a: number; b: number } | null>(null);
  // Which node brought each node onto the screen, so clicking an expanded node collapses what it added.
  const [introducedBy, setIntroducedBy] = useState<Map<number, number>>(new Map());
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [trail, setTrail] = useState<GNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
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
    setLoading(true);
    try {
      const g = await api.view(node?.id, request.signal);
      if (graphGeneration.current !== generation || seedRequest.current !== request) return;
      setCanvasKey((k) => k + 1);
      setData(g);
      setIntroducedBy(new Map());
      setExpanded(new Set());
      setSelected(null);
      setWhy(null);
      setInspected(g.seed);
      setMatches([]);
      setQuery("");
      const seedNode = g.nodes.find((n) => n.id === g.seed) ?? node;
      if (push && seedNode) setTrail((t) => [...t.filter((x) => x.id !== seedNode.id), seedNode].slice(-6));
    } catch (e) {
      if (!request.signal.aborted) setError(`Couldn't load the graph. Check that the API is running on port 8000. (${String(e)})`);
    } finally {
      if (seedRequest.current === request) {
        seedRequest.current = null;
        setLoading(false);
      }
    }
  }, []);

  // Open on you: the graph starts from the account owner, no search needed.
  useEffect(() => { load(null); }, [load]);

  const seedId = trail[trail.length - 1]?.id;
  const dataRef = useRef(data);
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
        return { nodes: [...prev.nodes, ...g.nodes.filter((n) => !have.has(n.id))], edges: [...edges.values()] };
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
  const hubs = data.nodes.filter((n) => n.kind === "category");
  const current = trail[trail.length - 1];
  const back = () => {
    if (trail.length < 2) return;
    const prev = trail[trail.length - 2];
    setTrail((t) => t.slice(0, -1));
    load(prev, false);
  };

  return (
    <div className="flex h-[calc(100vh-49px)] bg-bg text-ink">
      <aside className="w-64 shrink-0 space-y-6 overflow-y-auto border-r border-line bg-panel p-4 text-sm">
        <div className="relative">
          <label htmlFor="seed" className="mb-1.5 block font-medium">Start from</label>
          <input id="seed" value={query} onChange={(e) => setQuery(e.target.value)}
            placeholder="A person, conversation, or event"
            className="w-full rounded-md border border-line bg-bg px-2.5 py-1.5 outline-none placeholder:text-muted focus:border-accent" />
          {matches.length > 0 && (
            <ul className="absolute z-20 mt-1 max-h-72 w-full overflow-y-auto rounded-md border border-line bg-panel shadow-lg">
              {matches.map((m) => (
                <li key={m.id}>
                  <button onClick={() => load(m)} className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left hover:bg-hover">
                    <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: TYPE_COLORS[m.type] }} />
                    <span className="truncate">{m.name}</span>
                    <span className="ml-auto text-xs text-muted">{m.degree}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {data.nodes.length > 0 && (
          <div>
            <div className="mb-1.5 font-medium">Moving around</div>
            <ul className="space-y-1 text-xs leading-relaxed text-muted">
              <li>Click anything to open it up; click it again to fold it back</li>
              <li>Drag to rotate, right-drag to pan, scroll to zoom</li>
              <li>Hover a line to see why it&apos;s there; click it for the details</li>
              <li>Drag a dot to pin it in place</li>
            </ul>
          </div>
        )}

        {error && <p className="text-xs leading-relaxed text-danger">{error}</p>}
      </aside>

      <main className="relative flex-1 overflow-hidden">
        {data.nodes.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center text-sm text-muted">
            <p className="text-base text-ink">Pick someone to start with</p>
            <p>Search on the left for a person, conversation, or event. Try “Me”.</p>
          </div>
        ) : (
          <Graph3D key={canvasKey} data={data} dark={dark} selected={selected}
            onSelectAction={(id) => { setSelected(id); setWhy(null); if (id !== null) setInspected(id); }}
            onExpandAction={toggle}
            onLinkAction={(a, b) => { if (a.id > 0 && b.id > 0) setWhy({ a: a.id, b: b.id }); }}
            onCommunitiesAction={onLegend} />
        )}

        {current && (
          <div className="absolute left-3 top-3 z-10 flex max-w-[70%] items-center gap-1 rounded-md border border-line bg-panel px-2 py-1 text-xs">
            <button onClick={back} disabled={trail.length < 2} aria-label="Back"
              className="rounded px-1 text-muted hover:bg-hover disabled:opacity-30">‹</button>
            {trail.map((t, i) => (
              <span key={t.id} className="flex min-w-0 items-center gap-1">
                {i > 0 && <span className="text-muted">/</span>}
                <button onClick={() => { setTrail((tr) => tr.slice(0, i + 1)); load(t, false); }}
                  className={`truncate ${i === trail.length - 1 ? "font-medium" : "text-muted hover:text-ink"}`}>
                  {t.name}
                </button>
              </span>
            ))}
          </div>
        )}

        {loading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-bg/60 text-sm text-muted">
            Building the graph…
          </div>
        )}

        {hubs.length > 0 && (
          <div className="absolute bottom-3 right-3 z-10 w-56 space-y-1.5 rounded-md border border-line bg-panel/95 p-3 text-xs">
            <div className="font-medium">What&apos;s here</div>
            {hubs.map((h) => (
              <button key={h.id} onClick={() => toggle(h.id)} className="flex w-full items-center gap-2 text-left hover:text-ink">
                <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: CATEGORY_COLOR[h.name] ?? "#a8aca3" }} />
                <span className="text-soft">{h.name}</span>
                <span className="ml-auto text-muted">{h.total}</span>
              </button>
            ))}
            <p className="pt-1 leading-relaxed text-muted">Bigger dots are closer to you.</p>
          </div>
        )}
      </main>

      <aside className="w-96 shrink-0 overflow-y-auto border-l border-line bg-panel">
        {why ? (
          <WhyConnected a={why.a} b={why.b} onCloseAction={() => setWhy(null)}
            onOpenAction={(id) => { setWhy(null); setInspected(id); setSelected(null); }} />
        ) : (
          <Inspector id={selected !== null && selected > 0 ? selected : inspected} onExpandAction={toggle} />
        )}
      </aside>
    </div>
  );
}
