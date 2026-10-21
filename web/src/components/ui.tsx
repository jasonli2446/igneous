// Small shared placeholders so loading and error states look the same everywhere.

export function Skeleton({ className = "" }: { className?: string }) {
  return <div aria-hidden="true" className={`animate-pulse rounded bg-hover ${className}`} />;
}

export function InlineError({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex items-start gap-2 text-xs leading-relaxed text-danger">
      <span className="min-w-0 break-words">{message}</span>
      {onRetry && (
        <button onClick={onRetry} className="shrink-0 font-medium underline underline-offset-2 hover:no-underline">Retry</button>
      )}
    </div>
  );
}

/** Thin indeterminate bar pinned to the top edge of its (relative) parent. */
export function TopProgress({ active }: { active: boolean }) {
  return (
    <div role="progressbar" aria-label="Loading" aria-hidden={!active}
      className={`pointer-events-none absolute inset-x-0 top-0 z-30 h-0.5 overflow-hidden transition-opacity duration-200 ${active ? "opacity-100" : "opacity-0"}`}>
      <div className="progress-slide h-full w-1/3 bg-accent" />
    </div>
  );
}
