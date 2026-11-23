// Every call to the API, with the shapes it answers. A type here says exactly what the API sends: change both together.

export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// ---- The graph the explorer draws (GET /graph/view, /graph/expand) ----

/** A node on the graph. `kind`: the centre ("seed") or anything around it; `strength` (0 to 1): how strongly it ties
 *  to the centre, which sets its size; `category`: its section (People, Groups, or a project's kind). */
export type GNode = {
  id: number; type: string; name: string; key: string; category: string | null;
  kind: "seed" | "entity"; strength: number; expandable: boolean;
};
/** Where a link came from: read from your data, added by you or by an agent ("agent:<name>"), or inferred. */
export type Origin = "stated" | "user" | "derived" | `agent:${string}`;
/** A link on the graph. Links the explorer draws (a spoke from the centre, a tie between two people) have negative ids
 *  of their own. `stated`: a fact (read from your data or added by you), not an inference. */
export type GEdge = { id: number; src: number; dst: number; type: string; weight: number; origin: Origin; stated: boolean };
export type Subgraph = { nodes: GNode[]; edges: GEdge[] };
/** The opening view. With no one to open on it is `empty`, and says why: nothing is built yet, or no source says which
 *  person is you (or nothing links you to anyone yet). */
export type View = Subgraph & ({ seed: number; empty: false } | { seed: null; empty: true; reason: "nothing_built" | "no_owner" });

/** The node types Jump to and the pickers can look for. */
export type ThingType = "Person" | "Project" | "Thread" | "Event" | "Organization" | "Place" | "Folder" | "File";
/** A thing Jump to or a picker found. `context`: one line about it ("12 messages with you", "You"). */
export type Thing = { id: number; type: ThingType; name: string; category: string | null; context: string; score: number };

export type NodeSummary = {
  summary: string;
  highlights: { id: number; name: string; type: string; category: string | null; why: string }[];
  sources: number[];
  generated_by: "model" | "fallback";
};

// ---- Records ----

/** A source item as lists show it: enough to display, Open and View. `kind`: how it backs a link (evidence only). */
export type SourceCard = {
  id: number; source: string; title: string | null; date: string | null; snippet: string;
  open_url: string | null; open_label: string | null; local_file: boolean; kind?: string;
};
export type ItemDetail = {
  id: number; source: string; title: string | null; occurred_at: string | null; text: string;
  meta: Record<string, unknown>; open_url: string | null; open_label: string | null; local_path: string | null;
};
/** Another address, number or profile merged into a person, named by its key (never a node id). */
export type Identity = { key: string; name: string };
/** A node as the inspector shows it, with its own record (as the reader shows it) when it has one, and for a person
 *  the other identities merged into them. */
export type NodeDetail = {
  id: number; type: string; key: string; name: string; category: string | null;
  props: Record<string, unknown>;
  source_item: ItemDetail | null;
  identities: Identity[];
};

/** One edge with where it came from (`how`, in plain words), the verifier's reason (when it has run) and the items it
 *  was derived from. Only an inferred one (`stated` false) can be called wrong; one you added can be removed. `from`
 *  and `to`: which way a hop of a path goes. */
export type WhyEdge = {
  id: number; type: string; extractor: string; origin: Origin; stated: boolean; how: string;
  src: number; dst: number; src_name: string | null; dst_name: string | null;
  verdict: "keep" | "drop" | "unsure" | null; reason: string | null; why: string | null; confidence: number | null;
  evidence: SourceCard[];
  from?: number; to?: number;
};

export type Explanation = {
  a: { id: number; name: string };
  b: { id: number; name: string };
  messages: number;
  // thread_id is null for a one-on-one conversation: it is the relationship itself, not a node to open.
  conversations: { thread_id: number | null; thread: string; messages: number; people: number; first: string | null;
    last: string | null; source: string; excerpt: string | null }[];
  events: { id: number; name: string; at: string | null }[];
  shared: SourceCard[];
  direct: WhyEdge[];
  path: WhyEdge[] | null;
  /** Your rejections of the pair; `override_id` undoes one (null for one made before undo existed). */
  corrections: { override_id: number | null; src: number; dst: number; src_name: string; dst_name: string;
    reason: string | null; updated_at: string }[];
};

/** A correction that shapes a project, oldest first: your rename (`value`: the name you gave), your hide, or a project
 *  you merged into it (`value`: its name then). */
export type EntityCorrection = { override_id: number; op: "rename" | "hide" | "merge"; value: string | null; created_at: string };
export type EntityDetail = {
  id: number; name: string; key: string; aliases: string[]; anchors: string[]; hidden: boolean;
  people: { id: number; name: string; items: number }[];
  groups: { id: number; name: string; why: string | null; days: number | null }[];
  events: { id: number; name: string; date: string | null; why: string | null }[];
  items: { id: number; type: string; name: string; why: string | null; quote: string | null; source: string | null;
    date: string | null; confidence: number | null }[];
  counts: { people: number; groups: number; events: number; items: number };
  corrections: EntityCorrection[];
};

/** A link the verifier was unsure about, for you to answer. Each end as the explorer shows it. `claim`: what the link
 *  says, in the verifier's words. */
export type Suggestion = {
  edge: number; type: string; extractor: string;
  src: number; src_type: string; src_name: string; dst: number; dst_type: string; dst_name: string;
  reason: string | null; confidence: number | null; claim: string;
};
export type LinkOption = { label: string; type: string; src: number; dst: number };

// ---- Corrections ----

/** When a correction applies: now; when the rebuild that is running publishes (GET /graph/version says when); or
 *  with the next rebuild, when it couldn't apply now and none is running. `note` says which in words. */
export type Applied = { applied: "now" } | { applied: "on_publish" | "next_rebuild"; note: string };
/** A node pair a correction touched (identity heads, as the graph shows them), and whether a link the graph draws
 *  joins it once the correction applies: null until then. */
export type Pair = { a: number; b: number; still_linked: boolean | null };
/** A correction's answer: when it applies and the pairs it touched (none for a rename, a hide or a merge). */
export type Settled = Applied & { pairs?: Pair[] };

type PairCorrection = Applied & { override_id: number; pairs: Pair[] };
export type Rejected = PairCorrection & { removed: number; also_removed: number; edges: number[] };
export type EntityCorrected = Applied & { override_id: number; id: number };
/** A connection you added: `id` is its link once it applies (null until then); `override_id` undoes it (null for a
 *  Project put under a Project, which removeLink takes out). */
export type LinkAdded = Applied & {
  id: number | null; src: number; dst: number; type: string; override_id: number | null; pairs: Pair[];
};
export type Undone = Applied & { undone: number; op: string; pairs: Pair[] };

// ---- Search and Ask ----

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
  // Ask's evidence only: how a graph neighbour was reached from a hit, and the cross-encoder's score.
  relation?: "reply" | "thread" | "correspondents";
  via_chunk_id?: number;
  rerank_score?: number;
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

export type Expert = { id: number; name: string; score: number; evidence_count: number; evidence: ExpertEvidence[] };

/** What narrowed a search or an answer: the request's own filters and what the question stated (`from_question`). */
export type AppliedFilters = {
  sources: string[]; // stored source values; empty = every source (for Ask: every opted-in source)
  person: { id: number; name: string | null } | null; // sent or received by any of this person's identities
  mentions: { id: number; name: string | null } | null; // records that name this person
  since: string | null;
  until: string | null; // exclusive
  from_question: ("sources" | "person" | "mentions" | "since" | "until")[];
};

export type SearchMode = "hybrid" | "lexical" | "dense";
export type SearchOptions = { sources?: string[]; person?: string; from?: string; to?: string; mode?: SearchMode };

type AskCommon = { filters: AppliedFilters; timing: { retrieve_s: number; total_s?: number } };
/** An answer, or why there is none. With a `reason` nothing was sent to the hosted model, and `answer` says how to
 *  change that: `no_opted_in_sources` (nothing the question may read is opted in) or `model_not_set_up` (hosted calls
 *  are off, or no key is saved). */
export type AskResult = AskCommon & (
  | { reason: "no_opted_in_sources" | "model_not_set_up"; answerable: false; answer: string; citations: Hit[]; evidence: Hit[] }
  | { reason?: undefined; answerable: boolean; answer: string; citations: Hit[]; evidence: Hit[] }
);

// ---- Sources and jobs ----

/** What every import and sync stores, counted. */
export type StoreStats = { inserted: number; updated: number; unchanged: number; deleted: number; redactions: number };
export type AiProvider = "chatgpt" | "claude_ai" | "gemini";
export type AiImportResult = StoreStats & { provider: AiProvider; conversations: number };
export type TakeoutResult = StoreStats & {
  places: number; parts: Record<string, number>; counts: Record<string, number>; notes: string[];
};
export type LinkedInResult = StoreStats & {
  profile: number; connections: number; positions: number; education: number; files: string[];
};
/** A sync: the stats summed over its accounts, and each account's own. */
export type SyncResult = StoreStats & { accounts: Record<string, StoreStats & { seconds: number }> };
/** A rebuild: each stage's own result and how long it took. */
export type PipelineResult = Record<string, { result: unknown; seconds: number }>;

type JobCommon = {
  id: string;
  account: string | null;
  status: "running" | "succeeded" | "failed";
  started_at: string;
  finished_at: string | null;
  error: string | null;
};
// Progress is {} until a job first reports, and once it ends with items stored, {items}: how many.
export type SyncJob = JobCommon & {
  kind: "sync"; source: string; progress: { items?: number; account?: string }; result: SyncResult | null;
};
export type PipelineJob = JobCommon & {
  kind: "pipeline"; source: "pipeline";
  // The stage running, what it does, which step of how many, and counts within a long stage.
  progress: { stage?: string | null; label?: string | null; step?: number; steps?: number; count?: number | null;
    total?: number | null; note?: string | null; results?: PipelineResult };
  result: PipelineResult | null;
};
/** An import: its source is the format it reads (an AI chat export's is the assistant it comes from). */
export type ImportJobOf<Source extends string, Progress, Result> = JobCommon & {
  kind: "import"; source: Source; progress: Progress & { items?: number }; result: Result | null;
};
export type TakeoutJob = ImportJobOf<"takeout",
  { file?: string; files_done?: number; files_total?: number; items_total?: number }, TakeoutResult>;
export type LinkedInJob = ImportJobOf<"linkedin", object, LinkedInResult>;
export type AiExportJob = ImportJobOf<AiProvider, { conversations?: number }, AiImportResult>;
export type ImportJob = TakeoutJob | LinkedInJob | AiExportJob;
export type Job = SyncJob | PipelineJob | ImportJob;

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
/** GET /sources: every source, the hosted model's state, and what a record from each stored source value is called. */
export type SourcesAnswer = { sources: SourceInfo[]; model: ModelStatus; labels: Record<string, string> };

/** What an import route takes: files to upload, or a path the API reads in place (it runs on your computer). */
export type ImportInput = { files?: File[]; path?: string; scanDownloads?: boolean };

// ---- Requests ----

/** A failed call to the API: the HTTP status (0 when the API couldn't be reached) and the API's own explanation. */
export class ApiError extends Error {
  constructor(readonly status: number, readonly detail: string) {
    super(detail);
    this.name = "ApiError";
  }
}

/** The API's explanation of a refused request: its `detail`, or FastAPI's list of {loc, msg} for a rejected one. */
function explain(body: unknown, fallback: string): string {
  const detail = typeof body === "object" && body !== null && "detail" in body ? body.detail : undefined;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`).join("; ");
  }
  return fallback;
}

async function requestJson<T>(url: URL, init?: RequestInit): Promise<T> {
  let r: Response;
  try {
    r = await fetch(url, init);
  } catch (e) {
    if (e instanceof Error && e.name === "AbortError") throw e;
    throw new ApiError(0, "Could not reach the API. Check that it is running.");
  }
  if (!r.ok) throw new ApiError(r.status, explain(await r.json().catch(() => null), `${r.status} ${url.pathname}`));
  return (await r.json()) as T;
}

function get<T>(path: string, params: Record<string, string | number | (string | number)[] | undefined>, signal?: AbortSignal) {
  const url = new URL(path, API);
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === "") continue;
    for (const x of Array.isArray(v) ? v : [v]) url.searchParams.append(k, String(x));
  }
  return requestJson<T>(url, { signal });
}

function send<T>(method: "POST" | "DELETE", path: string, body?: unknown, params: Record<string, string> = {}) {
  const url = new URL(path, API);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  return requestJson<T>(url, {
    method,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function searchUrl(path: string, q: string, { sources, person, from, to, mode }: SearchOptions) {
  const url = new URL(path, API);
  url.searchParams.set("q", q);
  for (const s of sources ?? []) url.searchParams.append("sources", s);
  for (const [k, v] of Object.entries({ person: person?.trim(), from, to, mode })) if (v) url.searchParams.set(k, v);
  return url;
}

/** Start an import job. XHR rather than fetch: fetch cannot report upload progress. */
function startImport<J extends ImportJob>(route: string, input: ImportInput, onProgress?: (fraction: number) => void) {
  const form = new FormData();
  for (const f of input.files ?? []) form.append("file", f);
  if (input.path?.trim()) form.append("path", input.path.trim());
  if (input.scanDownloads) form.append("scan_downloads", "true");
  return new Promise<J>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", new URL(route, API).toString());
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onerror = () => reject(new ApiError(0, "Could not reach the API. Check that it is running."));
    xhr.onload = () => {
      let body: unknown = null;
      try { body = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as J);
      else reject(new ApiError(xhr.status, explain(body, `Import failed (${xhr.status}).`)));
    };
    xhr.send(form);
  });
}

export const api = {
  // ---- Sources, imports and jobs
  sources: (signal?: AbortSignal) => get<SourcesAnswer>("/sources", {}, signal),
  jobs: (signal?: AbortSignal) => get<{ jobs: Job[] }>("/jobs", {}, signal),
  /** A job you started, as it is now: the same kind of job, with its progress and, once it ends, its result. */
  job: <J extends Pick<Job, "id">>(job: J, signal?: AbortSignal) => get<J>(`/jobs/${job.id}`, {}, signal),
  syncSource: (id: string, account?: string) => send<SyncJob>("POST", `/sources/${id}/sync`, undefined, account ? { account } : {}),
  runPipeline: () => send<PipelineJob>("POST", "/pipeline/run"),
  // A Takeout export can be several GB: prefer a path or scanDownloads (read in place) to an upload.
  importTakeout: (input: ImportInput, onProgress?: (fraction: number) => void) =>
    startImport<TakeoutJob>("/import/takeout", input, onProgress),
  importLinkedin: (input: ImportInput, onProgress?: (fraction: number) => void) =>
    startImport<LinkedInJob>("/import/linkedin", input, onProgress),
  importAiExport: (input: ImportInput, onProgress?: (fraction: number) => void) =>
    startImport<AiExportJob>("/import/ai-export", input, onProgress),
  saveGithubToken: (token: string) => send<{ stored: boolean; account: string }>("POST", "/sources/github/token", { token }),
  disconnect: (provider: "google" | "github", account: string) =>
    send<{ removed: string }>("DELETE", `/sources/${provider}/accounts/${encodeURIComponent(account)}`),
  googleConnectUrl: () => new URL("/sources/google/connect", API).toString(),

  // ---- Search and Ask
  search: (q: string, opts: SearchOptions = {}, signal?: AbortSignal) =>
    requestJson<{ hits: Hit[]; filters: AppliedFilters }>(searchUrl("/search", q, opts), { signal }),
  experts: (q: string, opts: SearchOptions = {}, signal?: AbortSignal) =>
    requestJson<{ experts: Expert[]; filters: AppliedFilters }>(searchUrl("/experts", q, opts), { signal }),
  ask: (question: string, useGraph: boolean, signal?: AbortSignal) =>
    requestJson<AskResult>(new URL("/ask", API), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question, use_graph: useGraph }),
      signal,
    }),
  askSuggestions: (signal?: AbortSignal) => get<string[]>("/ask/suggestions", {}, signal),

  // ---- The graph
  /** The graph's change token, and whether a rebuild is running (a correction made now applies when it publishes). */
  graphVersion: (signal?: AbortSignal) => get<{ version: string; rebuilding: boolean }>("/graph/version", {}, signal),
  view: (seed?: number, signal?: AbortSignal) => get<View>("/graph/view", { seed }, signal),
  expandNode: (seed: number, node: number, visible: number[], signal?: AbortSignal) =>
    get<Subgraph>("/graph/expand", { seed, node, visible }, signal),
  /** Things named like `q`, ranked by closeness to `seed` (default: you); `types` keeps only those. */
  findThings: (q: string, { seed, types }: { seed?: number; types?: ThingType[] } = {}, signal?: AbortSignal) =>
    get<{ results: Thing[] }>("/graph/find", { q, seed, types }, signal),
  explain: (a: number, b: number, signal?: AbortSignal) => get<Explanation>("/graph/explain", { a, b }, signal),
  node: (id: number, signal?: AbortSignal) => get<NodeDetail>(`/graph/node/${id}`, {}, signal),
  nodeSummary: (id: number, signal?: AbortSignal) => get<NodeSummary>(`/graph/node/${id}/summary`, {}, signal),
  nodeSources: (id: number, offset = 0, limit = 8, signal?: AbortSignal) =>
    get<{ items: SourceCard[]; more: boolean }>(`/graph/node/${id}/sources`, { offset, limit }, signal),
  entity: (id: number, signal?: AbortSignal) => get<EntityDetail>(`/graph/entity/${id}`, {}, signal),
  item: (id: number, signal?: AbortSignal) => get<ItemDetail>(`/items/${id}`, {}, signal),
  openItem: (id: number) => send<{ opened: string }>("POST", `/items/${id}/open`),
  /** The links to check, most confident first: the first 50 of `total`. */
  suggestions: (signal?: AbortSignal) => get<{ suggestions: Suggestion[]; total: number }>("/graph/suggestions", {}, signal),
  linkOptions: (a: number, b: number, signal?: AbortSignal) =>
    get<{ options: LinkOption[] }>("/graph/link/options", { a, b }, signal),

  // ---- Corrections: each answers when it applies, and how to undo it
  edgeIsWrong: (edge: number) => send<Rejected>("POST", "/graph/edge/wrong", { edge }),
  edgeIsRight: (edge: number) => send<Applied & { confirmed: number }>("POST", "/graph/edge/confirm", { edge }),
  addLink: (src: number, dst: number, type: string, note?: string) =>
    send<LinkAdded>("POST", "/graph/link", { src, dst, type, note }),
  removeLink: (edge: number) => send<PairCorrection & { removed: number }>("DELETE", `/graph/link/${edge}`),
  mergePeople: (id: number, other: number) =>
    send<Applied & { merged: [number, number]; person: number; override_id: number }>("POST", `/graph/person/${id}/merge`, { other }),
  splitPerson: (id: number, key: string) => send<PairCorrection>("POST", `/graph/person/${id}/split`, { key }),
  renameEntity: (id: number, name: string) =>
    send<EntityCorrected & { op: "rename"; was: string }>("POST", `/graph/entity/${id}/rename`, { name }),
  hideEntity: (id: number) => send<EntityCorrected & { op: "hide" }>("POST", `/graph/entity/${id}/hide`),
  mergeEntity: (id: number, other: number) => send<EntityCorrected & { op: "merge" }>("POST", `/graph/entity/${id}/merge`, { other }),
  unlinkFromEntity: (id: number, node: number) =>
    send<Rejected & { op: "unlink"; id: number }>("POST", `/graph/entity/${id}/unlink`, { node }),
  undoCorrection: (overrideId: number) => send<Undone>("DELETE", `/overrides/${overrideId}`),
};
