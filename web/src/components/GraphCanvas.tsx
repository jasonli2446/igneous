"use client";

import Graph from "graphology";
import louvain from "graphology-communities-louvain";
import forceAtlas2 from "graphology-layout-forceatlas2";
import FA2Layout from "graphology-layout-forceatlas2/worker";
import { useEffect, useRef } from "react";
import Sigma from "sigma";
import { Subgraph, TYPE_COLORS } from "@/lib/api";

// Distinct, readable hues for communities (friend groups, teams). Hubs and the seed stay neutral.
const COMMUNITY = ["#2563eb", "#db2777", "#059669", "#d97706", "#7c3aed", "#0891b2", "#dc2626", "#65a30d",
  "#c026d3", "#0d9488", "#ea580c", "#4f46e5"];
const HUB_TYPES = new Set(["Thread", "Event", "Organization", "Topic"]);
// Labels for the most important nodes only; the rest show on hover or selection (no pile-up of text).
const LABEL_TOP_N = 18;

type Props = {
  data: Subgraph;
  hiddenTypes: Set<string>;
  selected: number | null;
  expansionParent: number | null;
  onSelectAction: (id: number | null) => void;
  onExpandAction: (id: number) => void;
};

const LAYOUT_DURATION_MS = 750;

function hash(value: string) {
  let result = 2166136261;
  for (let index = 0; index < value.length; index++) result = Math.imul(result ^ value.charCodeAt(index), 16777619);
  return result >>> 0;
}

function initialPosition(id: string) {
  return {
    x: (hash(`${id}:x`) / 0xffffffff) * 2 - 1,
    y: (hash(`${id}:y`) / 0xffffffff) * 2 - 1,
  };
}

function expansionPosition(g: Graph, id: string, parentId: number | null) {
  const parent = parentId === null ? null : String(parentId);
  if (!parent || !g.hasNode(parent)) return initialPosition(id);

  const { x, y } = g.getNodeAttributes(parent);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return initialPosition(id);

  const value = hash(`${parent}:${id}`);
  const angle = (value / 0xffffffff) * Math.PI * 2;
  const radius = 0.08 + (((value >>> 16) & 0xffff) / 0xffff) * 0.24;
  return { x: x + Math.cos(angle) * radius, y: y + Math.sin(angle) * radius };
}

export default function GraphCanvas({ data, hiddenTypes, selected, expansionParent, onSelectAction: onSelect, onExpandAction: onExpand }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const sigma = useRef<Sigma | null>(null);
  const graph = useRef(new Graph({ multi: true }));
  const layout = useRef<FA2Layout | null>(null);
  const layoutStopTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Latest props for the reducers, which Sigma calls outside React's render cycle.
  // Rendering every label makes dense people views unreadable; retain labels for the most
  // connected visible nodes and reveal the rest on hover or selection.
  const view = useRef({ hiddenTypes, selected, hovered: null as string | null });
  view.current.hiddenTypes = hiddenTypes;
  view.current.selected = selected;

  useEffect(() => {
    const s = new Sigma(graph.current, container.current!, {
      settings: { renderEdgeLabels: false, labelRenderedSizeThreshold: 0, hideEdgesOnMove: true,
        labelDensity: 1, labelGridCellSize: 120 },
      nodeReducer: (node, data, attrs) => {
        const { hiddenTypes, selected, hovered } = view.current;
        const out: Partial<typeof data> = {};
        if (hiddenTypes.has(attrs.nodeType as string)) out.visibility = "hidden";
        if (!attrs.showLabel && node !== hovered && node !== String(selected)) out.label = null;
        if (selected !== null) {
          const g = graph.current;
          const sel = String(selected);
          if (node === sel || (g.hasNode(sel) && g.areNeighbors(node, sel))) out.label = attrs.label as string;
          if (node === sel) out.highlighted = true;
          else if (!(g.hasNode(sel) && g.areNeighbors(node, sel))) {
            out.color = "#e5e7eb";
            out.label = null;
          }
        }
        return out;
      },
      edgeReducer: (edge, data, attrs) => {
        const { selected } = view.current;
        if (selected === null) return attrs.spoke ? { color: "#e8ecf2", size: 0.5 } : {};
        const touches = graph.current.extremities(edge).includes(String(selected));
        return touches ? { color: "#475569", size: 1.5 } : { color: "#f1f5f9" };
      },
    });
    s.on("clickNode", ({ node }) => onSelect(Number(node)));
    s.on("enterNode", ({ node }) => {
      view.current.hovered = node;
      s.refresh({ skipIndexation: true });
    });
    s.on("leaveNode", () => {
      view.current.hovered = null;
      s.refresh({ skipIndexation: true });
    });
    s.on("doubleClickNode", ({ node, event }) => {
      event.preventSigmaDefault();
      onExpand(Number(node));
    });
    s.on("clickStage", () => onSelect(null));
    sigma.current = s;
    return () => s.kill();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => () => {
    if (layoutStopTimer.current !== null) clearTimeout(layoutStopTimer.current);
    layout.current?.kill();
    layout.current = null;
  }, []);

  // Merge new data in place so existing nodes keep their positions on expand.
  useEffect(() => {
    const g = graph.current;
    const scale = graphScale(g);
    const seed = data.nodes[0] ? String(data.nodes[0].id) : null;
    // Importance to the seed: messages exchanged with it (people) or members (hubs); falls back to degree.
    const tie = new Map<string, number>();
    for (const e of data.edges) {
      const a = String(e.src), b = String(e.dst), w = e.weight ?? 1;
      if (a === seed) tie.set(b, (tie.get(b) ?? 0) + w);
      else if (b === seed) tie.set(a, (tie.get(a) ?? 0) + w);
      if (e.type === "PART_OF") tie.set(b, (tie.get(b) ?? 0) + 1);
    }
    const score = (n: Subgraph["nodes"][number]) => tie.get(String(n.id)) ?? n.degree;
    const maxScore = Math.max(1, ...data.nodes.map(score));
    const labeled = new Set(
      [...data.nodes]
        .sort((a, b) => score(b) - score(a) || a.name.localeCompare(b.name))
        .slice(0, LABEL_TOP_N)
        .map((n) => String(n.id)),
    );
    let addedNodes = 0;
    for (const n of data.nodes) {
      const id = String(n.id);
      const hub = HUB_TYPES.has(n.type);
      const attrs = {
        label: n.name,
        nodeType: n.type,
        showLabel: id === seed || labeled.has(id),
        color: TYPE_COLORS[n.type] ?? "#666",
        size: id === seed ? 16 : hub ? 3 + 5 * Math.sqrt(score(n) / maxScore) : 4 + 11 * Math.sqrt(score(n) / maxScore),
      };
      if (g.hasNode(id)) g.mergeNodeAttributes(id, attrs);
      else {
        g.addNode(id, { ...attrs, ...expansionPosition(g, id, expansionParent, scale) });
        addedNodes++;
      }
    }
    const maxW = Math.max(1, ...data.edges.map((e) => e.weight ?? 1));
    for (const e of data.edges) {
      const id = String(e.id);
      const spoke = seed !== null && (String(e.src) === seed || String(e.dst) === seed);
      const size = e.type === "PART_OF" ? 0.8 : e.weight ? 0.5 + 3.5 * Math.sqrt(e.weight / maxW) : 0.6;
      if (!g.hasEdge(id) && g.hasNode(String(e.src)) && g.hasNode(String(e.dst)))
        g.addEdgeWithKey(id, String(e.src), String(e.dst), {
          edgeType: e.type, spoke, size, weight: e.weight ?? 1,
          color: e.type === "PART_OF" ? "#d4d4d8" : "#94a3b8",
        });
    }
    if (g.order > 2) {
      // Communities over the non-seed graph (the seed touches everyone and would merge them all).
      const sub = g.copy();
      if (seed && sub.hasNode(seed)) sub.dropNode(seed);
      if (sub.size > 0) {
        const comm = louvain(sub, { getEdgeWeight: "weight", resolution: 1.1 });
        const sizes = new Map<number, number>();
        for (const c of Object.values(comm)) sizes.set(c, (sizes.get(c) ?? 0) + 1);
        const rank = [...sizes.entries()].sort((a, b) => b[1] - a[1]).map(([c]) => c);
        g.forEachNode((node, a) => {
          if (node === seed) g.setNodeAttribute(node, "color", "#18181b");
          else if (HUB_TYPES.has(a.nodeType as string)) g.setNodeAttribute(node, "color", "#a1a1aa");
          else if (node in comm) {
            const r = rank.indexOf(comm[node]);
            g.setNodeAttribute(node, "color", sizes.get(comm[node])! > 1 ? COMMUNITY[r % COMMUNITY.length] : "#94a3b8");
          }
        });
      }
    }
    if (addedNodes > 0 && g.order > 1) {
      if (layoutStopTimer.current !== null) clearTimeout(layoutStopTimer.current);
      layout.current?.kill();
      try {
        layout.current = new FA2Layout(g, {
          settings: {
            ...forceAtlas2.inferSettings(g),
            linLogMode: true,
            outboundAttractionDistribution: true,
            adjustSizes: true,
            barnesHutOptimize: g.order > 500,
            edgeWeightInfluence: 0.6,
          },
        });
        layout.current.start();
        layoutStopTimer.current = setTimeout(() => {
          layout.current?.stop();
          layoutStopTimer.current = null;
        }, LAYOUT_DURATION_MS);
      } catch (error) {
        layout.current = null;
        console.warn("Unable to start graph layout worker", error);
      }
    }
    sigma.current?.refresh();
  }, [data, expansionParent]);

  useEffect(() => {
    sigma.current?.refresh({ skipIndexation: true });
  }, [hiddenTypes, selected]);

  return <div ref={container} className="h-full w-full" />;
}

