"use client";

import ForceGraph3D, { type ForceGraph3DInstance } from "3d-force-graph";
import louvain from "graphology-communities-louvain";
import Graph from "graphology";
import { useEffect, useRef } from "react";
import { Fog } from "three";
import SpriteText from "three-spritetext";
import { GEdge, GNode, Subgraph } from "@/lib/api";
import { CATEGORY_COLOR, TYPE_CATEGORY } from "@/lib/graph-style";

export type { Community } from "@/lib/graph-style";
import type { Community } from "@/lib/graph-style";
type N = GNode & { color: string; val: number; hub: boolean; seed: boolean; x?: number; y?: number; z?: number;
  fx?: number; fy?: number; fz?: number };
type L = { source: number | N; target: number | N; type: string; weight: number; spoke: boolean };

const PALETTE = ["#6aa6ff", "#ff7aa8", "#4fd1a5", "#f5b14c", "#b48cff", "#4cc9e6", "#ff8a65", "#a3d65c",
  "#e879f9", "#2dd4bf"];
const REL = 3; // node radius = cbrt(val) * REL
const HUB = new Set(["Thread", "Event", "Organization", "Topic"]);

type Props = {
  data: Subgraph;
  dark: boolean;
  selected: number | null;
  onSelectAction: (id: number | null) => void;
  onExpandAction: (id: number) => void;
  onLinkAction: (a: GNode, b: GNode) => void;
  onCommunitiesAction: (c: Community[]) => void;
};

/** Color by community, computed without the seed (it touches everyone and would merge every group). */
function communities(data: Subgraph, seed: number | undefined) {
  const g = new Graph({ multi: true });
  for (const n of data.nodes) if (n.id !== seed) g.addNode(String(n.id));
  for (const e of data.edges)
    if (e.src !== seed && e.dst !== seed && g.hasNode(String(e.src)) && g.hasNode(String(e.dst)))
      g.addEdge(String(e.src), String(e.dst), { weight: e.weight ?? 1 });
  const comm = g.size ? louvain(g, { getEdgeWeight: "weight", resolution: 1.05 }) : {};
  const groups = new Map<number, number[]>();
  for (const [k, c] of Object.entries(comm)) groups.set(c, [...(groups.get(c) ?? []), Number(k)]);
  return [...groups.values()].sort((a, b) => b.length - a.length);
}

export default function Graph3D({ data, dark, selected, onSelectAction, onExpandAction, onLinkAction,
  onCommunitiesAction }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const fg = useRef<ForceGraph3DInstance<N, L> | null>(null);
  const cb = useRef({ onSelectAction, onExpandAction, onLinkAction });
  cb.current = { onSelectAction, onExpandAction, onLinkAction };
  const sel = useRef<number | null>(selected);
  sel.current = selected;
  const fitted = useRef(false);
  const labelIds = useRef<Set<number>>(new Set());

  // Create once.
  useEffect(() => {
    const el = host.current!;
    const g = new ForceGraph3D(el, { controlType: "orbit" }) as unknown as ForceGraph3DInstance<N, L>;
    guardOrbitPointerUp(g.controls() as unknown as OrbitInternals);
    g.width(el.clientWidth).height(el.clientHeight)
      .showNavInfo(false)
      .nodeId("id")
      .nodeVal("val")
      .nodeRelSize(REL)
      .nodeResolution(16)
      .nodeOpacity(0.95)
      .linkOpacity(0.35)
      .linkCurvature(0.08)
      .enableNodeDrag(false) // a click is only a click: dragging pinned and nudged nodes
      // Name labels float beside important nodes; everything else names itself on hover.
      .nodeThreeObjectExtend(true)
      .nodeLabel((n) => `<div class="g3-tip"><b>${escapeHtml(n.name)}</b><span>${describe(n)}</span></div>`)
      .linkLabel((l) => `<div class="g3-tip"><span>${linkReason(l)}</span><em>Click to see why</em></div>`)
      .onNodeClick((n) => {
        // One click: select it and reveal its top connections (clicking again collapses them).
        cb.current.onSelectAction(n.id);
        if (n.expandable !== false) cb.current.onExpandAction(n.id);
      })
      .onLinkClick((l) => {
        const a = l.source, b = l.target;
        if (typeof a === "object" && typeof b === "object" && a && b) cb.current.onLinkAction(a as N, b as N);
      })
      .onBackgroundClick(() => cb.current.onSelectAction(null))
      .onNodeHover((n) => { el.style.cursor = n ? "pointer" : ""; })
      .onLinkHover((l) => { el.style.cursor = l ? "pointer" : ""; })
      .cooldownTicks(400)
      .d3AlphaDecay(0.03)          // settle sooner, so the fit below frames where nodes actually end up
      .d3VelocityDecay(0.35)
      .onEngineStop(() => {
        // Frame the whole graph once the layout settles (only on the first settle, so it doesn't fight you),
        // and fit the fog to the graph's real size: nodes far behind fade, the near side stays crisp.
        if (fitted.current) return;
        fitted.current = true;
        const ns = g.graphData().nodes;
        const radius = Math.max(60, ...ns.map((n) => Math.hypot(n.x ?? 0, n.y ?? 0, n.z ?? 0)));
        const dist = frameAll(g, 700);
        // The layout keeps drifting a little after the engine "stops"; re-frame once it's truly still.
        setTimeout(() => frameAll(g, 600), 900);
        // Fog only on the far side of the graph: the near half stays crisp and the back half recedes, which is
        // what makes it read as depth. Measured from the camera's fit distance so it never washes out the front.
        const fog = g.scene().fog as Fog | null;
        if (fog) { fog.near = dist + radius * 0.8; fog.far = dist + radius * 4; }
      });
    // Spread groups apart; let strongly linked people sit close.
    g.d3Force("charge")?.strength?.(-45);
    g.d3Force("link")?.distance?.((l: L) => (l.spoke ? 60 : l.type === "PART_OF" ? 18 : 26 / Math.sqrt(l.weight || 1)));
    fg.current = g;
    const ro = new ResizeObserver(() => g.width(el.clientWidth).height(el.clientHeight));
    ro.observe(el);
    return () => { ro.disconnect(); g._destructor(); el.innerHTML = ""; };
  }, []);

  // Theme.
  useEffect(() => {
    const g = fg.current;
    if (!g) return;
    const bg = dark ? "#151714" : "#f6f6f3";
    g.backgroundColor(bg);
    const prev = g.scene().fog as Fog | null;
    // Start with no visible fog (near/far huge) until the layout settles and onEngineStop sizes it.
    g.scene().fog = new Fog(bg, prev?.near ?? 1e5, prev?.far ?? 2e5);
  }, [dark]);

  // Data: keep positions of nodes we already have, so expanding grows the graph instead of reshuffling it.
  useEffect(() => {
    const g = fg.current;
    if (!g) return;
    const seed = data.nodes[0]?.id;
    const groups = communities(data, seed);
    const colorOf = new Map<number, string>();
    const legend: Community[] = [];
    groups.forEach((members, i) => {
      const color = members.length > 1 ? PALETTE[i % PALETTE.length] : dark ? "#6b7069" : "#a8aca3";
      members.forEach((m) => colorOf.set(m, color));
      const people = members.map((m) => data.nodes.find((n) => n.id === m)).filter((n) => n && !HUB.has(n.type));
      if (members.length > 1 && people.length)
        legend.push({ color, size: people.length, members: people.slice(0, 3).map((p) => firstName(p!.name)) });
    });
    onCommunitiesAction(legend.slice(0, 8));

    const tie = new Map<number, number>();
    for (const e of data.edges) {
      if (e.src === seed) tie.set(e.dst, (tie.get(e.dst) ?? 0) + (e.weight ?? 1));
      if (e.dst === seed) tie.set(e.src, (tie.get(e.src) ?? 0) + (e.weight ?? 1));
      if (e.type === "PART_OF") tie.set(e.dst, (tie.get(e.dst) ?? 0) + (e.weight ?? 1));
    }
    const max = Math.max(1, ...[...tie.values()]);
    const old = new Map(g.graphData().nodes.map((n) => [n.id, n]));
    const ranked = [...data.nodes].sort((a, b) => (tie.get(b.id) ?? 0) - (tie.get(a.id) ?? 0));
    const labeled = new Set(ranked.filter((n) => !HUB.has(n.type)).slice(0, 10).map((n) => n.id));

    const nodes: N[] = data.nodes.map((n) => {
      const hub = HUB.has(n.type);
      const prev = old.get(n.id);
      const base: N = {
        ...n, hub, seed: n.id === seed,
        color: n.id === seed ? (dark ? "#f2f2ee" : "#1f2420")
          : n.kind === "category" ? (CATEGORY_COLOR[n.name] ?? "#a8aca3")
          : (n.category ?? TYPE_CATEGORY[n.type]) ? (CATEGORY_COLOR[(n.category ?? TYPE_CATEGORY[n.type])!] ?? "#a8aca3")
          : hub ? (dark ? "#5c6159" : "#b9bcb4") : colorOf.get(n.id) ?? "#a8aca3",
        // Biggest = strongest relationship to you. Category hubs are mid-sized landmarks.
        val: n.id === seed ? 14 : n.kind === "category" ? 6
          : n.strength !== undefined ? 1.2 + 10 * n.strength ** 1.6
          : hub ? 1.2 : 1.5 + 9 * Math.sqrt((tie.get(n.id) ?? 1) / max),
      };
      return prev ? Object.assign(prev, base) : base;
    });
    const ids = new Set(nodes.map((n) => n.id));
    const links: L[] = data.edges.filter((e) => ids.has(e.src) && ids.has(e.dst)).map((e: GEdge) => ({
      source: e.src, target: e.dst, type: e.type, weight: e.weight ?? 1, spoke: e.src === seed || e.dst === seed,
    }));

    labelIds.current = labeled;
    g.nodeThreeObject(labelFor(labeled, sel.current, dark))
      .linkColor((l: L) => (l.spoke ? (dark ? "#3a3d38" : "#d8dad4") : l.type === "PART_OF"
        ? (dark ? "#585c55" : "#c3c6be") : (dark ? "#8a9187" : "#8b9187")))
      .linkWidth((l: L) => (l.spoke ? 0.2 : l.type === "PART_OF" ? 0.4 : Math.min(3, 0.6 + Math.log2(l.weight || 1) * 0.5)))
      // A little movement along strong ties shows which connections carry the most traffic.
      .linkDirectionalParticles((l: L) => (!l.spoke && l.weight >= 5 ? 2 : 0))
      .linkDirectionalParticleWidth(1.4)
      .linkDirectionalParticleColor(() => (dark ? "#c9e7d7" : "#4b7f63"))
      .graphData({ nodes, links });

  }, [data, dark, onCommunitiesAction]);

  // Selecting a node (by click or from the inspector) brings it into view, never closer than a comfortable distance.
  useEffect(() => {
    const g = fg.current;
    if (!g || selected === null) return;
    const n = g.graphData().nodes.find((m) => m.id === selected);
    if (n) flyTo(g, n);
  }, [selected]);

  // Selection: dim everything not connected to the selected node.
  useEffect(() => {
    const g = fg.current;
    if (!g) return;
    const s = selected;
    const near = new Set<number>(s === null ? [] : [s]);
    if (s !== null)
      for (const l of g.graphData().links) {
        const a = endId(l.source), b = endId(l.target);
        if (a === undefined || b === undefined) continue;
        if (a === s) near.add(b);
        if (b === s) near.add(a);
      }
    g.nodeThreeObject(labelFor(labelIds.current, s, dark));
    g.nodeVisibility(() => true)
      .nodeOpacity(s === null ? 0.95 : 0.9)
      .nodeColor((n: N) => (s === null || near.has(n.id) ? n.color : dark ? "#2b2e29" : "#e2e3de"))
      .linkVisibility((l: L) => {
        const a = endId(l.source), b = endId(l.target);
        if (a === undefined || b === undefined) return false;
        return s === null || a === s || b === s || (near.has(a) && near.has(b));
      });
  }, [selected, dark]);

  return <div ref={host} className="absolute inset-0 cursor-grab active:cursor-grabbing" />;
}

/** Names float beside the seed, the strongest few nodes, and the selected one; everything else names itself on hover. */
function labelFor(labeled: Set<number>, selected: number | null, dark: boolean) {
  return (n: N) => {
    if (!(n.seed || n.id === selected || labeled.has(n.id))) return undefined as unknown as never;
    const t = new SpriteText(n.id === selected ? n.name.slice(0, 32) : firstName(n.name));
    t.color = dark ? "#e8e8e3" : "#2b302b";
    t.textHeight = n.seed || n.id === selected ? 6 : 4;
    t.fontFace = "ui-sans-serif, system-ui";
    t.position.set(0, -(Math.cbrt(n.val) * REL + 5), 0);
    return t;
  };
}

/** Frame every node from a comfortable distance (fixed field of view, so distance is what sets the size). It never
 *  presses the camera against a lone node, and never pulls in closer than the user already is when few nodes show. */
function frameAll(g: ForceGraph3DInstance<N, L>, ms: number): number {
  const nodes = g.graphData().nodes;
  if (!nodes.length) return 0;
  const c = { x: 0, y: 0, z: 0 };
  for (const n of nodes) { c.x += (n.x ?? 0) / nodes.length; c.y += (n.y ?? 0) / nodes.length; c.z += (n.z ?? 0) / nodes.length; }
  const r = Math.max(40, ...nodes.map((n) => Math.hypot((n.x ?? 0) - c.x, (n.y ?? 0) - c.y, (n.z ?? 0) - c.z) + Math.cbrt(n.val) * REL));
  const dist = Math.max(nodes.length <= 3 ? 420 : 200, r * 2.4);
  const cam = g.camera().position;
  const dir = { x: cam.x - c.x, y: cam.y - c.y, z: cam.z - c.z };
  const len = Math.hypot(dir.x, dir.y, dir.z);
  const u = len > 1 ? { x: dir.x / len, y: dir.y / len, z: dir.z / len } : { x: 0, y: 0, z: 1 };
  if (nodes.length <= 3 && len >= dist) return len; // already looking from far enough
  g.cameraPosition({ x: c.x + u.x * dist, y: c.y + u.y * dist, z: c.z + u.z * dist }, c, ms);
  return dist;
}

/** Re-centre the camera on a node without ever ending inside it or right on top of it. It keeps the zoom the user
 *  chose (never pulling closer than they already are) but not nearer than the larger of 120, eight node radii, and
 *  the graph's own radius. With only a few nodes there is nothing to zoom into, so frame them all instead. */
function flyTo(g: ForceGraph3DInstance<N, L>, n: N) {
  const nodes = g.graphData().nodes;
  if (nodes.length <= 3) return void frameAll(g, 700);
  const at = { x: n.x ?? 0, y: n.y ?? 0, z: n.z ?? 0 };
  const radius = Math.max(60, ...nodes.map((m) => Math.hypot(m.x ?? 0, m.y ?? 0, m.z ?? 0)));
  const floor = Math.max(120, Math.cbrt(n.val) * REL * 8, radius);
  const cam = g.camera().position;
  const v = { x: cam.x - at.x, y: cam.y - at.y, z: cam.z - at.z };
  const len = Math.hypot(v.x, v.y, v.z);
  const dist = Math.max(floor, len);
  const u = len > 1 ? { x: v.x / len, y: v.y / len, z: v.z / len } : { x: 0, y: 0, z: 1 };
  g.cameraPosition({ x: at.x + u.x * dist, y: at.y + u.y * dist, z: at.z + u.z * dist }, at, 900);
}

// The force engine replaces link endpoints (numeric ids) with node objects only on its next tick. Code that
// reads links in between (selection, hover, a click mid-expansion) must accept either form, and must never
// touch .x on an endpoint that is still an id, which is the "reading 'x' of undefined" crash.
function endId(e: number | N | undefined): number | undefined {
  return typeof e === "object" && e !== null ? e.id : e;
}

function firstName(name: string) {
  if (/^\+?\d[\d\s()-]{6,}$/.test(name)) return name; // a bare phone number: show it whole
  const clean = name.replace(/\s*\(.*?\)\s*/g, " ").trim();
  return clean.length <= 18 ? clean : clean.split(/\s+/).slice(0, 2).join(" ");
}

function describe(n: N) {
  if (n.seed) return "You";
  if (n.kind === "category") return `${n.total} ${n.name.toLowerCase()} · click to show more`;
  if (n.type === "Thread") return n.members ? `Conversation with ${n.members} of your contacts` : "Conversation";
  if (n.type === "Event") return n.members ? `Event with ${n.members} of your contacts` : "Event";
  return n.type;
}

function linkReason(l: L) {
  if (typeof l.source !== "object" || typeof l.target !== "object") return "";
  const a = (l.source as N).name, b = (l.target as N).name;
  if (l.type === "HAS" || l.type === "IN_CATEGORY") return escapeHtml(b);
  if (l.type === "PART_OF") return `${escapeHtml(firstName(a))} in ${escapeHtml(b)} · ${l.weight} message${l.weight === 1 ? "" : "s"}`;
  return `${escapeHtml(firstName(a))} and ${escapeHtml(firstName(b))} · ${l.weight} shared message${l.weight === 1 ? "" : "s"}`;
}

function escapeHtml(s: string) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

type OrbitInternals = {
  domElement: HTMLElement;
  _pointers: number[];
  _pointerPositions: Record<number, unknown>;
  _onPointerUp: (e: PointerEvent) => void;
};

/** Ending a node drag makes 3d-force-graph dispatch a synthetic pointerup with an id OrbitControls never saw.
 *  OrbitControls then thinks a pointer is still down whose position it already forgot, and crashes reading
 *  `position.x`. Drop pointers without a position before the real handler runs. */
function guardOrbitPointerUp(c: OrbitInternals) {
  if (!c?._onPointerUp || !c.domElement) return;
  const doc = c.domElement.ownerDocument;
  const original = c._onPointerUp;
  const safe = (e: PointerEvent) => {
    c._pointers = c._pointers.filter((id) => id === e.pointerId || c._pointerPositions[id] !== undefined);
    // The synthetic event's pointer was never captured, so releasePointerCapture throws NotFoundError.
    if (!c._pointers.includes(e.pointerId)) return;
    try {
      original(e);
    } catch (err) {
      if (!(err instanceof DOMException && err.name === "NotFoundError")) throw err;
    }
  };
  doc.removeEventListener("pointerup", original);
  c.domElement.removeEventListener("pointercancel", original);
  c._onPointerUp = safe;
  doc.addEventListener("pointerup", safe);
  c.domElement.addEventListener("pointercancel", safe);
}
