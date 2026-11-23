// Dates as the app shows them, in the reader's locale and time zone. Each formatter takes the API's ISO strings and
// returns "" for a missing or unreadable date, so the caller decides what an absent date reads as.

const DAY = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
const DAY_TIME = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "Mar 4, 2001" */
export function formatDay(iso: string | null | undefined): string {
  const d = parse(iso);
  return d ? DAY.format(d) : "";
}

/** "Mar 4, 2001, 3:05 PM" */
export function formatDayTime(iso: string | null | undefined): string {
  const d = parse(iso);
  return d ? DAY_TIME.format(d) : "";
}

/** "Mar 4 – Jun 1, 2001", or a single day when both ends fall on it. */
export function formatDayRange(from: string | null | undefined, to: string | null | undefined): string {
  const a = parse(from), b = parse(to);
  if (!a || !b) return formatDay(from) || formatDay(to);
  return a <= b ? DAY.formatRange(a, b) : DAY.formatRange(b, a);
}
