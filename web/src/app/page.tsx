"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useRef, useState } from "react";
import Inspector from "@/components/Inspector";
import { api, GNode, Subgraph, TYPE_COLORS } from "@/lib/api";

// sigma touches WebGL/window at import time, so it can't render on the server.
const GraphCanvas = dynamic(() => import("@/components/GraphCanvas"), { ssr: false });

const EMPTY: Subgraph = { nodes: [], edges: [] };

export default function Explorer() {
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<GNode[]>([]);
  const [data, setData] = useState<Subgraph>(EMPTY);
  const [canvasKey, setCanvasKey] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);   // canvas highlight (user clicked)
  const [inspected, setInspected] = useState<number | null>(null); // inspector panel
  const [hidden, setHidden] = useState<Set<string>>(new Set(["Organization", "Message"]));
  const [view, setView] = useState<"network" | "people" | "items">("network");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (query.trim().length < 2) return setMatches([]);
    const t = setTimeout(() => api.findNodes(query).then((r) => setMatches(r.nodes)).catch(() => {}), 200);
    return () => clearTimeout(t);
  }, [query]);

  const seed = async (node: GNode) => {
    setError(null);
    try {
      const id = node.id;
      const g = node.type !== "Person" || view === "items" ? await api.subgraph([id], 1, 400)
        : view === "network" ? await api.network(id) : await api.people(id, 60);
      setCanvasKey((k) => k + 1); // fresh canvas for a fresh seed
      setData(g);
      setSelected(null);   // don't dim the canvas: auto-highlighting the seed greyed out every hub
      setInspected(id);
      setMatches([]);
    } catch (e) {
      setError(String(e));
    }
  };

  // The canvas binds this once at mount, so read the current view through a ref, not a closure.
  const viewRef = useRef(view);
  viewRef.current = view;
  const expand = useCallback(async (id: number) => {
    const g = viewRef.current === "items" ? await api.subgraph([id], 1, 200)
      : viewRef.current === "network" ? await api.network(id, 25, 15) : await api.people(id, 30);
    setData((prev) => {
      const nodes = new Map(prev.nodes.map((n) => [n.id, n]));
      g.nodes.forEach((n) => nodes.set(n.id, n));
      const edges = new Map(prev.edges.map((e) => [e.id, e]));
      g.edges.forEach((e) => edges.set(e.id, e));
      return { nodes: [...nodes.values()], edges: [...edges.values()] };
    });
  }, []);

  const types = [...new Set(data.nodes.map((n) => n.type))].sort();

  return (
    <div className="flex h-[calc(100vh-49px)]">
      <aside className="w-64 shrink-0 space-y-5 overflow-y-auto border-r border-zinc-200 p-4 text-sm">
        <div className="relative">
          <label className="mb-1 block font-medium text-zinc-700">Seed node</label>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Person, thread, file…"
            className="w-full rounded border border-zinc-300 px-2 py-1.5 outline-none focus:border-zinc-500"
          />
          {matches.length > 0 && (
            <ul className="absolute z-10 mt-1 max-h-72 w-full overflow-y-auto rounded border border-zinc-200 bg-white shadow">
              {matches.map((m) => (
                <li key={m.id}>
                  <button onClick={() => seed(m)} className="flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-zinc-50">
                    <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: TYPE_COLORS[m.type] }} />
                    <span className="truncate">{m.name}</span>
                    <span className="ml-auto text-xs text-zinc-400">{m.degree}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div>
          <label className="mb-1 block font-medium text-zinc-700">View</label>
          <div className="flex gap-1">
            {([["network", "Network"], ["people", "People"], ["items", "Items"]] as const).map(([v, label]) => (
              <button key={v} onClick={() => setView(v)}
                className={`flex-1 rounded border px-2 py-1 ${view === v ? "border-zinc-800 bg-zinc-800 text-white" : "border-zinc-300"}`}>
                {label}
              </button>
            ))}
          </div>
          <p className="mt-1 text-xs text-zinc-500">
            {view === "network" ? "People clustered by the group chats, threads, and events they share. Color = community; grey dots = the shared conversation or event."
              : view === "people" ? "Who a person corresponds with; edge width = messages exchanged."
              : "Raw neighbors: messages, threads, files."}
          </p>
        </div>

        {types.length > 0 && (
          <div>
            <div className="mb-1 font-medium text-zinc-700">Node types</div>
            {types.map((t) => (
              <label key={t} className="flex items-center gap-2 py-0.5">
                <input type="checkbox" checked={!hidden.has(t)}
                  onChange={() => setHidden((h) => { const n = new Set(h); if (n.has(t)) n.delete(t); else n.add(t); return n; })} />
                <span className="h-2 w-2 rounded-full" style={{ background: TYPE_COLORS[t] }} />
                {t}
                <span className="ml-auto text-xs text-zinc-400">{data.nodes.filter((n) => n.type === t).length}</span>
              </label>
            ))}
          </div>
        )}

        <p className="text-xs leading-relaxed text-zinc-500">
          Click a node to inspect it. Double-click to expand its neighbors. Click empty space to clear.
        </p>
        <p className="text-xs text-zinc-500">{data.nodes.length} nodes · {data.edges.length} edges</p>
        {error && <p className="text-xs text-red-600">{error}</p>}
      </aside>

      <main className="relative flex-1 bg-zinc-50">
        {data.nodes.length === 0 ? (
          <div className="flex h-full items-center justify-center text-sm text-zinc-500">
            Search for a person or thread to seed the graph.
          </div>
        ) : (
          <GraphCanvas key={canvasKey} data={data} hiddenTypes={hidden} selected={selected}
            onSelectAction={(id) => { setSelected(id); if (id !== null) setInspected(id); }} onExpandAction={expand} />
        )}
      </main>

      <aside className="w-96 shrink-0 overflow-y-auto border-l border-zinc-200">
        <Inspector id={selected ?? inspected} onExpandAction={expand} />
      </aside>
    </div>
  );
}
