"use client";

import { useEffect, useRef, useState } from "react";
import { AiImportResult, api, Job, LinkedInResult, TakeoutResult } from "@/lib/api";

type Provider = {
  id: "chatgpt" | "claude_ai";
  name: string;
  wait: string;
  file: string;
  steps: string[];
  note?: string;
};

const PROVIDERS: Provider[] = [
  {
    id: "chatgpt",
    name: "ChatGPT",
    wait: "Usually a few minutes to a day. OpenAI emails you a download link that expires after 24 hours.",
    file: "The .zip from the email, or conversations.json from inside it.",
    steps: [
      "Open ChatGPT and click your profile picture, then Settings.",
      "Go to Data controls and choose Export data, then Confirm export.",
      "Wait for the email from OpenAI and click Download data export.",
      "Upload the .zip here. You do not need to unzip it.",
    ],
  },
  {
    id: "claude_ai",
    name: "Claude.ai",
    wait: "Usually a few minutes. Anthropic emails you a download link that expires after 24 hours.",
    file: "The .zip from the email, or conversations.json from inside it.",
    steps: [
      "Open claude.ai and click your name, then Settings.",
      "Go to Privacy and choose Export data.",
      "Wait for the email from Anthropic and click the download link.",
      "Upload the .zip here. You do not need to unzip it.",
    ],
  },
];

type Status =
  | { kind: "idle" }
  | { kind: "uploading"; progress: number; name: string }
  | { kind: "done"; result: AiImportResult }
  | { kind: "error"; message: string };

const LABEL: Record<string, string> = { chatgpt: "ChatGPT", gemini: "Gemini", claude_ai: "Claude.ai" };

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <p className="text-[11px] font-medium uppercase tracking-[0.12em] text-muted">{label}</p>
      <p className="mt-1 text-2xl font-medium tabular-nums text-ink">{new Intl.NumberFormat().format(value)}</p>
    </div>
  );
}

function ProviderCard({ provider }: { provider: Provider }) {
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const busy = status.kind === "uploading";

  async function upload(file: File | undefined) {
    if (!file || busy) return;
    setStatus({ kind: "uploading", progress: 0, name: file.name });
    try {
      const result = await api.importAiExport(file, undefined, (progress) =>
        setStatus({ kind: "uploading", progress, name: file.name }),
      );
      setStatus({ kind: "done", result });
    } catch (cause) {
      setStatus({ kind: "error", message: cause instanceof Error ? cause.message : String(cause) });
    } finally {
      if (input.current) input.current.value = "";
    }
  }

  return (
    <section className="rounded-lg border border-line bg-panel" aria-labelledby={`${provider.id}-title`}>
      <div className="grid gap-6 p-5 sm:p-6 md:grid-cols-2">
        <div>
          <h2 id={`${provider.id}-title`} className="text-lg font-semibold tracking-tight text-ink">
            {provider.name}
          </h2>
          <ol className="mt-3 list-decimal space-y-2 pl-5 text-sm leading-6 text-soft marker:text-muted">
            {provider.steps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
          <p className="mt-4 text-sm leading-6 text-muted">
            <span className="font-medium text-soft">How long it takes:</span> {provider.wait}
          </p>
          {provider.note && <p className="mt-2 text-sm leading-6 text-muted">{provider.note}</p>}
        </div>

        <div className="flex flex-col">
          <label
            htmlFor={`${provider.id}-file`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              void upload(e.dataTransfer.files[0]);
            }}
            className={`flex min-h-36 flex-1 cursor-pointer flex-col items-center justify-center gap-1 rounded-lg border border-dashed px-4 py-6 text-center text-sm transition-colors ${
              dragging ? "border-accent bg-hover" : "border-line hover:bg-hover"
            } ${busy ? "pointer-events-none opacity-60" : ""}`}
          >
            <span className="font-medium text-ink">Drop the export here or choose a file</span>
            <span className="text-xs text-muted">{provider.file}</span>
            <input
              ref={input}
              id={`${provider.id}-file`}
              type="file"
              accept=".zip,.json,application/zip,application/json"
              className="sr-only"
              disabled={busy}
              onChange={(e) => void upload(e.target.files?.[0])}
            />
          </label>

          <div aria-live="polite" className="mt-3 min-h-6 text-sm">
            {status.kind === "uploading" && (
              <div>
                <div className="flex justify-between gap-3 text-xs text-muted">
                  <span className="truncate">
                    {status.progress >= 1 ? "Reading conversations" : "Uploading"} {status.name}
                  </span>
                  <span className="tabular-nums">{Math.round(status.progress * 100)}%</span>
                </div>
                <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-hover">
                  <div className="h-full bg-accent transition-[width]" style={{ width: `${status.progress * 100}%` }} />
                </div>
              </div>
            )}
            {status.kind === "error" && (
              <p role="alert" className="text-danger">
                {status.message}
              </p>
            )}
            {status.kind === "done" && (
              <div>
                <p className="text-accent">Imported {LABEL[status.result.provider] ?? status.result.provider} chats.</p>
                <div className="mt-3 grid grid-cols-2 gap-4">
                  <Stat label="Conversations" value={status.result.conversations} />
                  <Stat label="New days" value={status.result.items_inserted} />
                  <Stat label="Updated" value={status.result.items_updated} />
                  <Stat label="Secrets removed" value={status.result.redactions} />
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

const TAKEOUT_STEPS = [
  "Go to takeout.google.com and click Deselect all.",
  "Tick My Activity, Chrome, and Maps (your places). If you see Location History (Timeline), tick it too.",
  "On My Activity, click Multiple formats and change Activity records from HTML to JSON. HTML files are not read.",
  "Click Next step, keep One-time export and .zip, then Create export.",
  "Wait for Google's email (usually hours; it may arrive as several zips) and download every zip.",
  "Pick them below: scan Downloads, give a folder or path, or upload the zips.",
];

function TakeoutCard() {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [upload, setUpload] = useState<number | null>(null);
  const [path, setPath] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const running = starting || job?.status === "running";

  useEffect(() => {
    if (job?.status !== "running") return;
    const t = setInterval(() => {
      api.job(job.id).then(setJob).catch(() => {});
    }, 1500);
    return () => clearInterval(t);
  }, [job]);

  async function start(args: { files?: File[]; path?: string; scanDownloads?: boolean }) {
    if (running) return;
    setError(null);
    setStarting(true);
    setUpload(args.files?.length ? 0 : null);
    try {
      setJob(await api.importTakeout(args, args.files?.length ? setUpload : undefined));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setStarting(false);
      setUpload(null);
      if (input.current) input.current.value = "";
    }
  }

  const result = job?.status === "succeeded" ? (job.result as unknown as TakeoutResult | null) : null;
  const p = job?.progress;
  const secondary =
    "rounded-md border border-line bg-panel px-3 py-1.5 text-sm font-medium text-soft transition-colors hover:bg-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50";

  return (
    <section className="rounded-lg border border-line bg-panel" aria-labelledby="takeout-title">
      <div className="grid gap-6 p-5 sm:p-6 md:grid-cols-2">
        <div>
          <h2 id="takeout-title" className="text-lg font-semibold tracking-tight text-ink">
            Google Takeout
          </h2>
          <p className="mt-1 text-sm leading-6 text-muted">
            My Activity (Search, YouTube, Maps, Gemini and more), Chrome history and bookmarks, and Maps places and visits.
          </p>
          <ol className="mt-3 list-decimal space-y-2 pl-5 text-sm leading-6 text-soft marker:text-muted">
            {TAKEOUT_STEPS.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
          <p className="mt-4 text-sm leading-6 text-muted">
            <span className="font-medium text-soft">How long it takes:</span> Google usually needs a few hours to prepare
            a Takeout; the download link works for about a week.
          </p>
          <p className="mt-2 text-sm leading-6 text-muted">
            Gemini chats come in with My Activity. An export of only Gemini Apps (My Activity, JSON) works here too. This data stays on this computer and is never sent to a model unless
            you opt in.
          </p>
        </div>

        <div className="flex flex-col gap-4">
          <div>
            <button type="button" onClick={() => void start({ scanDownloads: true })} disabled={running} className={secondary}>
              Scan Downloads for takeout-*.zip
            </button>
            <p className="mt-1.5 text-xs text-muted">Best for multi-GB files: they are read in place, not uploaded.</p>
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void start({ path });
            }}
            className="flex flex-col gap-2 sm:flex-row"
          >
            <label htmlFor="takeout-path" className="sr-only">Path to a Takeout zip or folder</label>
            <input
              id="takeout-path"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="~/Downloads/takeout-2025.zip or a folder"
              className="min-w-0 flex-1 rounded-md border border-line bg-bg px-3 py-1.5 font-mono text-sm outline-none placeholder:text-muted focus-visible:border-accent"
            />
            <button type="submit" disabled={running || !path.trim()} className={secondary}>Import path</button>
          </form>
          <label
            htmlFor="takeout-file"
            className={`flex min-h-24 cursor-pointer flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-line px-4 py-5 text-center text-sm transition-colors hover:bg-hover ${running ? "pointer-events-none opacity-60" : ""}`}
          >
            <span className="font-medium text-ink">Or choose the zip files</span>
            <span className="text-xs text-muted">Select all parts of a split export at once.</span>
            <input
              ref={input}
              id="takeout-file"
              type="file"
              multiple
              accept=".zip,application/zip"
              className="sr-only"
              disabled={running}
              onChange={(e) => void start({ files: Array.from(e.target.files ?? []) })}
            />
          </label>

          <div aria-live="polite" className="min-h-6 text-sm">
            {upload !== null && <p className="text-xs text-muted">Uploading {Math.round(upload * 100)}%</p>}
            {job?.status === "running" && (
              <div role="status">
                <div className="h-1.5 overflow-hidden rounded-full bg-hover">
                  <div className="progress-slide h-full w-1/3 bg-accent" />
                </div>
                <p className="mt-1.5 text-xs text-muted tabular-nums">
                  {p?.items_total
                    ? `Saving ${new Intl.NumberFormat().format(p.items ?? 0)} of ${new Intl.NumberFormat().format(p.items_total)} entries`
                    : p?.files_total
                      ? `Reading file ${(p.files_done ?? 0) + 1} of ${p.files_total}${p.file ? `: ${p.file}` : ""}`
                      : "Starting"}
                  . You can leave this page open.
                </p>
              </div>
            )}
            {error && <p role="alert" className="text-danger">{error}</p>}
            {job?.status === "failed" && <p role="alert" className="text-danger">{job.error ?? "The import failed."}</p>}
            {result && (
              <div>
                <p className="text-accent">
                  Imported. Open Sources and press Rebuild to add it to the graph.
                </p>
                <div className="mt-3 grid grid-cols-2 gap-4">
                  <Stat label="New days" value={result.inserted} />
                  <Stat label="Updated" value={result.updated} />
                  <Stat label="Unchanged" value={result.unchanged} />
                  <Stat label="Places" value={result.places} />
                  <Stat label="Secrets removed" value={result.redactions} />
                </div>
                {result.notes?.length > 0 && (
                  <ul className="mt-3 list-disc space-y-1 pl-5 text-xs leading-5 text-muted">
                    {result.notes.map((n) => <li key={n}>{n}</li>)}
                  </ul>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

const LINKEDIN_STEPS = [
  "On linkedin.com click Me, then Settings & Privacy, then Data privacy, then Get a copy of your data.",
  "Fast option: choose Want something in particular? and tick Connections only. It usually takes minutes.",
  "Full option: choose Download larger data archive for your positions, education, projects and skills. LinkedIn says it can take up to 24 hours.",
  "Click Request archive and confirm your password. LinkedIn emails you a download link.",
  "Download the .zip and upload it below, or give its path or an extracted folder.",
];

function LinkedInCard() {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [upload, setUpload] = useState<number | null>(null);
  const [path, setPath] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const running = starting || job?.status === "running";

  useEffect(() => {
    if (job?.status !== "running") return;
    const t = setInterval(() => {
      api.job(job.id).then(setJob).catch(() => {});
    }, 1500);
    return () => clearInterval(t);
  }, [job]);

  async function start(args: { file?: File; path?: string }) {
    if (running) return;
    setError(null);
    setStarting(true);
    setUpload(args.file ? 0 : null);
    try {
      setJob(await api.importLinkedin(args, args.file ? setUpload : undefined));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setStarting(false);
      setUpload(null);
      if (input.current) input.current.value = "";
    }
  }

  const result = job?.status === "succeeded" ? (job.result as unknown as LinkedInResult | null) : null;
  const secondary =
    "rounded-md border border-line bg-panel px-3 py-1.5 text-sm font-medium text-soft transition-colors hover:bg-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50";

  return (
    <section className="rounded-lg border border-line bg-panel" aria-labelledby="linkedin-title">
      <div className="grid gap-6 p-5 sm:p-6 md:grid-cols-2">
        <div>
          <h2 id="linkedin-title" className="text-lg font-semibold tracking-tight text-ink">
            LinkedIn
          </h2>
          <p className="mt-1 text-sm leading-6 text-muted">
            Your profile (positions, education, projects, skills) and your connections with their company and position.
          </p>
          <ol className="mt-3 list-decimal space-y-2 pl-5 text-sm leading-6 text-soft marker:text-muted">
            {LINKEDIN_STEPS.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
          <p className="mt-4 text-sm leading-6 text-muted">
            <span className="font-medium text-soft">How long it takes:</span> Connections only is usually ready in
            minutes; the larger archive can take up to 24 hours.
          </p>
          <p className="mt-2 text-sm leading-6 text-muted">
            Only profile and connection files are read; messages and login history are ignored. This data stays on this
            computer and is never sent to a model unless you opt in.
          </p>
        </div>

        <div className="flex flex-col gap-4">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void start({ path });
            }}
            className="flex flex-col gap-2 sm:flex-row"
          >
            <label htmlFor="linkedin-path" className="sr-only">Path to a LinkedIn export zip or folder</label>
            <input
              id="linkedin-path"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="~/Downloads/Basic_LinkedInDataExport.zip or a folder"
              className="min-w-0 flex-1 rounded-md border border-line bg-bg px-3 py-1.5 font-mono text-sm outline-none placeholder:text-muted focus-visible:border-accent"
            />
            <button type="submit" disabled={running || !path.trim()} className={secondary}>Import path</button>
          </form>
          <label
            htmlFor="linkedin-file"
            className={`flex min-h-24 cursor-pointer flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-line px-4 py-5 text-center text-sm transition-colors hover:bg-hover ${running ? "pointer-events-none opacity-60" : ""}`}
          >
            <span className="font-medium text-ink">Or choose the zip file</span>
            <span className="text-xs text-muted">The .zip from LinkedIn&apos;s email, no need to unzip it.</span>
            <input
              ref={input}
              id="linkedin-file"
              type="file"
              accept=".zip,.csv,application/zip,text/csv"
              className="sr-only"
              disabled={running}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void start({ file });
              }}
            />
          </label>

          <div aria-live="polite" className="min-h-6 text-sm">
            {upload !== null && <p className="text-xs text-muted">Uploading {Math.round(upload * 100)}%</p>}
            {job?.status === "running" && (
              <div role="status">
                <div className="h-1.5 overflow-hidden rounded-full bg-hover">
                  <div className="progress-slide h-full w-1/3 bg-accent" />
                </div>
                <p className="mt-1.5 text-xs text-muted">Saving your profile and connections. You can leave this page open.</p>
              </div>
            )}
            {error && <p role="alert" className="text-danger">{error}</p>}
            {job?.status === "failed" && <p role="alert" className="text-danger">{job.error ?? "The import failed."}</p>}
            {result && (
              <div>
                <p className="text-accent">Imported. Open Sources and press Rebuild to add it to the graph.</p>
                <div className="mt-3 grid grid-cols-2 gap-4">
                  <Stat label="Connections" value={result.connections} />
                  <Stat label="Positions" value={result.positions} />
                  <Stat label="Schools" value={result.education} />
                  <Stat label="New" value={result.inserted} />
                  <Stat label="Updated" value={result.updated} />
                  <Stat label="Secrets removed" value={result.redactions} />
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

export default function ImportPage() {
  return (
    <main className="mx-auto w-full max-w-5xl px-4 py-10 sm:px-6 sm:py-14">
      <header className="border-b border-line pb-7">
        <p className="text-xs font-medium uppercase tracking-[0.16em] text-accent">Sources</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight text-ink">Import exports</h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-soft">
          Download your history from Google or each assistant, then give the file to its card. Passwords, API keys and tokens are
          removed before anything is saved, and the uploaded file itself is not kept. Importing the same file again
          only updates what changed.
        </p>
      </header>

      <div className="mt-8 space-y-6">
        <TakeoutCard />
        <LinkedInCard />
        {PROVIDERS.map((p) => (
          <ProviderCard key={p.id} provider={p} />
        ))}

        <section className="rounded-lg border border-line bg-panel p-5 sm:p-6">
          <h2 className="text-lg font-semibold tracking-tight text-ink">Claude Code and Codex</h2>
          <p className="mt-2 text-sm leading-6 text-soft">
            These are imported automatically from this machine; there is nothing to download. Run{" "}
            <code className="rounded bg-hover px-1.5 py-0.5 font-mono text-xs text-ink">igneous sync ai_chats</code> in a
            terminal to pick up new sessions.
          </p>
        </section>
      </div>
    </main>
  );
}
