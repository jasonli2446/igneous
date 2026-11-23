"""What changed since a stage last covered it, and the graph version readers cache against.

A source change (records added, changed or deleted) is recorded for every stage that skips work while its inputs
are unchanged (`pipeline.GATED`), once per writing transaction. Such a stage reads the changes it is about to cover
(`pending`) and removes exactly those (`consume`) in the transaction that publishes its work: a change committed
while it runs stays for the next run, a failed run keeps them all, and no stage consumes another's changes.

The graph version is bumped once each time a rebuild publishes (pipeline.publishing) and by each correction made
through the API, so the explorer and the MCP server know when a cached view is stale. `cached` keeps a reader's
results, in the process that read them, for as long as the graph stays at the version they were read at.
"""
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

CACHE_SIZE = 256  # results kept, least recently used first out
_cache: OrderedDict[tuple, Any] = OrderedDict()
_lock = threading.Lock()


def mark(s: Session, node_ids: set[int], reason: str) -> None:
    """Record that a source change touched `node_ids` (nothing to record when it touched none)."""
    if node_ids:
        _record(s, reason)


def mark_rebuild(s: Session) -> None:
    """Record that source records were deleted."""
    _record(s, "source records deleted")


def _record(s: Session, reason: str) -> None:
    from igneous.pipeline import GATED

    s.execute(text("""
        INSERT INTO pipeline_changes (stage, xact, reason)
        SELECT unnest(CAST(:stages AS text[])), pg_current_xact_id(), :reason
        ON CONFLICT (stage, xact) DO NOTHING
    """), {"stages": sorted(GATED), "reason": reason[:200]})


def pending(s: Session, stage: str) -> list[int]:
    """The changes `stage` has not covered yet, visible now."""
    return list(s.execute(text("SELECT id FROM pipeline_changes WHERE stage = :st ORDER BY id"),
                          {"st": stage}).scalars())


def consume(s: Session, stage: str, ids: list[int]) -> None:
    """`stage` covered these changes (from `pending`); takes effect when the caller's transaction commits."""
    if ids:
        s.execute(text("DELETE FROM pipeline_changes WHERE stage = :st AND id = ANY(:ids)"),
                  {"st": stage, "ids": ids})


def touch(s: Session) -> None:
    s.execute(text("UPDATE graph_version SET version = version + 1 WHERE id = true"))


def version(s: Session) -> int:
    return s.execute(text("SELECT version FROM graph_version WHERE id = true")).scalar_one()


def cached(s: Session, key: tuple, compute: Callable[[], Any]) -> Any:
    """`compute()`, remembered under `key` while the graph version stays what it is now, so a newer graph never gets
    an older answer. Callers must not change what it returns."""
    full = (version(s), *key)
    with _lock:
        if full in _cache:
            _cache.move_to_end(full)
            return _cache[full]
    value = compute()
    with _lock:
        _cache[full] = value
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return value


def forget() -> None:
    """Drop every cached result (a test's database starts over at the same version)."""
    with _lock:
        _cache.clear()
