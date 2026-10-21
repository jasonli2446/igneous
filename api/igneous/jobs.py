"""Background jobs for the browser UI (syncs, pipeline runs, Google sign-in).

State lives in a small Postgres `jobs` table, created idempotently like google_auth's oauth_tokens, so a page
reload (or an API restart) still shows what happened. A partial unique index allows one running job per source.
Jobs run in a daemon thread with their own Session; progress counts are written at most once a second.
"""
import json
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


class JobBusy(RuntimeError):
    pass


def ensure_table(s: Session) -> None:
    s.execute(text("""
        CREATE TABLE IF NOT EXISTS jobs (
            id          text PRIMARY KEY,
            kind        text NOT NULL,            -- sync | pipeline | connect
            source      text NOT NULL,            -- connector id, 'pipeline', or 'google_connect'
            account     text,
            status      text NOT NULL DEFAULT 'running',   -- running | succeeded | failed
            started_at  timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            progress    jsonb NOT NULL DEFAULT '{}',
            result      jsonb,
            error       text)
    """))
    s.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_running ON jobs (source) WHERE status = 'running'"))
    s.commit()


def fail_interrupted(s: Session) -> int:
    """A job still 'running' at API startup lost its thread (restart / --reload): mark it failed."""
    ensure_table(s)
    n = s.execute(text("""
        UPDATE jobs SET status = 'failed', finished_at = now(),
               error = 'Interrupted because the API restarted. Start it again; it resumes where it stopped.'
        WHERE status = 'running'
    """)).rowcount
    s.commit()
    return n


def _row(r) -> dict:
    return dict(r._mapping)


def get(s: Session, job_id: str) -> dict | None:
    ensure_table(s)
    r = s.execute(text("SELECT * FROM jobs WHERE id = :i"), {"i": job_id}).first()
    return _row(r) if r else None


def recent(s: Session, limit: int = 30) -> list[dict]:
    ensure_table(s)
    return [_row(r) for r in s.execute(text("SELECT * FROM jobs ORDER BY started_at DESC LIMIT :n"), {"n": limit})]


def latest_by_source(s: Session) -> dict[str, dict]:
    ensure_table(s)
    rows = s.execute(text("SELECT DISTINCT ON (source) * FROM jobs ORDER BY source, started_at DESC"))
    return {r.source: _row(r) for r in rows}


def start(kind: str, source: str, runner: Runner, account: str | None = None,
          session_factory=SessionLocal) -> dict:
    """Create the job row (JobBusy if `source` already has a running job) and run `runner` in a thread."""
    job_id = uuid.uuid4().hex[:12]
    with session_factory() as s:
        ensure_table(s)
        try:
            s.execute(text("INSERT INTO jobs (id, kind, source, account) VALUES (:i, :k, :s, :a)"),
                      {"i": job_id, "k": kind, "s": source, "a": account})
            s.commit()
        except IntegrityError as e:
            s.rollback()
            raise JobBusy(f"{source} is already running") from e
    threading.Thread(target=_work, args=(job_id, runner, session_factory), daemon=True,
                     name=f"job-{source}").start()
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
        status, error = "failed", (str(e) or type(e).__name__)[:500]
    final = None
    if result and {"inserted", "updated", "unchanged"} <= result.keys():
        final = {"items": result["inserted"] + result["updated"] + result["unchanged"]}
    with session_factory() as s:
        s.execute(text("""
            UPDATE jobs SET status = :st, finished_at = now(), error = :e, result = CAST(:r AS jsonb),
                   progress = COALESCE(CAST(:p AS jsonb), progress)
            WHERE id = :i"""),
                  {"st": status, "e": error, "i": job_id,
                   "r": json.dumps(result) if result is not None else None,
                   "p": json.dumps(final) if final else None})
        s.commit()


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
    """`run_sync` with progress: exactly what `igneous sync` does, plus item counts for the job row."""
    return run_sync(s, _Counting(connector, progress), account=account, limit=limit)
