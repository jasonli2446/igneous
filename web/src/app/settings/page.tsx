"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Skeleton } from "@/components/ui";
import { api, SyncStatus } from "@/lib/api";

const CONNECTOR_NAMES: Record<string, string> = {
  email: "Maildir email",
  gmail: "Gmail",
  imessage: "Messages",
  gcal: "Google Calendar",
  gcontacts: "Google Contacts",
};

function sourceName(connector: string) {
  return CONNECTOR_NAMES[connector] ?? connector;
}

function formatCount(count: number) {
  return new Intl.NumberFormat().format(count);
}

function formatDate(date: string | null) {
  if (!date) return "No saved checkpoint";
  const parsed = new Date(date);
  if (Number.isNaN(parsed.getTime())) return "Unavailable";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

function freshness(age: number | null) {
  if (age === null) return null;
  if (age < 60) return "less than a minute ago";
  if (age < 3600) return `${Math.floor(age / 60)} min ago`;
  if (age < 86400) return `${Math.floor(age / 3600)} hr ago`;
  return `${Math.floor(age / 86400)} days ago`;
}

function SyncRow({ sync }: { sync: SyncStatus }) {
  const isRecorded = sync.status === "known";
  const relativeTime = freshness(sync.cursor_age_seconds);

  return (
    <li className="grid gap-5 border-b border-line px-5 py-4 last:border-b-0 sm:grid-cols-[minmax(11rem,1.1fr)_minmax(10rem,1fr)_8rem] sm:items-center sm:gap-6">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <h2 className="font-medium text-ink">{sourceName(sync.connector)}</h2>
          <span className={`inline-flex items-center gap-1.5 text-xs ${isRecorded ? "text-accent" : "text-muted"}`}>
            <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${isRecorded ? "bg-accent" : "bg-muted"}`} />
            {isRecorded ? "Checkpoint recorded" : "Not yet synced"}
          </span>
        </div>
        <p className="mt-1 truncate font-mono text-xs text-muted">{sync.account}</p>
      </div>

      <div>
        <p className="text-xs text-muted">Last synced</p>
        <p className="mt-1 text-sm text-soft">{formatDate(sync.last_synced_at)}</p>
        {relativeTime && <p className="mt-0.5 text-xs text-muted">{relativeTime}</p>}
        {sync.cursor.gmail_backfill_in_progress && (
          <p className="mt-1 text-xs text-muted">A resumable backfill checkpoint is saved.</p>
        )}
      </div>

      <div className="sm:text-right">
        <p className="text-xs text-muted">Items indexed</p>
        <p className="mt-1 text-xl font-medium tabular-nums text-ink">{formatCount(sync.item_count)}</p>
        
      </div>
    </li>
  );
}

export default function SettingsPage() {
  const [syncs, setSyncs] = useState<SyncStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [checkedAt, setCheckedAt] = useState<Date | null>(null);
  const requestRef = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    requestRef.current?.abort();
    const request = new AbortController();
    requestRef.current = request;
    setLoading(true);
    setError(null);
    try {
      const result = await api.sync(request.signal);
      if (requestRef.current !== request) return;
      setSyncs(result.syncs);
      setCheckedAt(new Date());
    } catch (cause) {
      if (!request.signal.aborted) setError(`Could not read sync status. (${String(cause)})`);
    } finally {
      if (requestRef.current === request) {
        requestRef.current = null;
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void load();
    return () => requestRef.current?.abort();
  }, [load]);

  return (
    <main className="mx-auto w-full max-w-3xl px-4 py-8 sm:px-6">
      <header className="flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-ink">Sources</h1>
          <p className="mt-1 max-w-2xl text-sm text-muted">
            Where your data comes from, when each source last synced, and how much is indexed.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {checkedAt && <p className="text-xs text-muted">Checked {checkedAt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</p>}
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="rounded-md border border-line bg-panel px-3 py-2 text-sm font-medium text-soft transition-colors hover:bg-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-60"
          >
            {loading && syncs === null ? "Checking status…" : loading ? "Refreshing…" : "Refresh status"}
          </button>
        </div>
      </header>

      <section className="mt-6 rounded-md border border-line bg-panel" aria-labelledby="sources-heading">
        <div className="flex items-baseline justify-between border-b border-line px-5 py-3">
          <h2 id="sources-heading" className="text-sm font-medium text-ink">Connected sources</h2>
          {syncs && <p className="text-sm tabular-nums text-muted">{syncs.length} {syncs.length === 1 ? "source" : "sources"}</p>}
        </div>

        {error ? (
          <div className="px-5 py-12" role="alert">
            <p className="font-medium text-danger">Status unavailable</p>
            <p className="mt-1 text-sm leading-6 text-soft">{error}</p>
            <button type="button" onClick={() => void load()} className="mt-4 text-sm font-medium text-accent underline underline-offset-4">Try again</button>
          </div>
        ) : loading && syncs === null ? (
          <div className="space-y-4 px-5 py-6" role="status" aria-label="Loading sources">
            <Skeleton className="h-10 w-full" /><Skeleton className="h-10 w-full" />
          </div>
        ) : syncs?.length ? (
          <ul aria-label="Sync records">
            {syncs.map((sync) => <SyncRow key={`${sync.connector}:${sync.account}`} sync={sync} />)}
          </ul>
        ) : (
          <div className="px-5 py-12">
            <p className="font-medium text-ink">No source checkpoints yet</p>
            <p className="mt-1 max-w-xl text-sm leading-6 text-muted">
              This workspace has no saved sync records. Sources appear here after the ingestion process saves a checkpoint.
            </p>
          </div>
        )}
      </section>

      <p className="mt-4 text-xs leading-5 text-muted">
        This page is read-only: it shows saved checkpoints and does not start or monitor a sync. New data is added from <Link href="/import" className="text-accent hover:underline">Import</Link>.
      </p>
    </main>
  );
}
