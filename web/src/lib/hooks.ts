import { useEffect, useEffectEvent, useState } from "react";

type Settled<T> = { key: string; tag: string; data?: T; error?: unknown };

/**
 * What `load` returns for `key`, fetched again when `key` or `version` changes or on retry(). Only the reply to the
 * latest request lands: an older request is aborted and a late reply to it is dropped. While a new version loads,
 * `data` keeps the last answer for the same key; a different key starts empty. A null key fetches nothing, and an
 * answer already held is not fetched again when the page is shown again.
 */
export function useRequest<T>(key: string | number | null, load: (signal: AbortSignal) => Promise<T>, version = 0) {
  const [attempt, setAttempt] = useState(0);
  const [settled, setSettled] = useState<Settled<T> | null>(null);
  const id = key === null ? null : String(key);
  const tag = `${version}.${attempt}`;
  const run = useEffectEvent(load);
  const answered = settled?.key === id && settled?.tag === tag;

  useEffect(() => {
    if (id === null || answered) return;
    const request = new AbortController();
    run(request.signal).then(
      (data) => { if (!request.signal.aborted) setSettled({ key: id, tag, data }); },
      (error: unknown) => {
        if (!request.signal.aborted) setSettled((s) => ({ key: id, tag, error, data: s?.key === id ? s.data : undefined }));
      },
    );
    return () => request.abort();
  }, [id, tag, answered]);

  const mine = settled?.key === id ? settled : null;
  return {
    data: mine?.data,
    error: mine?.tag === tag ? mine.error : undefined,
    loading: id !== null && mine?.tag !== tag,
    retry: () => setAttempt((n) => n + 1),
  };
}

/**
 * Search as you type: once the trimmed query has `min` characters, `find` runs `delay` ms after the last keystroke.
 * `results` answer the current query only (null while that answer is pending), never an older one; a newer query
 * aborts the older request. `onResults` hears each answer as it lands.
 */
export function useLiveSearch<T>(query: string, find: (q: string, signal: AbortSignal) => Promise<T[]>,
  { min = 2, delay = 200, onResults }: { min?: number; delay?: number; onResults?: (q: string, results: T[]) => void } = {}) {
  const q = query.trim();
  const [found, setFound] = useState<{ q: string; results: T[]; failed: boolean } | null>(null);
  const run = useEffectEvent(find);
  const landed = useEffectEvent((answered: string, results: T[]) => onResults?.(answered, results));

  useEffect(() => {
    if (q.length < min) return;
    const request = new AbortController();
    const timer = setTimeout(() => {
      run(q, request.signal).then(
        (results) => {
          if (request.signal.aborted) return;
          setFound({ q, results, failed: false });
          landed(q, results);
        },
        () => { if (!request.signal.aborted) setFound({ q, results: [], failed: true }); },
      );
    }, delay);
    return () => { clearTimeout(timer); request.abort(); };
  }, [q, min, delay]);

  const active = q.length >= min;
  const current = active && found?.q === q ? found : null;
  return { q, active, results: current ? current.results : null, failed: current?.failed ?? false };
}
