"use client";

import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import { useEffect, useRef } from "react";
import Sigma from "sigma";
import { Subgraph, TYPE_COLORS } from "@/lib/api";

type Props = {
  data: Subgraph;
  hiddenTypes: Set<string>;
  selected: number | null;
  onSelectAction: (id: number | null) => void;
  onExpandAction: (id: number) => void;
};

export default function GraphCanvas({ data, hiddenTypes, selected, onSelectAction: onSelect, onExpandAction: onExpand }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const sigma = useRef<Sigma | null>(null);
  const graph = useRef(new Graph({ multi: true }));
  // Latest props for the reducer, which sigma calls outside React's render cycle.
  const view = useRef({ hiddenTypes, selected });
  view.current = { hiddenTypes, selected };

  useEffect(() => {
    const s = new Sigma(graph.current, container.current!, {
      settings: { renderEdgeLabels: false, labelRenderedSizeThreshold: 6, hideEdgesOnMove: true },
      nodeReducer: (node, data, attrs) => {
        const { hiddenTypes, selected } = view.current;
        const out: Partial<typeof data> = {};
        if (hiddenTypes.has(attrs.nodeType as string)) out.visibility = "hidden";
        if (selected !== null) {
          const g = graph.current;
          const sel = String(selected);
          if (node === sel) out.highlighted = true;
          else if (!(g.hasNode(sel) && g.areNeighbors(node, sel))) {
            out.color = "#e5e7eb";
            out.label = null;
          }
        }
        return out;
      },
      edgeReducer: (edge) => {
        const { selected } = view.current;
        if (selected === null) return {};
        const touches = graph.current.extremities(edge).includes(String(selected));
        return touches ? { color: "#475569", size: 1.5 } : { color: "#f1f5f9" };
      },
    });
    s.on("clickNode", ({ node }) => onSelect(Number(node)));
    s.on("doubleClickNode", ({ node, event }) => {
      event.preventSigmaDefault();
      onExpand(Number(node));
    });
    s.on("clickStage", () => onSelect(null));
    sigma.current = s;
    return () => s.kill();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Merge new data in place so existing nodes keep their positions on expand.
  useEffect(() => {
    const g = graph.current;
    const maxDeg = Math.max(1, ...data.nodes.map((n) => n.degree));
    let added = 0;
    for (const n of data.nodes) {
      const id = String(n.id);
      const attrs = {
        label: n.name,
        nodeType: n.type,
        color: TYPE_COLORS[n.type] ?? "#666",
        size: 3 + 12 * Math.sqrt(n.degree / maxDeg),
      };
      if (g.hasNode(id)) g.mergeNodeAttributes(id, attrs);
      else {
        g.addNode(id, { ...attrs, x: Math.random(), y: Math.random() });
        added++;
      }
    }
    const maxW = Math.max(1, ...data.edges.map((e) => e.weight ?? 1));
    for (const e of data.edges) {
      const id = String(e.id);
      const size = e.weight ? 0.5 + 4 * Math.sqrt(e.weight / maxW) : 0.6;
      if (!g.hasEdge(id) && g.hasNode(String(e.src)) && g.hasNode(String(e.dst)))
        g.addEdgeWithKey(id, String(e.src), String(e.dst), { edgeType: e.type, color: "#cbd5e1", size });
    }
    if (added > 0 && g.order > 1) {
      forceAtlas2.assign(g, {
        iterations: g.order > 2000 ? 60 : 150,
        settings: { ...forceAtlas2.inferSettings(g), barnesHutOptimize: g.order > 500 },
      });
    }
    sigma.current?.refresh();
  }, [data]);

  useEffect(() => {
    sigma.current?.refresh({ skipIndexation: true });
  }, [hiddenTypes, selected]);

  return <div ref={container} className="h-full w-full" />;
}

