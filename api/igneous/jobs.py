"""Jobs: syncs, imports and pipeline runs, started from the browser UI or the scheduled sync.

State lives in the `jobs` table, so a page reload or an API restart still shows what happened, and a partial unique
index allows one running job per source. Each job records its owner, the host and process running it: a job left
running by a process that died is marked failed (`fail_interrupted`, when the API or the scheduled sync starts),
while one another live process runs is left alone. `start` runs a job in a daemon thread and `run` in the caller,
each with its own Session; progress is written at most once a second.
"""
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from igneous.connectors.base import Connector, SourceItem, run_sync
from igneous.db import SessionLocal

Progress = Callable[..., None]  # progress(dict, force=False); unforced calls are throttled to 1/s
Runner = Callable[[Session, Progress], dict | None]

HOST = socket.gethostname()
# What a job that stopped with its process leaves behind, and what to do about it.
INTERRUPTED = {
    "sync": "Interrupted: the process running it stopped. Sync again; it continues where it stopped.",
    "import": "Interrupted: the process running it stopped. Import again; an import starts over, and keeps what it "
              "already stored.",
    "pipeline": "Interrupted: the process running it stopped. Press Rebuild again; finished stages are kept.",
}
_MINE: set[str] = set()  # jobs this process started


class JobBusy(RuntimeError):
    pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, owned by another user
        return True
    return True


def _dead(job) -> bool:
    """Whether a running job's process is gone. Only a process on this host can be checked; a job without an owner
    predates owners and outlived its process."""
    if job.owner_pid is None:
        return True
    if job.owner_host != HOST:
        return False
    if job.owner_pid == os.getpid():  # this pid, but not this process's job: a process before it had the pid
        return job.id not in _MINE
    return not _alive(job.owner_pid)


def fail_interrupted(s: Session) -> int:
    """Mark failed the running jobs whose process died (an API restart or --reload, a scheduled sync that was
    killed), each with what to do next. Safe to call from any process at any time."""
    rows = s.execute(text("SELECT id, kind, owner_host, owner_pid FROM jobs WHERE status = 'running'")).all()
    dead = [r for r in rows if _dead(r)]
    for job in dead:
        s.execute(text("UPDATE jobs SET status = 'failed', finished_at = now(), error = :e WHERE id = :i"),
                  {"i": job.id, "e": INTERRUPTED.get(job.kind, "Interrupted: the process running it stopped.")})
    s.commit()
    return len(dead)


def _row(r) -> dict:
    return dict(r._mapping)


def get(s: Session, job_id: str) -> dict | None:
    r = s.execute(text("SELECT * FROM jobs WHERE id = :i"), {"i": job_id}).first()
    return _row(r) if r else None


def recent(s: Session, limit: int = 30) -> list[dict]:
    return [_row(r) for r in s.execute(text("SELECT * FROM jobs ORDER BY started_at DESC LIMIT :n"), {"n": limit})]


def latest_by_source(s: Session) -> dict[str, dict]:
    rows = s.execute(text("SELECT DISTINCT ON (source) * FROM jobs ORDER BY source, started_at DESC"))
    return {r.source: _row(r) for r in rows}


def _create(kind: str, source: str, account: str | None, session_factory) -> str:
    """The job's row, owned by this process (JobBusy if `source` already has a running job)."""
    job_id = uuid.uuid4().hex[:12]
    with session_factory() as s:
        try:
            s.execute(text("""INSERT INTO jobs (id, kind, source, account, owner_host, owner_pid)
                              VALUES (:i, :k, :s, :a, :h, :p)"""),
                      {"i": job_id, "k": kind, "s": source, "a": account, "h": HOST, "p": os.getpid()})
            s.commit()
        except IntegrityError as e:
            s.rollback()
            raise JobBusy(f"{source} is already running") from e
    _MINE.add(job_id)
    return job_id


def start(kind: str, source: str, runner: Runner, account: str | None = None,
          session_factory=SessionLocal) -> dict:
    """Create the job row (JobBusy if `source` already has a running job) and run `runner` in a thread."""
    job_id = _create(kind, source, account, session_factory)
    threading.Thread(target=_work, args=(job_id, runner, session_factory), daemon=True,
                     name=f"job-{source}").start()
    with session_factory() as s:
        return get(s, job_id)


def run(kind: str, source: str, runner: Runner, account: str | None = None, session_factory=SessionLocal) -> dict:
    """Create the job row and run `runner` here; returns the finished job (JobBusy if `source` is running)."""
    job_id = _create(kind, source, account, session_factory)
    _work(job_id, runner, session_factory)
    with session_factory() as s:
        return get(s, job_id)


def _work(job_id: str, runner: Runner, session_factory) -> None:
    last = 0.0

    def progress(p: dict, force: bool = False) -> None:
        nonlocal last
        if not force and time.monotonic() - last < 1.0:
            return
        last = time.monotonic()
        with session_factory() as ps:
            ps.execute(text("UPDATE jobs SET progress = CAST(:p AS jsonb) WHERE id = :i"),
                       {"p": json.dumps(p), "i": job_id})
            ps.commit()

    status, result, error = "succeeded", None, None
    try:
        with session_factory() as s:
            result = runner(s, progress)
    except Exception as e:
        # A sync that failed for some accounts still reports what the others stored.
        status, error, result = "failed", (str(e) or type(e).__name__)[:500], getattr(e, "result", None)
    final = None
    if result and {"inserted", "updated", "unchanged"} <= result.keys():
        final = {"items": result["inserted"] + result["updated"] + result["unchanged"]}
    with session_factory() as s:
        s.execute(text("""
            UPDATE jobs SET status = :st, finished_at = now(), error = :e, result = CAST(:r AS jsonb),
                   progress = COALESCE(CAST(:p AS jsonb), progress)
            WHERE id = :i"""),
                  {"st": status, "e": error, "i": job_id,
                   "r": json.dumps(result, default=str) if result is not None else None,
                   "p": json.dumps(final) if final else None})
        s.commit()
    _MINE.discard(job_id)


class _Counting:
    """Passes a connector's events through, reporting how many items it has produced."""

    def __init__(self, inner: Connector, progress: Progress):
        self.inner, self.progress, self.name, self.items = inner, progress, inner.name, 0

    def sync(self, cursor: dict) -> Iterator:
        for obj in self.inner.sync(cursor):
            if isinstance(obj, SourceItem):
                self.items += 1
                self.progress({"items": self.items})
            yield obj


def run_connector(s: Session, connector: Connector, account: str, progress: Progress,
                  limit: int | None = None) -> dict:
    """`run_sync` with progress: item counts for the job row."""
    return run_sync(s, _Counting(connector, progress), account=account, limit=limit)
