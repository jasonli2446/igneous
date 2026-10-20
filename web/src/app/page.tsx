"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useRef, useState } from "react";
import type { Community } from "@/components/Graph3D";
import Inspector from "@/components/Inspector";
import { useTheme } from "@/components/ThemeToggle";
import WhyConnected from "@/components/WhyConnected";
import { api, GNode, Subgraph, TYPE_COLORS } from "@/lib/api";

// WebGL libraries touch window at import time, so they can't render on the server.
const Graph3D = dynamic(() => import("@/components/Graph3D"), { ssr: false });
const GraphCanvas = dynamic(() => import("@/components/GraphCanvas"), { ssr: false });

const EMPTY: Subgraph = { nodes: [], edges: [] };
const HIDDEN_2D = new Set(["Organization", "Message"]);
type View = "network" | "people" | "items";
const VIEWS: { id: View; label: string; help: string }[] = [
  { id: "network", label: "Network", help: "Your people in 3D, grouped by the conversations and events they share." },
  { id: "people", label: "People", help: "Who a person writes to most. Line width is messages exchanged." },
  { id: "items", label: "Items", help: "The raw messages, threads, and files around a node." },
];

export default function Explorer() {
  const dark = useTheme();
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<GNode[]>([]);
  const [data, setData] = useState<Subgraph>(EMPTY);
  const [canvasKey, setCanvasKey] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
  const [inspected, setInspected] = useState<number | null>(null);
  const [why, setWhy] = useState<{ a: number; b: number } | null>(null);
  const [view, setView] = useState<View>("network");
  const [trail, setTrail] = useState<GNode[]>([]);
  const [legend, setLegend] = useState<Community[]>([]);
  const [legendOpen, setLegendOpen] = useState(true);
  const [loading, setLoading] = useState(false);
  const [expansionParent, setExpansionParent] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (query.trim().length < 2) return setMatches([]);
    const t = setTimeout(() => api.findNodes(query).then((r) => setMatches(r.nodes)).catch(() => {}), 200);
    return () => clearTimeout(t);
  }, [query]);

  const graphGeneration = useRef(0);
  const seedRequest = useRef<AbortController | null>(null);
  const expandRequest = useRef<AbortController | null>(null);

  useEffect(() => () => {
    seedRequest.current?.abort();
    expandRequest.current?.abort();
  }, []);

  const load = useCallback(async (node: GNode, v: View, push = true) => {
    const generation = ++graphGeneration.current;
    seedRequest.current?.abort();
    expandRequest.current?.abort();
    const request = new AbortController();
    seedRequest.current = request;
    setError(null);
    setLoading(true);
    try {
      const g = node.type !== "Person" || v === "items" ? await api.subgraph([node.id], 1, 400, {}, request.signal)
        : v === "network" ? await api.network(node.id, 50, 30, request.signal) : await api.people(node.id, 60, request.signal);
      if (graphGeneration.current !== generation || seedRequest.current !== request) return;
      setCanvasKey((k) => k + 1);
      setExpansionParent(null);
      setData(g);
      setSelected(null);
      setWhy(null);
      setInspected(node.id);
      setMatches([]);
      setQuery("");
      if (push) setTrail((t) => [...t.filter((x) => x.id !== node.id), node].slice(-6));
    } catch (e) {
      if (!request.signal.aborted) setError(`Couldn't load ${node.name}. Check that the API is running on port 8000. (${String(e)})`);
    } finally {
      if (seedRequest.current === request) {
        seedRequest.current = null;
        setLoading(false);
      }
    }
  }, []);

  // The canvases bind callbacks once, so read the current view through a ref.
  const viewRef = useRef(view);
  viewRef.current = view;
  const expand = useCallback(async (id: number) => {
    const generation = graphGeneration.current;
    expandRequest.current?.abort();
    const request = new AbortController();
    expandRequest.current = request;
    setError(null);
    try {
      const v = viewRef.current;
      const g = v === "items" ? await api.subgraph([id], 1, 200, {}, request.signal) : v === "network"
        ? await api.network(id, 20, 12, request.signal) : await api.people(id, 30, request.signal);
      if (graphGeneration.current !== generation || expandRequest.current !== request) return;
      setExpansionParent(id);
      setData((prev) => {
        const nodes = new Map(prev.nodes.map((n) => [n.id, n]));
        g.nodes.forEach((n) => nodes.has(n.id) || nodes.set(n.id, n)); // keep the seed first: don't reorder
        const edges = new Map(prev.edges.map((e) => [`${e.src}-${e.dst}-${e.type}`, e]));
        g.edges.forEach((e) => edges.set(`${e.src}-${e.dst}-${e.type}`, { ...e, id: edges.size + 1e6 }));
        return { nodes: [...nodes.values()], edges: [...edges.values()] };
      });
    } catch (e) {
      if (!request.signal.aborted) setError(String(e));
    } finally {
      if (expandRequest.current === request) expandRequest.current = null;
    }
  }, []);

  const onLegend = useCallback((c: Community[]) => setLegend(c), []);
  const current = trail[trail.length - 1];
  const switchView = (v: View) => {
    setView(v);
    if (current) load(current, v, false);
  };
  const back = () => {
    if (trail.length < 2) return;
    const prev = trail[trail.length - 2];
    setTrail((t) => t.slice(0, -1));
    load(prev, view, false);
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
                  <button onClick={() => load(m, view)} className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left hover:bg-hover">
                    <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: TYPE_COLORS[m.type] }} />
                    <span className="truncate">{m.name}</span>
                    <span className="ml-auto text-xs text-muted">{m.degree}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div>
          <div className="mb-1.5 font-medium">View</div>
          <div role="tablist" className="flex rounded-md border border-line p-0.5">
            {VIEWS.map((v) => (
              <button key={v.id} role="tab" aria-selected={view === v.id} onClick={() => switchView(v.id)}
                className={`flex-1 rounded px-2 py-1 ${view === v.id ? "bg-ink text-bg" : "text-soft hover:bg-hover"}`}>
                {v.label}
              </button>
            ))}
          </div>
          <p className="mt-1.5 text-xs leading-relaxed text-muted">{VIEWS.find((v) => v.id === view)!.help}</p>
        </div>

        {view === "network" && data.nodes.length > 0 && (
          <div>
            <div className="mb-1.5 font-medium">Moving around</div>
            <ul className="space-y-1 text-xs leading-relaxed text-muted">
              <li>Drag to rotate, right-drag to pan, scroll to zoom</li>
              <li>Click a person to fly to them; double-click to bring in their people</li>
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
        ) : view === "network" ? (
          <Graph3D key={canvasKey} data={data} dark={dark} selected={selected}
            onSelectAction={(id) => { setSelected(id); setWhy(null); if (id !== null) setInspected(id); }}
            onExpandAction={expand}
            onLinkAction={(a, b) => setWhy({ a: a.id, b: b.id })}
            onCommunitiesAction={onLegend} />
        ) : (
          <div className="absolute inset-0">
            <GraphCanvas key={canvasKey} data={data} hiddenTypes={HIDDEN_2D} selected={selected} expansionParent={expansionParent}
              onSelectAction={(id) => { setSelected(id); if (id !== null) setInspected(id); }} onExpandAction={expand} />
          </div>
        )}

        {current && (
          <div className="absolute left-3 top-3 z-10 flex max-w-[70%] items-center gap-1 rounded-md border border-line bg-panel px-2 py-1 text-xs">
            <button onClick={back} disabled={trail.length < 2} aria-label="Back"
              className="rounded px-1 text-muted hover:bg-hover disabled:opacity-30">‹</button>
            {trail.map((t, i) => (
              <span key={t.id} className="flex min-w-0 items-center gap-1">
                {i > 0 && <span className="text-muted">/</span>}
                <button onClick={() => { setTrail((tr) => tr.slice(0, i + 1)); load(t, view, false); }}
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

        {view === "network" && legend.length > 0 && (
          <div className="absolute bottom-3 right-3 z-10 w-60 space-y-1.5 rounded-md border border-line bg-panel/95 p-3 text-xs">
            <button onClick={() => setLegendOpen((o) => !o)} aria-expanded={legendOpen}
              className="flex w-full items-center font-medium">
              Groups <span className="ml-auto text-muted">{legendOpen ? "Hide" : `${legend.length}`}</span>
            </button>
            {legendOpen && legend.map((c) => (
              <div key={c.color} className="flex items-center gap-2">
                <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: c.color }} />
                <span className="truncate text-soft">
                  {c.members.join(", ")}{c.size > c.members.length ? ` and ${c.size - c.members.length} more` : ""}
                </span>
              </div>
            ))}
            {legendOpen && (
              <div className="flex items-center gap-2 pt-1 text-muted">
                <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-muted" />
                <span>Small dots are shared conversations and events</span>
              </div>
            )}
          </div>
        )}
      </main>

      <aside className="w-96 shrink-0 overflow-y-auto border-l border-line bg-panel">
        {why ? (
          <WhyConnected a={why.a} b={why.b} onCloseAction={() => setWhy(null)}
            onOpenAction={(id) => { setWhy(null); setInspected(id); setSelected(null); }} />
        ) : (
          <Inspector id={selected ?? inspected} onExpandAction={expand} />
        )}
      </aside>
    </div>
  );
}
