// Dates as the app shows them, in the reader's locale and time zone. Each formatter takes the API's ISO strings (or a
// Date) and returns "" for a missing or unreadable date, so the caller decides what an absent date reads as.

type When = string | Date | null | undefined;

const DAY = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
const DAY_TIME = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

function parse(when: When): Date | null {
  if (!when) return null;
  const d = new Date(when);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "Mar 4, 2001" */
export function formatDay(when: When): string {
  const d = parse(when);
  return d ? DAY.format(d) : "";
}

/** "Mar 4, 2001, 3:05 PM" */
export function formatDayTime(when: When): string {
  const d = parse(when);
  return d ? DAY_TIME.format(d) : "";
}

/** "Mar 4 – Jun 1, 2001", or a single day when both ends fall on it. */
export function formatDayRange(from: When, to: When): string {
  const a = parse(from), b = parse(to);
  if (!a || !b) return formatDay(from) || formatDay(to);
  return a <= b ? DAY.formatRange(a, b) : DAY.formatRange(b, a);
}
