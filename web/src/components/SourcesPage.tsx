"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useId, useLayoutEffect, useState } from "react";
import ImportForm from "@/components/ImportForms";
import { InlineError, Skeleton } from "@/components/ui";
import { API, api, type Job, type ModelStatus, type PipelineJob, type SourceInfo } from "@/lib/api";
import { formatDayTime } from "@/lib/dates";
import { useRequest } from "@/lib/hooks";
import { shareSources, useSourceLabel } from "@/lib/sources";

const STATUS: Record<SourceInfo["status"], { label: string; dot: string; text: string }> = {
  not_set_up: { label: "Not set up", dot: "bg-muted", text: "text-muted" },
  ready: { label: "Ready", dot: "bg-accent", text: "text-accent" },
  syncing: { label: "Working", dot: "bg-accent animate-pulse", text: "text-accent" },
  needs_attention: { label: "Needs attention", dot: "bg-danger", text: "text-danger" },
};

const nf = new Intl.NumberFormat();
const when = (date: string | null) => formatDayTime(date) || "Never";

function Steps({ steps }: { steps: string[] }) {
  return (
    <ol className="list-decimal space-y-1.5 pl-5 text-sm leading-6 text-soft marker:text-muted">
      {steps.map((s) => <li key={s}>{s}</li>)}
    </ol>
  );
}

function Cli({ command }: { command: string | null }) {
  return command ? <p className="mt-3 font-mono text-xs text-muted">Command line: {command}</p> : null;
}

const button = "rounded-md px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50";
const primary = `${button} bg-accent text-bg hover:opacity-90`;
const secondary = `${button} border border-line bg-panel text-soft hover:bg-hover hover:text-ink`;

function GithubToken({ onSaved }: { onSaved: () => void }) {
  const id = useId();
  const [token, setToken] = useState("");
  const [state, setState] = useState<{ busy: boolean; error?: string; saved?: string }>({ busy: false });
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setState({ busy: true });
    try {
      const { account } = await api.saveGithubToken(token);
      setToken("");
      setState({ busy: false, saved: account });
      onSaved();
    } catch (cause) {
      setState({ busy: false, error: cause instanceof Error ? cause.message : String(cause) });
    }
  }
  return (
    <form onSubmit={save} className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center">
      <label htmlFor={id} className="sr-only">GitHub token</label>
      <input id={id} type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)}
        placeholder="github_pat_..." className="min-w-0 flex-1 rounded-md border border-line bg-bg px-3 py-1.5 font-mono text-sm outline-none placeholder:text-muted focus-visible:border-accent" />
      <button type="submit" disabled={state.busy || token.trim().length < 20} className={secondary}>
        {state.busy ? "Saving..." : "Save token"}
      </button>
      {state.saved && <span className="text-xs text-accent" role="status">Saved for {state.saved}, encrypted.</span>}
      {state.error && <InlineError message={state.error} />}
    </form>
  );
}

/** Forget a connected account's token, after a second press. What it synced stays. */
function Disconnect({ provider, account, onDone }: { provider: "google" | "github"; account: string; onDone: () => void }) {
  const [state, setState] = useState<"idle" | "confirm" | "busy">("idle");
  const [error, setError] = useState<string | null>(null);
  async function confirm() {
    setState("busy");
    try {
      await api.disconnect(provider, account);
      onDone();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      setState("idle");
    }
  }
  const link = "text-xs text-muted underline underline-offset-4 hover:text-ink disabled:opacity-50";
  return (
    <span className="ml-2 inline-flex items-center gap-2 font-sans">
      {state === "idle" ? (
        <button type="button" className={link} onClick={() => setState("confirm")}>Disconnect</button>
      ) : (
        <>
          <span className="text-xs text-soft">Stop syncing {account}? What it synced stays.</span>
          <button type="button" className={`${link} text-danger`} disabled={state === "busy"} onClick={() => void confirm()}>
            Disconnect
          </button>
          <button type="button" className={link} disabled={state === "busy"} onClick={() => setState("idle")}>Keep</button>
        </>
      )}
      {error && <InlineError message={error} />}
    </span>
  );
}

function SourceCard({ source, busy, onSync, onRefresh }: {
  source: SourceInfo; busy: boolean; onSync: (id: string) => void; onRefresh: () => void;
}) {
  const st = STATUS[source.status];
  const setUp = source.status === "ready" || source.status === "syncing" || (source.status === "needs_attention" && source.items > 0);
  const [open, setOpen] = useState(false);
  // Google's steps are the same for all four services: show them once, on the first card.
  const sharedGoogle = source.kind === "google" && source.id !== "gmail";
  const showSteps = (!setUp || open) && !sharedGoogle;
  const job = source.job;
  const running = job?.status === "running";
  const sofar = job && job.kind !== "pipeline" ? job.progress.items ?? 0 : 0;
  const isGoogle = source.kind === "google";
  const isGithub = source.id === "github";
  const needsGoogle = isGoogle && source.accounts.length === 0;
  const [newToken, setNewToken] = useState(false);

  let action: React.ReactNode = null;
  if (isGithub && source.accounts.length === 0) {
    action = <GithubToken onSaved={onRefresh} />;
  } else if (needsGoogle) {
    // Without the OAuth client file Google can't be asked; the message says how to add it.
    action = source.requirements_ok
      ? <a href={api.googleConnectUrl()} className={primary}>Connect a Google account</a>
      : <button type="button" onClick={onRefresh} className={secondary}>Check again</button>;
  } else if (source.can_sync) {
    action = (
      <div className="flex gap-2">
        {isGoogle && <a href={api.googleConnectUrl()} className={secondary}>Add another account</a>}
        {isGithub && (
          <button type="button" onClick={() => setNewToken(!newToken)} aria-expanded={newToken} className={secondary}>
            Replace or add a token
          </button>
        )}
        <button type="button" onClick={() => onSync(source.id)} disabled={running || busy} className={primary}>
          {running ? "Syncing..." : "Sync now"}
        </button>
      </div>
    );
  } else if (source.kind === "local" && !source.requirements_ok) {
    action = <button type="button" onClick={onRefresh} className={secondary}>Check again</button>;
  }

  return (
    <li className="border-b border-line px-5 py-4 last:border-b-0">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <h3 className="font-medium text-ink">{source.name}</h3>
            <span className={`inline-flex items-center gap-1.5 text-xs ${st.text}`}>
              <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${st.dot}`} />
              {st.label}
            </span>
          </div>
          <p className="mt-1 max-w-xl text-sm leading-6 text-muted">{source.reads}</p>
          {source.kind !== "import" && (
            <p className="mt-1 text-xs text-muted tabular-nums">
              {nf.format(source.items)} items, last synced {when(source.last_sync)}
            </p>
          )}
          {source.kind === "import" && (
            <p className="mt-1 text-xs text-muted tabular-nums">
              {source.items > 0
                ? `${nf.format(source.items)} items imported${source.last_sync ? `, last import ${when(source.last_sync)}` : ""}`
                : "Nothing imported yet"}
            </p>
          )}
          <p className={`mt-1 max-w-xl text-xs leading-5 ${source.model.allowed === "no" ? "text-muted" : "text-accent"}`}>
            {source.model.note}
          </p>
        </div>
        <div className="shrink-0">{action}</div>
      </div>

      {running && (
        <div className="mt-3" role="status">
          <div className="h-1 overflow-hidden rounded-full bg-hover"><div className="progress-slide h-full w-1/3 bg-accent" /></div>
          <p className="mt-1.5 text-xs text-muted tabular-nums">
            Reading... {nf.format(sofar)} items so far. You can leave this page open or come back.
          </p>
        </div>
      )}

      {sharedGoogle && needsGoogle && <p className="mt-3 text-sm text-soft">Uses the Google account you connect under Gmail.</p>}

      {source.message && source.status !== "syncing" && !(sharedGoogle && needsGoogle) && (
        <p role={source.status === "needs_attention" ? "alert" : undefined}
          className={`mt-3 text-sm leading-6 ${source.status === "needs_attention" ? "text-danger" : "text-soft"}`}>
          {source.message}
          {source.status === "needs_attention" && job?.status === "failed" && isGoogle && " If Google says access expired, connect the account again."}
        </p>
      )}

      {isGithub && newToken && source.accounts.length > 0 && (
        <GithubToken onSaved={() => { setNewToken(false); onRefresh(); }} />
      )}

      {source.accounts.length > 0 && (
        <ul className="mt-3 space-y-1" aria-label={`${source.name} accounts`}>
          {source.accounts.map((a) => (
            <li key={a.account} className="font-mono text-xs text-soft">
              {a.account}{a.last_sync && <span className="text-muted"> · synced {when(a.last_sync)}</span>}
              {/* The four Google services share one sign-in: disconnect it once, under Gmail. */}
              {(isGithub || source.id === "gmail") && (
                <Disconnect provider={isGithub ? "github" : "google"} account={a.account} onDone={onRefresh} />
              )}
            </li>
          ))}
        </ul>
      )}

      {setUp && !sharedGoogle && (
        <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
          className="mt-3 text-xs text-muted underline underline-offset-4 hover:text-ink">
          {open ? "Hide setup steps" : "Show setup steps"}
        </button>
      )}
      {showSteps && (
        <div className="mt-3 rounded-md border border-line bg-bg px-4 py-3">
          <Steps steps={source.steps} />
          <Cli command={source.cli} />
        </div>
      )}

      {source.kind === "import" && (
        <div className="mt-4"><ImportForm sourceId={source.id} onDone={onRefresh} /></div>
      )}
    </li>
  );
}

function ModelPanel({ model }: { model: ModelStatus }) {
  const sourceLabel = useSourceLabel();
  let state: string;
  if (!model.enabled) {
    state = "Hosted model calls are off (IGNEOUS_LLM_ENABLED=false). Search and the graph work; Ask is unavailable.";
  } else if (!model.configured) {
    state = "No Bedrock key is set, so no model is called and Ask is unavailable: save one with `igneous connect "
      + "bedrock`, or set AWS_BEARER_TOKEN_BEDROCK in the API's environment. Search and the graph work without it.";
  } else {
    state = "Ask, AI extraction, summaries, naming your life threads and projects, reading your resume, and the "
      + "checks on each connection send text to a model on Amazon Bedrock. They read only the sources opted in here; "
      + "everything else stays on this computer and reaches the model only as a count. Search may also send your "
      + "question, never your records, to pick out names and dates.";
  }
  return (
    <section id="model" tabIndex={-1} className="mt-8 scroll-mt-4 rounded-md border border-line bg-panel px-5 py-4 outline-none target:border-accent"
      aria-labelledby="model-heading">
      <h2 id="model-heading" className="text-sm font-medium text-ink">Hosted model</h2>
      <p className="mt-1 max-w-xl text-sm leading-6 text-muted">{state}</p>
      <p className="mt-2 text-sm text-soft">
        Opted in: {model.sources.length ? [...new Set(model.sources.map(sourceLabel))].join(", ") : "nothing yet, so Ask stays off."}
      </p>
      <p className="mt-2 max-w-xl text-xs leading-5 text-muted">
        To opt a source in, add the id its entry shows to IGNEOUS_LLM_SOURCES in api/.env (comma-separated) and
        restart the API. Local files and Apple Notes also need IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED=true. The MCP
        server (igneous mcp) is separate: what its tools return goes to the model of the AI client you connect it to.
      </p>
    </section>
  );
}

// The API names each stage as it starts (label), how many steps the run has, and counts within long stages.
function buildProgress(p: PipelineJob["progress"] | undefined) {
  const steps = p?.steps ?? 0;
  const step = Math.min(p?.step ?? 0, steps);
  const within = p?.count != null && p?.total ? Math.min(p.count / p.total, 1) : 0;
  const counted = p?.count != null ? `${nf.format(p.count)}${p.total ? ` of ${nf.format(p.total)}` : ""}` : null;
  return { step, steps, label: p?.label ?? null, counted, share: steps ? (Math.max(step - 1, 0) + within) / steps : 0 };
}

function BuildGraph({ job, itemCount, onRun, starting }: { job: PipelineJob | null; itemCount: number; onRun: () => void; starting: boolean }) {
  const running = job?.status === "running";
  const { step, steps, label, counted, share } = buildProgress(job?.progress);
  return (
    <section className="mt-8 rounded-md border border-line bg-panel" aria-labelledby="build-heading">
      <div className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 id="build-heading" className="text-sm font-medium text-ink">Build the graph</h2>
          <p className="mt-1 max-w-xl text-sm leading-6 text-muted">
            Syncing only collects your data. Press Rebuild after you connect or sync sources so the Graph, Ask and
            Search pages can use it. It reads what is new, then works out who is who, your projects, threads and
            organizations again, and shows them all at once when it finishes: until then the Graph keeps the previous
            build, and a correction you make there meanwhile applies when it finishes. It is safe to run again; the
            first run can take a few minutes.
          </p>
          <p className="mt-2 font-mono text-xs text-muted">Command line: igneous pipeline</p>
        </div>
        <button type="button" onClick={onRun} disabled={running || starting || itemCount === 0} className={primary}>
          {running ? "Building..." : "Rebuild"}
        </button>
      </div>
      {itemCount === 0 && <p className="px-5 pb-4 text-sm text-muted">Connect and sync at least one source first.</p>}
      {running && (
        <div className="px-5 pb-4" role="status">
          <div className="h-1.5 overflow-hidden rounded-full bg-hover">
            <div className="h-full bg-accent transition-[width]" style={{ width: `${share * 100}%` }} />
          </div>
          <p className="mt-1.5 text-xs text-muted">
            {steps ? `Step ${step} of ${steps}` : "Starting"}{label ? `: ${label}` : ""}{counted ? ` (${counted})` : ""}
          </p>
        </div>
      )}
      {job?.status === "succeeded" && !running && (
        <p className="px-5 pb-4 text-sm text-accent" role="status">Done. <Link href="/" className="underline underline-offset-4">Open the graph</Link>.</p>
      )}
      {job?.status === "failed" && (
        <div className="px-5 pb-4"><InlineError message={`The build stopped${label ? ` while ${label.charAt(0).toLowerCase()}${label.slice(1)}` : ""}: ${job.error ?? "unknown error"}. The Graph keeps the previous build. Fix that and press Rebuild; records it already read are kept.`} /></div>
      )}
    </section>
  );
}

const isPipeline = (j: Job): j is PipelineJob => j.kind === "pipeline";

/** The Sources page below its title: every source, the build, and the hosted model. Google sign-in comes back here with
 *  `?connected=` or `?connect_error=` in the URL, shown until you dismiss it. */
export default function SourcesPage() {
  const router = useRouter();
  const params = useSearchParams();
  const connected = params.get("connected");
  const connectError = params.get("connect_error");
  const groupsId = useId();
  // Bumped to load the page again: after every action, and every 2 s while something runs.
  const [tick, setTick] = useState(0);
  const loaded = useRequest("sources", async (signal) => {
    const [s, j] = await Promise.all([api.sources(signal), api.jobs(signal)]);
    return { ...s, pipeline: j.jobs.find(isPipeline) ?? null };
  }, tick);
  const [actionError, setActionError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const page = loaded.data;
  const reload = () => setTick((n) => n + 1);

  // Next keeps this page alive while you use the other tabs. What it shows goes stale meanwhile (a sync or a rebuild
  // runs on, the scheduled sync adds items), so hiding it marks it stale, and it loads again when it is shown.
  useLayoutEffect(() => () => setTick((n) => n + 1), []);

  // Every page names records and reads the model's state from GET /sources: hand them this fresher answer.
  useEffect(() => {
    if (page) shareSources(page);
  }, [page]);

  const anyRunning = page?.pipeline?.status === "running" || !!page?.sources.some((s) => s.job?.status === "running");
  useEffect(() => {
    if (!anyRunning) return;
    const t = setInterval(() => setTick((n) => n + 1), 2000);
    return () => clearInterval(t);
  }, [anyRunning]);

  // A link to the model section (Ask's "set up the hosted model") lands on it once the page has loaded.
  const shown = page !== undefined;
  useEffect(() => {
    if (shown && window.location.hash === "#model") document.getElementById("model")?.focus();
  }, [shown]);

  async function start(fn: () => Promise<Job>) {
    setActionError(null);
    setStarting(true);
    try {
      await fn();
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setStarting(false);
      reload();
    }
  }

  const groups = page ? [...new Set(page.sources.map((s) => s.group))] : [];
  // Every source counts, exports included: one imported export is enough to build a graph from.
  const total = page?.sources.reduce((n, s) => n + s.items, 0) ?? 0;
  const dismiss = () => router.replace("/settings", { scroll: false });

  return (
    <>
      {connected && (
        <p role="status" className="mt-4 flex items-start gap-3 rounded-md border border-line bg-panel px-4 py-2 text-sm text-accent">
          <span className="min-w-0 flex-1">Connected {connected}. You can sync it below.</span>
          <button type="button" onClick={dismiss} aria-label="Dismiss" className="btn-close">×</button>
        </p>
      )}
      {connectError && (
        <div className="mt-4 flex items-start gap-3 rounded-md border border-line bg-panel px-4 py-2">
          <div className="min-w-0 flex-1"><InlineError message={`Google sign-in did not finish: ${connectError}`} /></div>
          <button type="button" onClick={dismiss} aria-label="Dismiss" className="btn-close">×</button>
        </div>
      )}
      {actionError && <div className="mt-4 rounded-md border border-line bg-panel px-4 py-2"><InlineError message={actionError} /></div>}

      {loaded.error !== undefined && !page ? (
        <div className="mt-6 rounded-md border border-line bg-panel px-5 py-10" role="alert">
          <p className="font-medium text-danger">Sources unavailable</p>
          <p className="mt-1 text-sm leading-6 text-soft">Could not reach the Igneous API. Is it running on {API}? ({String(loaded.error)})</p>
          <button type="button" onClick={loaded.retry} className="mt-4 text-sm font-medium text-accent underline underline-offset-4">Try again</button>
        </div>
      ) : !page ? (
        <div className="mt-6 space-y-3" role="status" aria-label="Loading sources">
          <Skeleton className="h-24 w-full" /><Skeleton className="h-24 w-full" /><Skeleton className="h-24 w-full" />
        </div>
      ) : (
        <>
          {groups.map((g, i) => (
            <section key={g} className="mt-6" aria-labelledby={`${groupsId}-${i}`}>
              <h2 id={`${groupsId}-${i}`} className="mb-2 text-[11px] font-medium uppercase tracking-[0.12em] text-muted">{g}</h2>
              <ul className="rounded-md border border-line bg-panel">
                {page.sources.filter((s) => s.group === g).map((s) => (
                  <SourceCard key={s.id} source={s} busy={starting}
                    onSync={(id) => void start(() => api.syncSource(id))} onRefresh={reload} />
                ))}
              </ul>
            </section>
          ))}
          <BuildGraph job={page.pipeline} itemCount={total} starting={starting} onRun={() => void start(api.runPipeline)} />
          <ModelPanel model={page.model} />
        </>
      )}
    </>
  );
}
