// GET /sources as every page shares it: what a record from each stored source value is called (one label map, the
// names the Sources page uses), and whether the hosted model is set up. Fetched once per page load; the Sources page,
// which polls it, keeps it current.

import { useSyncExternalStore } from "react";
import { api, type SourcesAnswer } from "@/lib/api";

let answer: SourcesAnswer | null = null;
let loading: Promise<void> | null = null;
const listeners = new Set<() => void>();

/** Share a fresh answer with every page. */
export function shareSources(fresh: SourcesAnswer) {
  answer = fresh;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  // A failed load is tried again by the next page that asks.
  if (!answer) loading ??= api.sources().then(shareSources, () => { loading = null; });
  return () => { listeners.delete(listener); };
}

/** The last answer of GET /sources, or null until it lands (and while the API can't be reached). */
export function useSourcesAnswer(): SourcesAnswer | null {
  return useSyncExternalStore(subscribe, () => answer, () => null);
}

/** What a record from a stored source value (an item's or a hit's `source`) is called; the value itself until the
 *  labels land. */
export function useSourceLabel(): (value: string) => string {
  const labels = useSourcesAnswer()?.labels;
  return (value) => labels?.[value] ?? value;
}
