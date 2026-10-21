export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type GNode = {
  id: number; type: string; name: string; key: string; degree: number; members?: number;
  strength?: number; kind?: "seed" | "category" | "entity"; category?: string | null; total?: number; expandable?: boolean;
};
export type GEdge = { id: number; src: number; dst: number; type: string; extractor: string; confidence: number | null; weight?: number };
export type Subgraph = { nodes: GNode[]; edges: GEdge[] };
export type GraphFilters = { source?: string; edgeTypes?: string[]; from?: string; to?: string };

export type SyncStatus = {
  connector: string;
  account: string;
  status: "known" | "never_synced";
  last_synced_at: string | null;
  cursor_age_seconds: number | null;
  item_count: number;
  item_count_scope: "connector";
  cursor: {
    present: boolean;
    keys: string[];
    gmail_backfill_in_progress: boolean;
  };
};

function graphParams(filters: GraphFilters = {}) {
  return { source: filters.source, types: filters.edgeTypes, from: filters.from, to: filters.to };
}

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

export type ExpertEvidence = {
  source_item_id: number;
  source: string;
  external_id: string;
  title: string | null;
  occurred_at: string | null;
  involvement: ("authored" | "mentioned" | "attended")[];
  excerpt: string;
};

export type Expert = {
  id: number;
  name: string;
  score: number;
  evidence_count: number;
  evidence: ExpertEvidence[];
};

export type AskResult = {
  answerable: boolean;
  answer: string;
  citations: Hit[];
  evidence: Hit[];
  timing: { retrieve_s: number; total_s?: number };
};

export type Evidence = {
  id: number;
  type: string;
  confidence: number | null;
  model: string | null;
  extractor: string;
  extractor_version: string;
  props: { role?: string; quote?: string };
  title: string | null;
  occurred_at: string | null;
  external_id: string;
  window: string;
  hl_start: number;
  hl_end: number;
  other_name: string;
  other_type: string;
};

export type Explanation = {
  a: { id: number; name: string };
  b: { id: number; name: string };
  messages: number;
  conversations: { thread_id: number; thread: string; messages: number; people: number; first: string | null;
    last: string | null; source: string; excerpt: string | null }[];
  events: { id: number; name: string; at: string | null }[];
};

export type TimelineEdge = {
  id: number;
  type: string;
  direction: "outgoing" | "incoming";
  counterpart: { id: number; type: string };
  interval: { from: string | null; to: string | null };
  provenance: {
    extractor: string;
    extractor_version: string;
    model: string | null;
    confidence: number | null;
    evidence: { source_item_id: number | null; source: string | null; occurred_at: string | null; span: { start: number; end: number } | null };
  };
};

export type Timeline = {
  mode: "activity" | "relationship";
  a: { id: number; type: string };
  b: { id: number; type: string } | null;
  edges: TimelineEdge[];
  truncated: boolean;
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

async function get<T>(
  path: string,
  params: Record<string, string | number | (string | number)[] | undefined>,
  signal?: AbortSignal,
) {
  const url = new URL(path, API);
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === "") continue;
    for (const x of Array.isArray(v) ? v : [v]) url.searchParams.append(k, String(x));
  }
  const r = await fetch(url, { signal });
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return (await r.json()) as T;
}

export type AiImportResult = {
  provider: "chatgpt" | "claude_ai" | "gemini";
  conversations: number;
  items_inserted: number;
  items_updated: number;
  redactions: number;
};

// XHR rather than fetch: fetch cannot report upload progress.
function importAiExport(file: File, provider?: string, onProgress?: (fraction: number) => void) {
  return new Promise<AiImportResult>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", new URL("/import/ai-export", API).toString());
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onerror = () => reject(new Error("Could not reach the API."));
    xhr.onload = () => {
      let body: { detail?: unknown } = {};
      try { body = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status >= 200 && xhr.status < 300) return resolve(body as unknown as AiImportResult);
      reject(new Error(typeof body.detail === "string" ? body.detail : `Import failed (${xhr.status}).`));
    };
    const form = new FormData();
    form.append("file", file);
    if (provider) form.append("provider", provider);
    xhr.send(form);
  });
}

export type Job = {
  id: string;
  kind: "sync" | "pipeline" | "connect";
  source: string;
  account: string | null;
  status: "running" | "succeeded" | "failed";
  started_at: string;
  finished_at: string | null;
  progress: { items?: number; stage?: string | null; step?: number; steps?: number };
  result: Record<string, number> | null;
  error: string | null;
};

export type SourceInfo = {
  id: string;
  kind: "google" | "local" | "remote" | "import" | "planned";
  name: string;
  group: string;
  reads: string;
  steps: string[];
  cli: string | null;
  status: "not_set_up" | "ready" | "syncing" | "needs_attention";
  message: string | null;
  items: number;
  last_sync: string | null;
  accounts: { account: string; last_sync: string | null }[];
  requirements_ok: boolean;
  can_sync: boolean;
  coming_soon: boolean;
  job: Job | null;
};

async function send<T>(method: string, path: string, body?: unknown, params: Record<string, string> = {}) {
  const url = new URL(path, API);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const r = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({}))).detail;
    throw new Error(typeof detail === "string" ? detail : `${r.status} ${path}`);
  }
  return (await r.json()) as T;
}

export const api = {
  importAiExport,
  sources: (signal?: AbortSignal) => get<{ sources: SourceInfo[] }>("/sources", {}, signal),
  jobs: (signal?: AbortSignal) => get<{ jobs: Job[] }>("/jobs", {}, signal),
  syncSource: (id: string, account?: string) => send<Job>("POST", `/sources/${id}/sync`, undefined, account ? { account } : {}),
  runPipeline: () => send<Job>("POST", "/pipeline/run"),
  saveGithubToken: (token: string) => send<{ stored: boolean }>("POST", "/sources/github/token", { token }),
  googleConnectUrl: () => new URL("/sources/google/connect", API).toString(),
  stats: () => get<{ source_items: Record<string, number>; nodes: Record<string, number>; edges: Record<string, number> }>("/stats", {}),
  sync: (signal?: AbortSignal) => get<{ syncs: SyncStatus[] }>("/sync", {}, signal),
  search: (q: string, opts: { source?: string; person?: string; from?: string; to?: string; mode?: string } = {}) =>
    get<{ hits: Hit[] }>("/search", { q, ...opts }),
  experts: (q: string, opts: { mode?: string } = {}) => get<{ experts: Expert[] }>("/experts", { q, ...opts }),
  findNodes: (q: string, type?: string) => get<{ nodes: GNode[] }>("/graph/search", { q, type }),
  subgraph: (seed: number[], hops = 1, limit = 300, filters: GraphFilters = {}, signal?: AbortSignal) =>
    get<Subgraph>("/graph/subgraph", { seed, hops, limit, ...graphParams(filters) }, signal),
  people: (seed: number, limit = 60, signal?: AbortSignal) => get<Subgraph>("/graph/people", { seed, limit }, signal),
  network: (seed: number, people = 50, hubs = 30, signal?: AbortSignal) =>
    get<Subgraph>("/graph/network", { seed, people, hubs }, signal),
  ask: async (question: string, use_graph = true) => {
    const r = await fetch(new URL("/ask", API), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question, use_graph }),
    });
    if (!r.ok) throw new Error(`${r.status} ${(await r.json().catch(() => ({}))).detail ?? "/ask"}`);
    return (await r.json()) as AskResult;
  },
  evidence: (id: number) => get<{ evidence: Evidence[] }>(`/graph/node/${id}/evidence`, { limit: 30 }),
  view: (seed?: number, signal?: AbortSignal) => get<Subgraph & { seed: number }>("/graph/view", { seed }, signal),
  expandNode: (seed: number, node: number, visible: number[], offset = 0, signal?: AbortSignal) =>
    get<Subgraph>("/graph/expand", { seed, node, visible, offset }, signal),
  explain: (a: number, b: number) => get<Explanation>("/graph/explain", { a, b }),
  timeline: (a: number, b?: number, limit = 50, signal?: AbortSignal) =>
    get<Timeline>("/timeline", { a, b, limit }, signal),
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
