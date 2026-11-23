"use client";

import ForceGraph3D, { type ForceGraph3DInstance } from "3d-force-graph";
import { useEffect, useEffectEvent, useMemo, useRef, useState } from "react";
import { DoubleSide, Fog, Group, Mesh, MeshBasicMaterial, RingGeometry } from "three";
import SpriteText from "three-spritetext";
import { useTheme } from "@/components/ThemeToggle";
import type { GEdge, GNode, Subgraph } from "@/lib/api";
import { sectionHue, sectionOf, themeColors, type ThemeColors } from "@/lib/graph-style";

type N = GNode & { val: number; seed: boolean; x?: number; y?: number; z?: number };
type L = { source: number | N; target: number | N; type: string; weight: number; spoke: boolean };
type Point = { x: number; y: number; z: number };
type Graph = ForceGraph3DInstance<N, L>;

/** One renderer and what belongs to it. A new one is made whenever the component mounts or its page is shown again. */
type Scene = {
  g: Graph;
  seed: number | null; // the centre whose layout is on screen
  framed: boolean; // the camera has framed this centre's layout once; after that it is yours
  flownTo: number | null; // the selection the camera last flew to
  fresh: Map<number, number>; // nodes that just appeared, with when: they get a brief halo
  later: (ms: number, run: () => void) => void; // a timer that is cleared with the renderer or a new centre
  cancelTimers: () => void;
};

/** Where things were when the page was hidden, so showing it again redraws the same picture. */
type Snapshot = {
  seed: number; nodes: Map<number, Point>; camera: Point; target: Point; fog: { near: number; far: number } | null;
  flownTo: number | null;
};

const REL = 3; // node radius = cbrt(val) * REL
const HALO_MS = 2400;
// Places people meet (groups, events, organizations, topics): labelled apart from people, so both kinds get names.
const HUB = new Set(["Thread", "Event", "Organization", "Topic"]);

type Props = {
  data: Subgraph;
  /** The centre: drawn largest. A new centre starts a fresh layout. */
  seed: number;
  selected: number | null;
  onSelectAction: (id: number | null) => void;
  /** "open" reveals connections if hidden (never hides); "toggle" also hides them when shown. */
  onExpandAction: (id: number, mode?: "open" | "toggle") => void;
  onLinkAction: (a: GNode, b: GNode) => void;
};

export default function Graph3D({ data, seed, selected, onSelectAction, onExpandAction, onLinkAction }: Props) {
  const dark = useTheme();
  const host = useRef<HTMLDivElement>(null);
  const scene = useRef<Scene | null>(null);
  const snapshot = useRef<Snapshot | null>(null);
  // Bumped when the newest halos have faded, so the labels redraw without them.
  const [haloTick, setHaloTick] = useState(0);
  const { weight, strongest, labeled } = useMemo(() => rank(data, seed), [data, seed]);

  const nodeClicked = useEffectEvent((n: N) => {
    // Clicking opens a node: select it and reveal its connections. Coming back to it later (after clicking
    // elsewhere) keeps them; only clicking it again while it's the selected one hides them.
    const again = selected === n.id;
    onSelectAction(n.id);
    if (n.expandable !== false) onExpandAction(n.id, again ? "toggle" : "open");
  });
  const linkClicked = useEffectEvent((l: L) => {
    if (typeof l.source === "object" && typeof l.target === "object") onLinkAction(l.source, l.target);
  });
  const backgroundClicked = useEffectEvent(() => onSelectAction(null));

  // The renderer: made once per mount, and released with its WebGL context when the component unmounts or Next hides
  // the page (both run this cleanup). Browsers allow only a few live contexts, so dispose() alone is not enough.
  useEffect(() => {
    const el = host.current!;
    const g = new ForceGraph3D(el, { controlType: "orbit" }) as unknown as Graph;
    const timers = new Set<number>();
    const s: Scene = {
      g, seed: null, framed: false, flownTo: null, fresh: new Map(),
      later: (ms, run) => {
        const t = window.setTimeout(() => { timers.delete(t); run(); }, ms);
        timers.add(t);
      },
      cancelTimers: () => {
        timers.forEach((t) => window.clearTimeout(t));
        timers.clear();
      },
    };
    // Development only: lets browser checks find a node's screen position (graph2ScreenCoords) and click it.
    if (process.env.NODE_ENV === "development") (window as unknown as { __fg?: Graph }).__fg = g;
    g.width(el.clientWidth).height(el.clientHeight)
      .showNavInfo(false)
      .nodeId("id")
      .nodeVal("val")
      .nodeRelSize(REL)
      .nodeResolution(16)
      .linkOpacity(0.35)
      .linkCurvature(0.08)
      .linkDirectionalParticleWidth(1.4)
      .enableNodeDrag(false) // a click is only a click: dragging pinned and nudged nodes
      // Name labels float beside important nodes; everything else names itself on hover.
      .nodeThreeObjectExtend(true)
      .nodeLabel((n) => `<div class="g3-tip"><b>${escapeHtml(n.name)}</b><span>${describe(n)}</span></div>`)
      .linkLabel((l) => `<div class="g3-tip"><span>${linkReason(l)}</span><em>Click to see why</em></div>`)
      .onNodeClick((n) => nodeClicked(n))
      .onLinkClick((l) => linkClicked(l))
      .onBackgroundClick(() => backgroundClicked())
      .onNodeHover((n) => { el.style.cursor = n ? "pointer" : ""; })
      .onLinkHover((l) => { el.style.cursor = l ? "pointer" : ""; })
      .d3AlphaDecay(0.03) // settle sooner, so the framing below frames where nodes actually end up
      .d3VelocityDecay(0.35)
      .onEngineStop(() => {
        // Frame the whole graph once the layout settles (only the first time for a centre, so it doesn't fight you),
        // and fit the fog to the graph's real size: nodes far behind fade, the near side stays crisp.
        if (s.framed) return;
        s.framed = true;
        const radius = Math.max(60, ...g.graphData().nodes.map((n) => Math.hypot(n.x ?? 0, n.y ?? 0, n.z ?? 0)));
        const dist = frameAll(g, 700, true);
        // The layout keeps drifting a little after the engine "stops"; re-frame once it's truly still.
        s.later(900, () => frameAll(g, 600));
        // Fog only on the far side of the graph, measured from the camera's fit distance so it never washes out
        // the front: the back half recedes, which is what makes it read as depth.
        const fog = g.scene().fog as Fog | null;
        if (fog) { fog.near = dist + radius * 0.8; fog.far = dist + radius * 4; }
      });
    // Spread groups apart; let strongly linked people sit close.
    g.d3Force("charge")?.strength?.(-45);
    g.d3Force("link")?.distance?.((l: L) => (l.spoke ? 60 : l.type === "PART_OF" ? 18 : 26 / Math.sqrt(l.weight || 1)));
    const resize = new ResizeObserver(() => g.width(el.clientWidth).height(el.clientHeight));
    resize.observe(el);
    scene.current = s;

    return () => {
      // Only a picture that has settled is worth keeping (React's development remount comes before any layout), and
      // only nodes the layout has placed.
      if (s.seed !== null && s.framed) {
        const fog = g.scene().fog as Fog | null;
        const placed = g.graphData().nodes.filter((n) => n.x !== undefined && n.y !== undefined && n.z !== undefined);
        snapshot.current = {
          seed: s.seed,
          nodes: new Map(placed.map((n) => [n.id, { x: n.x!, y: n.y!, z: n.z! }])),
          camera: { ...g.camera().position },
          target: { ...(g.controls() as { target: Point }).target },
          fog: fog ? { near: fog.near, far: fog.far } : null,
          flownTo: s.flownTo,
        };
      } else {
        snapshot.current = null;
      }
      resize.disconnect();
      s.cancelTimers();
      const renderer = g.renderer();
      g._destructor();
      renderer.forceContextLoss();
      el.replaceChildren();
      scene.current = null;
      const w = window as unknown as { __fg?: Graph };
      if (w.__fg === g) delete w.__fg;
    };
  }, []);

  // Theme: the background and the fog's color.
  useEffect(() => {
    const g = scene.current?.g;
    if (!g) return;
    const { bg } = themeColors();
    g.backgroundColor(bg);
    const prev = g.scene().fog as Fog | null;
    // No visible fog (near/far huge) until the layout settles and onEngineStop sizes it.
    g.scene().fog = new Fog(bg, prev?.near ?? 1e5, prev?.far ?? 2e5);
  }, [dark]);

  // Data: keep the positions of nodes already shown, so expanding grows the graph instead of reshuffling it. A new
  // centre is a new picture, laid out and framed from scratch.
  useEffect(() => {
    const s = scene.current;
    if (!s) return;
    const g = s.g;
    const restore = snapshot.current?.seed === seed ? snapshot.current : null;
    snapshot.current = null;
    const sameCentre = s.seed === seed;
    if (!sameCentre) {
      s.seed = seed;
      s.framed = false;
      s.fresh.clear();
      s.cancelTimers(); // framing and halos meant for the previous centre
    }
    const old = new Map(sameCentre ? g.graphData().nodes.map((n) => [n.id, n]) : []);
    const now = performance.now();
    const nodes: N[] = data.nodes.map((n) => {
      const node: N = {
        ...n, seed: n.id === seed,
        // Biggest = strongest relationship to the centre.
        val: n.id === seed ? 14
          : n.strength !== undefined ? 1.2 + 10 * n.strength ** 1.6
          : HUB.has(n.type) ? 1.2 : 1.5 + 9 * Math.sqrt((weight.get(n.id) ?? 1) / strongest),
      };
      const prev = old.get(n.id);
      if (prev) return Object.assign(prev, node);
      const at = restore?.nodes.get(n.id);
      if (old.size) s.fresh.set(n.id, now);
      return at ? { ...node, ...at } : node;
    });
    for (const [id, born] of s.fresh) if (now - born >= HALO_MS) s.fresh.delete(id);
    if (s.fresh.size) s.later(HALO_MS + 50, () => setHaloTick((t) => t + 1));

    const ids = new Set(nodes.map((n) => n.id));
    const links: L[] = data.edges.filter((e) => ids.has(e.src) && ids.has(e.dst)).map((e: GEdge) => ({
      source: e.src, target: e.dst, type: e.type, weight: e.weight ?? 1, spoke: e.src === seed || e.dst === seed,
    }));
    // Shown again with nothing new: every node goes back where it was and the layout stays still.
    const still = restore !== null && nodes.every((n) => restore.nodes.has(n.id));
    g.cooldownTicks(still ? 0 : 250).graphData({ nodes, links });

    if (restore) {
      g.cameraPosition(restore.camera, restore.target);
      if (restore.fog) {
        const fog = g.scene().fog as Fog | null;
        if (fog) { fog.near = restore.fog.near; fog.far = restore.fog.far; }
      }
      s.framed = true;
      s.flownTo = restore.flownTo; // the camera already shows the selection it flew to before
    } else if (!s.framed) {
      // Frame early (the layout is mostly in place within a second), then again when it fully settles.
      s.later(1200, () => { if (!s.framed) frameAll(g, 500, true); });
    }
  }, [data, seed, weight, strongest]);

  // The one place labels and highlights are drawn. Selection lights up what the click is about: the node (ringed,
  // full name), everything it connects to (full color, every one named), the lines between them (accent, thicker,
  // moving), and the chain of nodes back to the centre when it isn't directly connected. The rest dims but stays
  // visible so you keep your bearings. Runs again when the data, the selection, the theme or a fading halo changes.
  useEffect(() => {
    const s = scene.current;
    if (!s) return;
    const g = s.g;
    const colors = themeColors();
    const { nodes, links } = g.graphData();
    const centre = nodes.find((n) => n.seed)?.id;
    const adj = new Map<number, number[]>();
    for (const l of links) {
      const a = endId(l.source), b = endId(l.target);
      if (a === undefined || b === undefined) continue;
      adj.set(a, [...(adj.get(a) ?? []), b]);
      adj.set(b, [...(adj.get(b) ?? []), a]);
    }
    const near = new Set<number>(selected === null ? [] : [selected, ...(adj.get(selected) ?? [])]);
    const path = selected === null || centre === undefined ? [] : shortestPath(adj, selected, centre);
    const onPath = new Set(path);
    const pathEdge = new Set(path.slice(1).map((x, i) => edgeKey(path[i], x)));
    const lit = (id: number) => near.has(id) || onPath.has(id);
    const isHot = (l: L) => {
      const a = endId(l.source), b = endId(l.target);
      return selected !== null && a !== undefined && b !== undefined
        && (a === selected || b === selected || pathEdge.has(edgeKey(a, b)));
    };
    const color = (n: N) => (n.seed ? colors.ink : sectionHue(sectionOf(n)) ?? colors.muted);
    const lineColor = (l: L) => (l.spoke ? colors.spoke : l.type === "PART_OF" ? colors.structure : colors.tie);
    const lineWidth = (l: L) => (l.spoke ? 0.2 : l.type === "PART_OF" ? 0.4 : Math.min(3, 0.6 + Math.log2(l.weight || 1) * 0.5));
    const named = selected === null ? labeled : new Set([...labeled, ...near, ...onPath]);

    g.nodeThreeObject(labelFor(named, selected, colors, s.fresh, color))
      .nodeOpacity(selected === null ? 0.95 : 0.92)
      .nodeColor((n: N) => (selected === null || lit(n.id) ? color(n) : colors.line))
      .linkVisibility((l: L) => {
        const a = endId(l.source), b = endId(l.target);
        if (a === undefined || b === undefined) return false;
        return selected === null || (lit(a) && lit(b));
      })
      .linkColor((l: L) => (isHot(l) ? colors.accent : lineColor(l)))
      .linkWidth((l: L) => (isHot(l) ? 1.6 : lineWidth(l)))
      // A little movement along strong ties shows which connections carry the most traffic.
      .linkDirectionalParticles((l: L) => (isHot(l) ? 3 : !l.spoke && l.weight >= 5 ? 2 : 0))
      .linkDirectionalParticleSpeed((l: L) => (isHot(l) ? 0.008 : 0.004))
      .linkDirectionalParticleColor((l: L) => (isHot(l) ? colors.accent : colors.flow));
  }, [data, seed, selected, dark, haloTick, labeled]);

  // Selecting a node (by click or from the panel) brings it into view, never closer than a comfortable distance.
  useEffect(() => {
    const s = scene.current;
    if (!s || selected === null || s.flownTo === selected) return;
    s.flownTo = selected;
    const n = s.g.graphData().nodes.find((m) => m.id === selected);
    if (n) flyTo(s.g, n);
  }, [selected]);

  return <div ref={host} className="absolute inset-0 cursor-grab active:cursor-grabbing" />;
}

/** How strongly each node is tied to the centre, and which nodes get a name label: the strongest people and the
 *  strongest few groups, events and projects (unnamed dots read as noise). */
function rank(data: Subgraph, seed: number) {
  const weight = new Map<number, number>();
  for (const e of data.edges) {
    if (e.src === seed) weight.set(e.dst, (weight.get(e.dst) ?? 0) + (e.weight ?? 1));
    if (e.dst === seed) weight.set(e.src, (weight.get(e.src) ?? 0) + (e.weight ?? 1));
    if (e.type === "PART_OF") weight.set(e.dst, (weight.get(e.dst) ?? 0) + (e.weight ?? 1));
  }
  const ranked = [...data.nodes].sort((a, b) => (weight.get(b.id) ?? 0) - (weight.get(a.id) ?? 0));
  const labeled = new Set([...ranked.filter((n) => !HUB.has(n.type)).slice(0, 8),
                           ...ranked.filter((n) => HUB.has(n.type) || n.type === "Project").slice(0, 5)].map((n) => n.id));
  return { weight, strongest: Math.max(1, ...weight.values()), labeled };
}

/** Names float beside the centre, the strongest few nodes, and (while something is selected) the selected node, its
 *  connections and the path to the centre. The selected node gets a ring; nodes that just appeared get a brief halo. */
function labelFor(named: Set<number>, selected: number | null, colors: ThemeColors, fresh: Map<number, number>,
  color: (n: N) => string) {
  const now = performance.now();
  return (n: N) => {
    const r = Math.cbrt(n.val) * REL;
    const group = new Group();
    if (n.seed || n.id === selected || named.has(n.id)) {
      const t = new SpriteText(n.id === selected || n.seed ? n.name.slice(0, 32) : shortName(n.name));
      t.color = colors.ink;
      t.textHeight = n.seed || n.id === selected ? 6 : 4;
      t.fontFace = "ui-sans-serif, system-ui";
      t.position.set(0, -(r + 5), 0);
      group.add(t);
    }
    if (n.id === selected) group.add(ring(r * 1.6, colors.accent, 0.9));
    const born = fresh.get(n.id);
    if (born !== undefined && now - born < HALO_MS) group.add(ring(r * 2.2, color(n), 0.35));
    return group.children.length ? group : (undefined as unknown as Group);
  };
}

function ring(radius: number, color: string, opacity: number) {
  const m = new Mesh(new RingGeometry(radius, radius * 1.12, 48),
    new MeshBasicMaterial({ color, transparent: true, opacity, side: DoubleSide, depthWrite: false }));
  m.onBeforeRender = (_r, _s, camera) => m.quaternion.copy(camera.quaternion); // always face the viewer
  return m;
}

function edgeKey(a: number, b: number) {
  return a < b ? `${a}-${b}` : `${b}-${a}`;
}

/** Breadth-first: the chain of visible nodes linking `from` to `to` (empty if none or already adjacent). */
function shortestPath(adj: Map<number, number[]>, from: number, to: number): number[] {
  if (from === to) return [];
  const prev = new Map<number, number>([[from, from]]);
  const queue = [from];
  while (queue.length) {
    const x = queue.shift()!;
    if (x === to) break;
    for (const y of adj.get(x) ?? []) if (!prev.has(y)) { prev.set(y, x); queue.push(y); }
  }
  if (!prev.has(to)) return [];
  const out = [to];
  while (out[out.length - 1] !== from) out.push(prev.get(out[out.length - 1])!);
  return out.length > 2 ? out.reverse() : [];
}

function shortName(name: string) {
  return name.length <= 18 ? name : firstName(name);
}

/** Frame every node from a comfortable distance (fixed field of view, so distance is what sets the size). It never
 *  presses the camera against a lone node, and never pulls in closer than the user already is when few nodes show. */
function frameAll(g: Graph, ms: number, first = false): number {
  const nodes = g.graphData().nodes;
  if (!nodes.length) return 0;
  const c = { x: 0, y: 0, z: 0 };
  for (const n of nodes) { c.x += (n.x ?? 0) / nodes.length; c.y += (n.y ?? 0) / nodes.length; c.z += (n.z ?? 0) / nodes.length; }
  // Frame the bulk of the graph (90th percentile of distances), so one stray node can't shrink everything else.
  const ds = nodes.map((n) => Math.hypot((n.x ?? 0) - c.x, (n.y ?? 0) - c.y, (n.z ?? 0) - c.z) + Math.cbrt(n.val) * REL)
    .sort((a, b) => a - b);
  const r = Math.max(40, ds[Math.min(ds.length - 1, Math.floor(ds.length * 0.9))]);
  const dist = Math.max(nodes.length <= 3 ? 220 : 160, r * 2.2);
  const cam = g.camera().position;
  const dir = { x: cam.x - c.x, y: cam.y - c.y, z: cam.z - c.z };
  const len = Math.hypot(dir.x, dir.y, dir.z);
  const u = len > 1 ? { x: dir.x / len, y: dir.y / len, z: dir.z / len } : { x: 0, y: 0, z: 1 };
  if (!first && nodes.length <= 3 && len >= dist) return len; // keep a zoom the user chose; the first fit sets it
  g.cameraPosition({ x: c.x + u.x * dist, y: c.y + u.y * dist, z: c.z + u.z * dist }, c, ms);
  return dist;
}

/** Re-centre the camera on a node without ever ending inside it or right on top of it. It keeps the zoom the user
 *  chose (never pulling closer than they already are) but not nearer than the larger of 120, eight node radii, and
 *  the graph's own radius. With only a few nodes there is nothing to zoom into, so frame them all instead. */
function flyTo(g: Graph, n: N) {
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

// The force engine replaces link endpoints (numeric ids) with node objects only on its next tick. Code that reads
// links in between (selection, hover, a click mid-expansion) must accept either form, and must never touch .x on an
// endpoint that is still an id, which is the "reading 'x' of undefined" crash.
function endId(e: number | N | undefined): number | undefined {
  return typeof e === "object" && e !== null ? e.id : e;
}

function firstName(name: string) {
  if (/^\+?\d[\d\s()-]{6,}$/.test(name)) return name; // a bare phone number: show it whole
  const clean = name.replace(/\s*\(.*?\)\s*/g, " ").trim();
  return clean.length <= 18 ? clean : clean.split(/\s+/).slice(0, 2).join(" ");
}

function describe(n: N) {
  // The section (what the color means) leads, with a dot in that color.
  const section = sectionOf(n);
  const swatch = section
    ? `<i style="display:inline-block;width:7px;height:7px;border-radius:9px;margin-right:5px;background:${sectionHue(section)}"></i>${escapeHtml(section)}`
    : "";
  const detail = n.seed ? "the centre of this graph"
    : n.type === "Thread" ? (n.members ? `conversation with ${n.members} of your contacts` : "conversation")
    : n.type === "Event" ? (n.members ? `event with ${n.members} of your contacts` : "event") : "";
  return [swatch, detail].filter(Boolean).join(" · ") || escapeHtml(n.type);
}

/** What a line means. Only a tie between people carries a count (the messages they share); the others' weights are
 *  drawing strengths, not counts. */
function linkReason(l: L) {
  if (typeof l.source !== "object" || typeof l.target !== "object") return "";
  const a = l.source, b = l.target;
  if (l.type === "PART_OF") return `${escapeHtml(firstName(a.name))} is part of ${escapeHtml(b.name)}`;
  if (l.type === "CORRESPONDS") {
    return `${escapeHtml(firstName(a.name))} and ${escapeHtml(firstName(b.name))} · ${l.weight} shared message${l.weight === 1 ? "" : "s"}`;
  }
  return `${escapeHtml(firstName(a.name))} and ${escapeHtml(firstName(b.name))}`;
}

function escapeHtml(s: string) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}
