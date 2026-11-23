export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type GNode = {
  id: number; type: string; name: string; key: string; degree: number; members?: number;
  strength?: number; kind?: "seed" | "category" | "entity"; category?: string | null; total?: number; expandable?: boolean;
};
export type GEdge = { id: number; src: number; dst: number; type: string; extractor: string; confidence: number | null; weight?: number };
export type Thing = { id: number; type: string; name: string; category: string | null; context: string; score: number };
export type NodeSummary = {
  summary: string;
  highlights: { id: number; name: string; type: string; category: string | null; why: string }[];
  sources: number[];
  generated_by: "model" | "fallback";
};
export type Subgraph = { nodes: GNode[]; edges: GEdge[]; categories?: GNode[] };
export type GraphFilters = { source?: string; edgeTypes?: string[]; from?: string; to?: string };

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
  reason?: "no_opted_in_sources"; // nothing the question may read is opted in to the hosted model
  citations: Hit[];
  evidence: Hit[];
  timing: { retrieve_s: number; total_s?: number };
};

export type Explanation = {
  a: { id: number; name: string };
  b: { id: number; name: string };
  messages: number;
  conversations: { thread_id: number; thread: string; messages: number; people: number; first: string | null;
    last: string | null; source: string; excerpt: string | null }[];
  events: { id: number; name: string; at: string | null }[];
  shared: SourceCard[];
  direct: WhyEdge[];
  path: WhyEdge[] | null;
  corrections: { override_id: number | null; src: number; dst: number; src_name: string; dst_name: string;
    reason: string | null; updated_at: string }[];
};

/** A source item as lists show it: enough to display, Open and View. */
export type SourceCard = {
  id: number; source: string; title: string | null; date: string | null; snippet: string;
  open_url: string | null; open_label: string | null; local_file: boolean; kind?: string;
};
export type ItemDetail = {
  id: number; source: string; title: string | null; occurred_at: string | null; text: string;
  meta: Record<string, unknown>; open_url: string | null; open_label: string | null; local_path: string | null;
};
/** One edge with the verifier's reason (when it has run) and the items it was derived from. */
export type WhyEdge = {
  id: number; type: string; extractor: string; src: number; dst: number; src_name: string | null; dst_name: string | null;
  verdict: "keep" | "drop" | "unsure" | null; reason: string | null; why?: string | null; confidence: number | null; evidence: SourceCard[];
  from?: number; to?: number;
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

export type EntityDetail = {
  id: number; name: string; key: string; aliases: string[]; anchors: string[]; hidden: boolean;
  people: { id: number; name: string; items: number }[];
  groups: { id: number; name: string; why: string | null; days: number | null }[];
  events: { id: number; name: string; date: string | null; why: string | null }[];
  items: { id: number; type: string; name: string; why: string | null; quote: string | null; source: string | null;
    date: string | null; confidence: number | null }[];
  counts: { people: number; groups: number; events: number; items: number };
};
export type Correction = { override_id: number; op: string; id: number };


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

/** What an import route takes: files to upload, or a path the API reads in place (it runs on your computer). */
export type ImportInput = { files?: File[]; path?: string; scanDownloads?: boolean };

/** Start an import job. XHR rather than fetch: fetch cannot report upload progress. */
function startImport(route: string, input: ImportInput, onProgress?: (fraction: number) => void) {
  const form = new FormData();
  for (const f of input.files ?? []) form.append("file", f);
  if (input.path?.trim()) form.append("path", input.path.trim());
  if (input.scanDownloads) form.append("scan_downloads", "true");
  return new Promise<Job>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", new URL(route, API).toString());
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onerror = () => reject(new Error("Could not reach the API."));
    xhr.onload = () => {
      let body: { detail?: unknown } = {};
      try { body = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status >= 200 && xhr.status < 300) return resolve(body as unknown as Job);
      reject(new Error(typeof body.detail === "string" ? body.detail : `Import failed (${xhr.status}).`));
    };
    xhr.send(form);
  });
}

/** The stats every import's job result carries. */
type ImportStats = { inserted: number; updated: number; unchanged: number; redactions: number };
export type AiImportResult = ImportStats & { provider: "chatgpt" | "claude_ai" | "gemini"; conversations: number };
export type TakeoutResult = ImportStats & {
  places: number; parts: Record<string, number>; counts: Record<string, number>; notes: string[];
};
export type LinkedInResult = ImportStats & { connections: number; positions: number; education: number; profile: number };

export type Job = {
  id: string;
  kind: "sync" | "pipeline" | "import";
  source: string;
  account: string | null;
  status: "running" | "succeeded" | "failed";
  started_at: string;
  finished_at: string | null;
  progress: {
    items?: number; items_total?: number;
    // Rebuild: the stage running, what it does, which step of how many, and counts within a long stage.
    stage?: string | null; label?: string | null; step?: number; steps?: number;
    count?: number | null; total?: number | null; note?: string | null;
    file?: string; files_done?: number; files_total?: number;
    conversations?: number; account?: string;
  };
  result: Record<string, unknown> | null;
  error: string | null;
};

export type SourceInfo = {
  id: string;
  kind: "google" | "local" | "remote" | "import";
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
  job: Job | null;
  // Whether a hosted model may read this source, and how to change that.
  model: { id: string; allowed: "yes" | "partly" | "no"; note: string };
};

export type ModelStatus = { enabled: boolean; configured: boolean; sources: string[]; local_sources: boolean };

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
  // A Takeout export can be several GB: prefer a path or scanDownloads (read in place) to an upload.
  importTakeout: (input: ImportInput, onProgress?: (fraction: number) => void) =>
    startImport("/import/takeout", input, onProgress),
  importLinkedin: (input: ImportInput, onProgress?: (fraction: number) => void) =>
    startImport("/import/linkedin", input, onProgress),
  importAiExport: (input: ImportInput, onProgress?: (fraction: number) => void) =>
    startImport("/import/ai-export", input, onProgress),
  job: (id: string, signal?: AbortSignal) => get<Job>(`/jobs/${id}`, {}, signal),
  sources: (signal?: AbortSignal) => get<{ sources: SourceInfo[]; model: ModelStatus }>("/sources", {}, signal),
  jobs: (signal?: AbortSignal) => get<{ jobs: Job[] }>("/jobs", {}, signal),
  syncSource: (id: string, account?: string) => send<Job>("POST", `/sources/${id}/sync`, undefined, account ? { account } : {}),
  runPipeline: () => send<Job>("POST", "/pipeline/run"),
  saveGithubToken: (token: string) => send<{ stored: boolean; account: string }>("POST", "/sources/github/token", { token }),
  disconnect: (provider: "google" | "github", account: string) =>
    send<{ removed: string }>("DELETE", `/sources/${provider}/accounts/${encodeURIComponent(account)}`),
  googleConnectUrl: () => new URL("/sources/google/connect", API).toString(),
  stats: () => get<{ source_items: Record<string, number>; nodes: Record<string, number>; edges: Record<string, number> }>("/stats", {}),
  search: (q: string, opts: { source?: string; person?: string; from?: string; to?: string; mode?: string } = {}) =>
    get<{ hits: Hit[] }>("/search", { q, ...opts }),
  experts: (q: string, opts: { mode?: string } = {}) => get<{ experts: Expert[] }>("/experts", { q, ...opts }),
  findThings: (q: string, seed?: number, signal?: AbortSignal) =>
    get<{ results: Thing[] }>("/graph/find", { q, seed }, signal),
  nodeSummary: (id: number, signal?: AbortSignal) => get<NodeSummary>(`/graph/node/${id}/summary`, {}, signal),
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
  askSuggestions: () => get<string[]>("/ask/suggestions", {}),
  graphVersion: (signal?: AbortSignal) => get<{ version: string }>("/graph/version", {}, signal),
  view: (seed?: number, signal?: AbortSignal) => get<Subgraph & { seed: number }>("/graph/view", { seed }, signal),
  expandNode: (seed: number, node: number, visible: number[], offset = 0, signal?: AbortSignal) =>
    get<Subgraph>("/graph/expand", { seed, node, visible, offset }, signal),
  explain: (a: number, b: number) => get<Explanation>("/graph/explain", { a, b }),
  edgeIsWrong: (edge: number) => send<{ removed: number; also_removed: number; edges: number[] }>("POST", "/graph/edge/wrong", { edge }),
  restoreEdges: (edges: number[]) => send<{ restored: number[] }>("POST", "/graph/edge/restore", { edges }),
  linkOptions: (a: number, b: number) =>
    get<{ options: { label: string; type: string; src: number; dst: number }[] }>("/graph/link/options", { a, b }),
  addLink: (src: number, dst: number, type: string, note?: string) =>
    send<{ id: number; src: number; dst: number; type: string }>("POST", "/graph/link", { src, dst, type, note }),
  entity: (id: number) => get<EntityDetail>(`/graph/entity/${id}`, {}),
  renameEntity: (id: number, name: string) => send<Correction>("POST", `/graph/entity/${id}/rename`, { name }),
  hideEntity: (id: number) => send<Correction>("POST", `/graph/entity/${id}/hide`),
  mergeEntity: (id: number, other: number) => send<Correction>("POST", `/graph/entity/${id}/merge`, { other }),
  mergePeople: (id: number, other: number) => send<{ merged: number[]; person: number }>("POST", `/graph/person/${id}/merge`, { other }),
  unlinkFromEntity: (id: number, node: number) => send<Correction>("POST", `/graph/entity/${id}/unlink`, { node }),
  undoCorrection: (overrideId: number) => send<{ undone: number }>("DELETE", `/overrides/${overrideId}`),
  node: (id: number) => get<NodeDetail>(`/graph/node/${id}`, {}),
  item: (id: number, signal?: AbortSignal) => get<ItemDetail>(`/items/${id}`, {}, signal),
  openItem: (id: number) => send<{ opened: string }>("POST", `/items/${id}/open`),
  nodeSources: (id: number, offset = 0, limit = 8, signal?: AbortSignal) =>
    get<{ items: SourceCard[]; more: boolean }>(`/graph/node/${id}/sources`, { offset, limit }, signal),
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
  Project: "#4f46e5",
  Place: "#ca8a04",
};
