export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type GNode = { id: number; type: string; name: string; key: string; degree: number };
export type GEdge = { id: number; src: number; dst: number; type: string; extractor: string; confidence: number | null; weight?: number };
export type Subgraph = { nodes: GNode[]; edges: GEdge[] };

export type Hit = {
  chunk_id: number;
  score: number;
  text: string;
  node_id: number | null;
  source_item_id: number;
  source: string;
  external_id: string;
  title: string | null;
  occurred_at: string | null;
  matched: string[];
};

export type NodeDetail = GNode & {
  props: Record<string, unknown>;
  degree: Record<string, number>;
  source_item: {
    id: number;
    source: string;
    title: string | null;
    body_text: string | null;
    occurred_at: string | null;
    raw_meta: Record<string, unknown>;
  } | null;
};

async function get<T>(path: string, params: Record<string, string | number | (string | number)[] | undefined>) {
  const url = new URL(path, API);
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === "") continue;
    for (const x of Array.isArray(v) ? v : [v]) url.searchParams.append(k, String(x));
  }
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return (await r.json()) as T;
}

export const api = {
  stats: () => get<{ source_items: Record<string, number>; nodes: Record<string, number>; edges: Record<string, number> }>("/stats", {}),
  search: (q: string, opts: { source?: string; person?: string; from?: string; to?: string; mode?: string } = {}) =>
    get<{ hits: Hit[] }>("/search", { q, ...opts }),
  findNodes: (q: string, type?: string) => get<{ nodes: GNode[] }>("/graph/search", { q, type }),
  subgraph: (seed: number[], hops = 1, limit = 300, types?: string[]) =>
    get<Subgraph>("/graph/subgraph", { seed, hops, limit, types }),
  people: (seed: number, limit = 60) => get<Subgraph>("/graph/people", { seed, limit }),
  node: (id: number) => get<NodeDetail>(`/graph/node/${id}`, {}),
};

export const TYPE_COLORS: Record<string, string> = {
  Person: "#2563eb",
  Organization: "#7c3aed",
  Thread: "#d97706",
  Message: "#64748b",
  File: "#059669",
  Folder: "#0d9488",
  Event: "#db2777",
  Topic: "#ea580c",
};
