"use client";

import { useRef, useState } from "react";
import { AiImportResult, api } from "@/lib/api";

type Provider = {
  id: "chatgpt" | "gemini" | "claude_ai";
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
    id: "gemini",
    name: "Gemini",
    wait: "Anywhere from a few minutes to several hours, depending on how much Google has to gather.",
    file: "The Takeout .zip, or the MyActivity.json inside it.",
    steps: [
      "Go to takeout.google.com and click Deselect all.",
      "Scroll to My Activity and tick it, then click All activity data included.",
      "Deselect everything except Gemini Apps, then click OK.",
      "Click Multiple formats, and change Activity records from HTML to JSON. HTML files are not supported.",
      "Click Next step, then Create export, and wait for Google's email with the download link.",
      "Upload the .zip here.",
    ],
    note: "Gemini activity only keeps your prompts and the replies Google logged, grouped into one chat per day.",
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

export default function ImportPage() {
  return (
    <main className="mx-auto w-full max-w-5xl px-4 py-10 sm:px-6 sm:py-14">
      <header className="border-b border-line pb-7">
        <p className="text-xs font-medium uppercase tracking-[0.16em] text-accent">Sources</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight text-ink">Import AI chats</h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-soft">
          Download your history from each assistant, then drop the file in its card. Passwords, API keys and tokens are
          removed before anything is saved, and the uploaded file itself is not kept. Importing the same file again
          only updates what changed.
        </p>
      </header>

      <div className="mt-8 space-y-6">
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
