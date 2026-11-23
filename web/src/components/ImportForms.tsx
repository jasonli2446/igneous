"use client";

import { useEffect, useEffectEvent, useId, useRef, useState } from "react";
import { type AiProvider, api, type ImportInput, type ImportJob, type ImportJobOf } from "@/lib/api";
import { useSourcesAnswer } from "@/lib/sources";

const nf = new Intl.NumberFormat();
const secondary =
  "rounded-md border border-line bg-panel px-3 py-1.5 text-sm font-medium text-soft transition-colors hover:bg-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50";
const pathInput =
  "min-w-0 flex-1 rounded-md border border-line bg-bg px-3 py-1.5 font-mono text-sm outline-none placeholder:text-muted focus-visible:border-accent";

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <p className="text-[11px] font-medium uppercase tracking-[0.12em] text-muted">{label}</p>
      <p className="mt-1 text-xl font-medium tabular-nums text-ink">{nf.format(value)}</p>
    </div>
  );
}

function Drop({ title, hint, accept, multiple, disabled, onFiles }: {
  title: string; hint: string; accept: string; multiple?: boolean; disabled: boolean;
  onFiles: (files: File[]) => void;
}) {
  const id = useId();
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  return (
    <label htmlFor={id}
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        const files = Array.from(e.dataTransfer.files);
        if (files.length) onFiles(multiple ? files : files.slice(0, 1));
      }}
      className={`flex min-h-24 cursor-pointer flex-col items-center justify-center gap-1 rounded-lg border border-dashed px-4 py-5 text-center text-sm transition-colors focus-within:border-accent focus-within:bg-hover ${
        dragging ? "border-accent bg-hover" : "border-line hover:bg-hover"
      } ${disabled ? "pointer-events-none opacity-60" : ""}`}>
      <span className="font-medium text-ink">{title}</span>
      <span className="text-xs text-muted">{hint}</span>
      <input ref={input} id={id} type="file" accept={accept} multiple={multiple} className="sr-only" disabled={disabled}
        onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          if (input.current) input.current.value = "";
          if (files.length) onFiles(files);
        }} />
    </label>
  );
}

/** Starts a background import job, polls it while it runs, and calls onDone once when it succeeds. `result` is the
 *  finished job's own result, typed by the job the route starts. */
function useImportJob<S extends string, P, R>(
  start: (input: ImportInput, onProgress?: (fraction: number) => void) => Promise<ImportJobOf<S, P, R>>, onDone: () => void) {
  const [job, setJob] = useState<ImportJobOf<S, P, R> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [upload, setUpload] = useState<number | null>(null);
  const running = starting || job?.status === "running";

  const runningId = job?.status === "running" ? job.id : null;
  const poll = useEffectEvent((signal: AbortSignal) => {
    if (job) api.job(job, signal).then((j) => { if (!signal.aborted) setJob(j); }, () => {});
  });
  useEffect(() => {
    if (!runningId) return;
    const request = new AbortController();
    const timer = setInterval(() => poll(request.signal), 1500);
    return () => { clearInterval(timer); request.abort(); };
  }, [runningId]);

  const done = useEffectEvent(onDone);
  const finished = job?.status === "succeeded" ? job.id : null;
  useEffect(() => {
    if (finished) done();
  }, [finished]);

  async function run(input: ImportInput) {
    if (running) return;
    const uploading = !!input.files?.length;
    setError(null);
    setStarting(true);
    setUpload(uploading ? 0 : null);
    try {
      setJob(await start(input, uploading ? setUpload : undefined));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setStarting(false);
      setUpload(null);
    }
  }
  // Only a finished import has a result: one that failed stored nothing and has none.
  const result = job?.result ?? null;
  return { job, error, upload, running, run, result };
}

function PathForm({ label, placeholder, disabled, onSubmit }: {
  label: string; placeholder: string; disabled: boolean; onSubmit: (path: string) => void;
}) {
  const id = useId();
  const [path, setPath] = useState("");
  return (
    <form onSubmit={(e) => { e.preventDefault(); onSubmit(path); }} className="flex flex-col gap-2 sm:flex-row">
      <label htmlFor={id} className="sr-only">{label}</label>
      <input id={id} value={path} onChange={(e) => setPath(e.target.value)} placeholder={placeholder} className={pathInput} />
      <button type="submit" disabled={disabled || !path.trim()} className={secondary}>Import path</button>
    </form>
  );
}

function JobStatus({ upload, job, error, running, children }: {
  upload: number | null; job: ImportJob | null; error: string | null; running: boolean; children: React.ReactNode;
}) {
  return (
    <div aria-live="polite" className="min-h-6 text-sm">
      {upload !== null && <p className="text-xs text-muted">Uploading {Math.round(upload * 100)}%</p>}
      {job?.status === "running" && (
        <div role="status">
          <div className="h-1.5 overflow-hidden rounded-full bg-hover"><div className="progress-slide h-full w-1/3 bg-accent" /></div>
          <p className="mt-1.5 text-xs text-muted tabular-nums">{children} You can leave this page open.</p>
        </div>
      )}
      {error && <p role="alert" className="text-danger">{error}</p>}
      {job?.status === "failed" && <p role="alert" className="text-danger">{job.error ?? "The import failed."}</p>}
      {!running && job?.status === "succeeded" && <p className="text-accent">Imported. Press Rebuild below to add it to the graph.</p>}
    </div>
  );
}

function TakeoutForm({ onDone }: { onDone: () => void }) {
  const { job, error, upload, running, run, result } = useImportJob(api.importTakeout, onDone);
  const p = job?.progress;
  return (
    <div className="flex flex-col gap-4">
      <div>
        <button type="button" disabled={running} className={secondary} onClick={() => void run({ scanDownloads: true })}>
          Scan Downloads for takeout-*.zip
        </button>
        <p className="mt-1.5 text-xs text-muted">Best for multi-GB files: they are read in place, not uploaded.</p>
      </div>
      <PathForm label="Path to a Takeout zip or folder" placeholder="~/Downloads/takeout-2025.zip or a folder"
        disabled={running} onSubmit={(path) => void run({ path })} />
      <Drop title="Or choose the zip files" hint="Select all parts of a split export at once."
        accept=".zip,application/zip" multiple disabled={running} onFiles={(files) => void run({ files })} />
      <JobStatus upload={upload} job={job} error={error} running={running}>
        {p?.items_total
          ? `Saving ${nf.format(p.items ?? 0)} of ${nf.format(p.items_total)} entries.`
          : p?.files_total
            ? `Reading file ${(p.files_done ?? 0) + 1} of ${p.files_total}${p.file ? `: ${p.file}` : ""}.`
            : "Starting."}
      </JobStatus>
      {result && (
        <div>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
            <Stat label="New days" value={result.inserted} />
            <Stat label="Updated" value={result.updated} />
            <Stat label="Unchanged" value={result.unchanged} />
            <Stat label="Places" value={result.places} />
            <Stat label="Secrets removed" value={result.redactions} />
          </div>
          {result.notes.length > 0 && (
            <ul className="mt-3 list-disc space-y-1 pl-5 text-xs leading-5 text-muted">
              {result.notes.map((n) => <li key={n}>{n}</li>)}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function LinkedInForm({ onDone }: { onDone: () => void }) {
  const { job, error, upload, running, run, result } = useImportJob(api.importLinkedin, onDone);
  return (
    <div className="flex flex-col gap-4">
      <PathForm label="Path to a LinkedIn export zip or folder" placeholder="~/Downloads/Basic_LinkedInDataExport.zip or a folder"
        disabled={running} onSubmit={(path) => void run({ path })} />
      <Drop title="Or choose the zip file" hint="The .zip from LinkedIn's email, no need to unzip it."
        accept=".zip,.csv,application/zip,text/csv" disabled={running} onFiles={(files) => void run({ files })} />
      <JobStatus upload={upload} job={job} error={error} running={running}>Saving your profile and connections.</JobStatus>
      {result && (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Stat label="Connections" value={result.connections} />
          <Stat label="Positions" value={result.positions} />
          <Stat label="Schools" value={result.education} />
          <Stat label="New" value={result.inserted} />
          <Stat label="Updated" value={result.updated} />
          <Stat label="Secrets removed" value={result.redactions} />
        </div>
      )}
    </div>
  );
}

// Gemini chats come from Google Takeout and count under its entry (sources.describe).
const COUNTED_UNDER: Record<AiProvider, string> = { chatgpt: "chatgpt", claude_ai: "claude_ai", gemini: "takeout" };

/** The import box of one AI chat export entry. The API recognises which assistant an export comes from, so a file
 *  from another one still imports, and counts under that one's entry, which the result says. */
function AiExportForm({ entry, onDone }: { entry: string; onDone: () => void }) {
  const { job, error, upload, running, run, result } = useImportJob(api.importAiExport, onDone);
  const sources = useSourcesAnswer()?.sources;
  const read = job?.progress.conversations;
  const other = result && COUNTED_UNDER[result.provider] !== entry ? COUNTED_UNDER[result.provider] : null;
  return (
    <div className="flex flex-col gap-4">
      <PathForm label="Path to the export zip or conversations.json"
        placeholder="~/Downloads/export.zip" disabled={running} onSubmit={(path) => void run({ path })} />
      <Drop title="Or choose the export file" hint="The .zip from the email, or conversations.json from inside it."
        accept=".zip,.json,application/zip,application/json" disabled={running} onFiles={(files) => void run({ files })} />
      <JobStatus upload={upload} job={job} error={error} running={running}>
        {read ? `Read ${nf.format(read)} conversations so far.` : "Reading the export."}
      </JobStatus>
      {result && (
        <div>
          {other && (
            <p className="text-sm text-soft">
              These chats count under “{sources?.find((s) => s.id === other)?.name ?? other}”, the source they came from.
            </p>
          )}
          <div className="mt-3 grid grid-cols-2 gap-4 sm:grid-cols-4">
            <Stat label="Conversations" value={result.conversations} />
            <Stat label="New days" value={result.inserted} />
            <Stat label="Updated" value={result.updated} />
            <Stat label="Secrets removed" value={result.redactions} />
          </div>
        </div>
      )}
    </div>
  );
}

/** The inline import form for an export-based source, or null if the source has no form. */
export default function ImportForm({ sourceId, onDone }: { sourceId: string; onDone: () => void }) {
  switch (sourceId) {
    case "takeout": return <TakeoutForm onDone={onDone} />;
    case "linkedin": return <LinkedInForm onDone={onDone} />;
    case "chatgpt":
    case "claude_ai": return <AiExportForm entry={sourceId} onDone={onDone} />;
    default: return null;
  }
}
