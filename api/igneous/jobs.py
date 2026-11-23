"""Jobs: syncs, imports and pipeline runs, started from the Sources page, the command line or the scheduled sync.

State lives in the `jobs` table, so a page reload or an API restart still shows what happened, and a partial unique
index allows one running job per source, whoever started it. Each job records its owner, the host and process running
it: a job left running by a process that died is marked failed (`fail_interrupted`: before any job starts, when the
API or the scheduled sync starts, and when the Sources page reads the jobs), while one another live process runs is
left alone, and a job stopped where it runs (Ctrl-C on a command) is marked failed as it stops. `start` runs a job in
a daemon thread; `run` runs it in the caller and returns it finished; `call` runs it in the caller as if the caller
ran the runner itself, returning what it returns and raising what it raises, for a command that reports the outcome
itself. Each has its own Session; progress is written at most once a second.
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
    "import": "Interrupted: the process running it stopped before the import finished, so nothing was stored. "
              "Import again.",
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
    """Mark failed the running jobs whose process died (an API restart or --reload, a scheduled sync or a command
    that was killed), each with what to do next; returns how many. Safe to call from any process at any time."""
    rows = s.execute(text("SELECT id, kind, owner_host, owner_pid FROM jobs WHERE status = 'running'")).all()
    failed = 0
    for job in (r for r in rows if _dead(r)):
        error = INTERRUPTED.get(job.kind, "Interrupted: the process running it stopped.")
        # Only while it still runs: a job of this process that finished since the SELECT keeps how it ended.
        failed += s.execute(text("""UPDATE jobs SET status = 'failed', finished_at = now(), error = :e
                                    WHERE id = :i AND status = 'running'"""), {"i": job.id, "e": error}).rowcount
    s.commit()
    return failed


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
    """The job's row, owned by this process (JobBusy if a live process runs a job for `source`; one whose process
    died is failed first, and holds its source no longer)."""
    job_id = uuid.uuid4().hex[:12]
    _MINE.add(job_id)  # before the row exists, so no other thread takes it for a dead process's job
    try:
        with session_factory() as s:
            fail_interrupted(s)
            s.execute(text("""INSERT INTO jobs (id, kind, source, account, owner_host, owner_pid)
                              VALUES (:i, :k, :s, :a, :h, :p)"""),
                      {"i": job_id, "k": kind, "s": source, "a": account, "h": HOST, "p": os.getpid()})
            s.commit()
    except BaseException as e:
        _MINE.discard(job_id)
        if isinstance(e, IntegrityError):
            raise JobBusy(f"{source} is already running") from e
        raise
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


def call(kind: str, source: str, runner: Runner, account: str | None = None,
         session_factory=SessionLocal) -> dict | None:
    """Create the job row and run `runner` here, as if called directly: returns what it returns and raises what it
    raises, once the job row records how it ended (JobBusy if `source` is running). For a command, which reports
    the outcome itself."""
    result, raised = _work(_create(kind, source, account, session_factory), runner, session_factory)
    if raised is not None:
        raise raised
    return result


def _work(job_id: str, runner: Runner, session_factory) -> tuple[dict | None, Exception | None]:
    """Run the job and record how it ended; returns the result it recorded and what the runner raised, if anything."""
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

    status, result, error, raised = "succeeded", None, None, None
    try:
        with session_factory() as s:
            result = runner(s, progress)
    except Exception as e:
        # A sync that failed for some accounts still reports what the others stored.
        status, error, result, raised = "failed", (str(e) or type(e).__name__)[:500], getattr(e, "result", None), e
    except BaseException:
        # Stopped where it runs (Ctrl-C on a command): no longer running here, so it is failed as interrupted.
        _MINE.discard(job_id)
        with session_factory() as s:
            fail_interrupted(s)
        raise
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
    return result, raised


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
