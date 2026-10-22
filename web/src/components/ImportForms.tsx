"use client";

import { useEffect, useRef, useState } from "react";
import { AiImportResult, api, Job, LinkedInResult, TakeoutResult } from "@/lib/api";

const nf = new Intl.NumberFormat();
const AI_LABEL: Record<string, string> = { chatgpt: "ChatGPT", gemini: "Gemini", claude_ai: "Claude.ai" };
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

function Drop({ id, title, hint, accept, multiple, disabled, onFiles }: {
  id: string; title: string; hint: string; accept: string; multiple?: boolean; disabled: boolean;
  onFiles: (files: File[]) => void;
}) {
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
      className={`flex min-h-24 cursor-pointer flex-col items-center justify-center gap-1 rounded-lg border border-dashed px-4 py-5 text-center text-sm transition-colors ${
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

/** Starts a background import job, polls it, and calls onDone once when it succeeds. */
function useImportJob(onDone: () => void) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [upload, setUpload] = useState<number | null>(null);
  const running = starting || job?.status === "running";

  useEffect(() => {
    if (job?.status !== "running") return;
    const t = setInterval(() => {
      api.job(job.id).then(setJob).catch(() => {});
    }, 1500);
    return () => clearInterval(t);
  }, [job]);

  const finished = job?.status === "succeeded" ? job.id : null;
  useEffect(() => {
    if (finished) onDone();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finished]);

  async function start(run: (onProgress: (f: number) => void) => Promise<Job>, withUpload: boolean) {
    if (running) return;
    setError(null);
    setStarting(true);
    setUpload(withUpload ? 0 : null);
    try {
      setJob(await run(setUpload));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setStarting(false);
      setUpload(null);
    }
  }
  return { job, error, upload, running, start };
}

function PathForm({ id, label, placeholder, disabled, onSubmit }: {
  id: string; label: string; placeholder: string; disabled: boolean; onSubmit: (path: string) => void;
}) {
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
  upload: number | null; job: Job | null; error: string | null; running: boolean; children: React.ReactNode;
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
  const { job, error, upload, running, start } = useImportJob(onDone);
  const p = job?.progress;
  const result = job?.status === "succeeded" ? (job.result as unknown as TakeoutResult | null) : null;
  return (
    <div className="flex flex-col gap-4">
      <div>
        <button type="button" disabled={running} className={secondary}
          onClick={() => void start(() => api.importTakeout({ scanDownloads: true }), false)}>
          Scan Downloads for takeout-*.zip
        </button>
        <p className="mt-1.5 text-xs text-muted">Best for multi-GB files: they are read in place, not uploaded.</p>
      </div>
      <PathForm id="takeout-path" label="Path to a Takeout zip or folder" placeholder="~/Downloads/takeout-2025.zip or a folder"
        disabled={running} onSubmit={(path) => void start(() => api.importTakeout({ path }), false)} />
      <Drop id="takeout-file" title="Or choose the zip files" hint="Select all parts of a split export at once."
        accept=".zip,application/zip" multiple disabled={running}
        onFiles={(files) => void start((on) => api.importTakeout({ files }, on), true)} />
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
          {result.notes?.length > 0 && (
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
  const { job, error, upload, running, start } = useImportJob(onDone);
  const result = job?.status === "succeeded" ? (job.result as unknown as LinkedInResult | null) : null;
  return (
    <div className="flex flex-col gap-4">
      <PathForm id="linkedin-path" label="Path to a LinkedIn export zip or folder"
        placeholder="~/Downloads/Basic_LinkedInDataExport.zip or a folder"
        disabled={running} onSubmit={(path) => void start(() => api.importLinkedin({ path }), false)} />
      <Drop id="linkedin-file" title="Or choose the zip file" hint="The .zip from LinkedIn's email, no need to unzip it."
        accept=".zip,.csv,application/zip,text/csv" disabled={running}
        onFiles={([file]) => void start((on) => api.importLinkedin({ file }, on), true)} />
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

type AiStatus =
  | { kind: "idle" }
  | { kind: "uploading"; progress: number; name: string }
  | { kind: "done"; result: AiImportResult }
  | { kind: "error"; message: string };

function AiExportForm({ id, onDone }: { id: string; onDone: () => void }) {
  const [status, setStatus] = useState<AiStatus>({ kind: "idle" });
  const busy = status.kind === "uploading";

  async function upload(file: File) {
    if (busy) return;
    setStatus({ kind: "uploading", progress: 0, name: file.name });
    try {
      const result = await api.importAiExport(file, undefined, (progress) => setStatus({ kind: "uploading", progress, name: file.name }));
      setStatus({ kind: "done", result });
      onDone();
    } catch (cause) {
      setStatus({ kind: "error", message: cause instanceof Error ? cause.message : String(cause) });
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <Drop id={`${id}-file`} title="Drop the export here or choose a file" hint="The .zip from the email, or conversations.json from inside it."
        accept=".zip,.json,application/zip,application/json" disabled={busy} onFiles={([file]) => void upload(file)} />
      <div aria-live="polite" className="min-h-6 text-sm">
        {status.kind === "uploading" && (
          <div>
            <div className="flex justify-between gap-3 text-xs text-muted">
              <span className="truncate">{status.progress >= 1 ? "Reading conversations" : "Uploading"} {status.name}</span>
              <span className="tabular-nums">{Math.round(status.progress * 100)}%</span>
            </div>
            <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-hover">
              <div className="h-full bg-accent transition-[width]" style={{ width: `${status.progress * 100}%` }} />
            </div>
          </div>
        )}
        {status.kind === "error" && <p role="alert" className="text-danger">{status.message}</p>}
        {status.kind === "done" && (
          <div>
            <p className="text-accent">
              Imported {AI_LABEL[status.result.provider] ?? status.result.provider} chats. Press Rebuild below to add them to the graph.
            </p>
            <div className="mt-3 grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label="Conversations" value={status.result.conversations} />
              <Stat label="New days" value={status.result.items_inserted} />
              <Stat label="Updated" value={status.result.items_updated} />
              <Stat label="Secrets removed" value={status.result.redactions} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** The inline import form for an export-based source, or null if the source has no form. */
export default function ImportForm({ sourceId, onDone }: { sourceId: string; onDone: () => void }) {
  switch (sourceId) {
    case "takeout": return <TakeoutForm onDone={onDone} />;
    case "linkedin": return <LinkedInForm onDone={onDone} />;
    case "chatgpt":
    case "claude_ai": return <AiExportForm id={sourceId} onDone={onDone} />;
    default: return null;
  }
}
