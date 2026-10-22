"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import ImportForm from "@/components/ImportForms";
import { InlineError, Skeleton } from "@/components/ui";
import { api, Job, SourceInfo } from "@/lib/api";

const STATUS: Record<SourceInfo["status"], { label: string; dot: string; text: string }> = {
  not_set_up: { label: "Not set up", dot: "bg-muted", text: "text-muted" },
  ready: { label: "Ready", dot: "bg-accent", text: "text-accent" },
  syncing: { label: "Working", dot: "bg-accent animate-pulse", text: "text-accent" },
  needs_attention: { label: "Needs attention", dot: "bg-danger", text: "text-danger" },
};

const STAGE_LABEL: Record<string, string> = {
  spine: "Reading your sources into people, threads and events",
  chunk: "Splitting text into searchable pieces",
  embed: "Indexing meaning for search",
  topics: "Finding topics",
  entities: "Finding people and organizations in text",
  self: "Working out which person is you",
  resolve: "Merging duplicate people",
};

const nf = new Intl.NumberFormat();

function when(date: string | null) {
  if (!date) return "Never";
  const d = new Date(date);
  return Number.isNaN(d.getTime()) ? "Unknown" : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(d);
}

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
  const [token, setToken] = useState("");
  const [state, setState] = useState<{ busy: boolean; error?: string; done?: boolean }>({ busy: false });
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setState({ busy: true });
    try {
      await api.saveGithubToken(token);
      setToken("");
      setState({ busy: false, done: true });
      onSaved();
    } catch (cause) {
      setState({ busy: false, error: cause instanceof Error ? cause.message : String(cause) });
    }
  }
  return (
    <form onSubmit={save} className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center">
      <label htmlFor="gh-token" className="sr-only">GitHub token</label>
      <input id="gh-token" type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)}
        placeholder="github_pat_..." className="min-w-0 flex-1 rounded-md border border-line bg-bg px-3 py-1.5 font-mono text-sm outline-none placeholder:text-muted focus-visible:border-accent" />
      <button type="submit" disabled={state.busy || token.trim().length < 20} className={secondary}>
        {state.busy ? "Saving..." : "Save token"}
      </button>
      {state.done && <span className="text-xs text-accent" role="status">Saved, encrypted.</span>}
      {state.error && <InlineError message={state.error} />}
    </form>
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
  const running = source.job?.status === "running";
  const isGoogle = source.kind === "google";
  const isGithub = source.id === "github";
  const needsGoogle = isGoogle && source.accounts.length === 0;

  let action: React.ReactNode = null;
  if (isGithub && source.accounts.length === 0) {
    action = <GithubToken onSaved={onRefresh} />;
  } else if (needsGoogle) {
    action = <a href={api.googleConnectUrl()} className={primary}>Connect a Google account</a>;
  } else if (source.can_sync) {
    action = (
      <div className="flex gap-2">
        {isGoogle && <a href={api.googleConnectUrl()} className={secondary}>Add another account</a>}
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
              {source.coming_soon ? "Coming soon" : st.label}
            </span>
          </div>
          <p className="mt-1 max-w-xl text-sm leading-6 text-muted">{source.reads}</p>
          {source.kind !== "import" && !source.coming_soon && (
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
        </div>
        <div className="shrink-0">{action}</div>
      </div>

      {running && (
        <div className="mt-3" role="status">
          <div className="h-1 overflow-hidden rounded-full bg-hover"><div className="progress-slide h-full w-1/3 bg-accent" /></div>
          <p className="mt-1.5 text-xs text-muted tabular-nums">
            Reading... {nf.format(source.job?.progress.items ?? 0)} items so far. You can leave this page open or come back.
          </p>
        </div>
      )}

      {sharedGoogle && needsGoogle && <p className="mt-3 text-sm text-soft">Uses the Google account you connect under Gmail.</p>}

      {source.message && source.status !== "syncing" && !(sharedGoogle && needsGoogle) && (
        <p role={source.status === "needs_attention" ? "alert" : undefined}
          className={`mt-3 text-sm leading-6 ${source.status === "needs_attention" ? "text-danger" : "text-soft"}`}>
          {source.message}
          {source.status === "needs_attention" && source.job?.status === "failed" && isGoogle && " If Google says access expired, connect the account again."}
        </p>
      )}

      {source.accounts.length > 0 && (
        <ul className="mt-3 space-y-1" aria-label={`${source.name} accounts`}>
          {source.accounts.map((a) => (
            <li key={a.account} className="font-mono text-xs text-soft">
              {a.account}{a.last_sync && <span className="text-muted"> · synced {when(a.last_sync)}</span>}
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

function BuildGraph({ job, itemCount, onRun, starting }: { job: Job | null; itemCount: number; onRun: () => void; starting: boolean }) {
  const running = job?.status === "running";
  const p = job?.progress;
  const stage = p?.stage ? STAGE_LABEL[p.stage] ?? p.stage : null;
  return (
    <section className="mt-8 rounded-md border border-line bg-panel" aria-labelledby="build-heading">
      <div className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 id="build-heading" className="text-sm font-medium text-ink">Build the graph</h2>
          <p className="mt-1 max-w-xl text-sm leading-6 text-muted">
            Syncing only collects your data. Press Rebuild after you connect or sync sources so the Graph, Ask and
            Search pages can use it. It only processes what is new, so it is safe to run again. The first run can take
            a few minutes.
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
            <div className="h-full bg-accent transition-[width]" style={{ width: `${((p?.step ?? 1) - 1) / (p?.steps ?? 7) * 100}%` }} />
          </div>
          <p className="mt-1.5 text-xs text-muted">
            Step {p?.step ?? 1} of {p?.steps ?? 7}{stage ? `: ${stage}` : ""}
          </p>
        </div>
      )}
      {job?.status === "succeeded" && !running && (
        <p className="px-5 pb-4 text-sm text-accent" role="status">Done. <Link href="/" className="underline underline-offset-4">Open the graph</Link>.</p>
      )}
      {job?.status === "failed" && (
        <div className="px-5 pb-4"><InlineError message={`The build stopped: ${job.error ?? "unknown error"}. Fix that and press Rebuild; it resumes where it stopped.`} /></div>
      )}
    </section>
  );
}

export default function SourcesPage() {
  const [sources, setSources] = useState<SourceInfo[] | null>(null);
  const [pipeline, setPipeline] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const request = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    request.current?.abort();
    const ctl = new AbortController();
    request.current = ctl;
    try {
      const [s, j] = await Promise.all([api.sources(ctl.signal), api.jobs(ctl.signal)]);
      if (request.current !== ctl) return;
      setSources(s.sources);
      setPipeline(j.jobs.find((x) => x.source === "pipeline") ?? null);
      setError(null);
    } catch (cause) {
      if (!ctl.signal.aborted) setError(`Could not reach the Igneous API. Is it running on ${new URL(api.googleConnectUrl()).origin}? (${String(cause)})`);
    }
  }, []);

  useEffect(() => {
    void load();
    const q = new URLSearchParams(window.location.search);
    if (q.get("connected")) setNotice(`Connected ${q.get("connected")}. You can sync it below.`);
    if (q.get("connect_error")) setActionError(`Google sign-in did not finish: ${q.get("connect_error")}`);
    if (q.size) window.history.replaceState(null, "", "/settings");
    return () => request.current?.abort();
  }, [load]);

  const anyRunning = pipeline?.status === "running" || !!sources?.some((s) => s.job?.status === "running");
  useEffect(() => {
    if (!anyRunning) return;
    const t = setInterval(() => void load(), 2000);
    return () => clearInterval(t);
  }, [anyRunning, load]);

  async function start(fn: () => Promise<Job>) {
    setActionError(null);
    setStarting(true);
    try {
      await fn();
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setStarting(false);
      await load();
    }
  }

  const groups = sources ? [...new Set(sources.map((s) => s.group))] : [];
  const total = sources?.filter((s) => s.kind !== "import").reduce((n, s) => n + s.items, 0) ?? 0;

  return (
    <main className="mx-auto w-full max-w-3xl px-4 py-8 sm:px-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight text-ink">Sources</h1>
        <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">
          Connect an account, upload an export, or point Igneous at something on this computer. Everything stays on
          this computer. Work down the list, then build the graph at the bottom.
        </p>
      </header>

      {notice && <p role="status" className="mt-4 rounded-md border border-line bg-panel px-4 py-2 text-sm text-accent">{notice}</p>}
      {actionError && <div className="mt-4 rounded-md border border-line bg-panel px-4 py-2"><InlineError message={actionError} /></div>}

      {error ? (
        <div className="mt-6 rounded-md border border-line bg-panel px-5 py-10" role="alert">
          <p className="font-medium text-danger">Sources unavailable</p>
          <p className="mt-1 text-sm leading-6 text-soft">{error}</p>
          <button type="button" onClick={() => void load()} className="mt-4 text-sm font-medium text-accent underline underline-offset-4">Try again</button>
        </div>
      ) : !sources ? (
        <div className="mt-6 space-y-3" role="status" aria-label="Loading sources">
          <Skeleton className="h-24 w-full" /><Skeleton className="h-24 w-full" /><Skeleton className="h-24 w-full" />
        </div>
      ) : (
        <>
          {groups.map((g) => (
            <section key={g} className="mt-6" aria-labelledby={`g-${g}`}>
              <h2 id={`g-${g}`} className="mb-2 text-[11px] font-medium uppercase tracking-[0.12em] text-muted">{g}</h2>
              <ul className="rounded-md border border-line bg-panel">
                {sources.filter((s) => s.group === g).map((s) => (
                  <SourceCard key={s.id} source={s} busy={starting}
                    onSync={(id) => void start(() => api.syncSource(id))} onRefresh={() => void load()} />
                ))}
              </ul>
            </section>
          ))}
          <BuildGraph job={pipeline} itemCount={total} starting={starting} onRun={() => void start(api.runPipeline)} />
        </>
      )}
    </main>
  );
}
