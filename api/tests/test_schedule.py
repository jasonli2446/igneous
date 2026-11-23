"""The scheduled sync (schedule.py) and job ownership (jobs.py): what runs unattended, and what a restart may fail."""
import json
import os
import subprocess
import sys

from sqlalchemy import text

from igneous import jobs, pipeline, schedule, sources


def add_item(s, source: str, external_id: str = "x") -> None:
    s.execute(text("INSERT INTO source_items (source, external_id, content_hash) VALUES (:s, :i, 'h')"),
              {"s": source, "i": external_id})


def rebuilt(*stages: str) -> dict:
    """What a build returns (pipeline.run): each stage it ran, by default every stage a rebuild runs."""
    return {name: {"result": 0, "seconds": 0.0} for name in stages or [st.name for st in pipeline.plan()]}


def test_only_sources_you_set_up_are_due_and_never_the_demo(db, monkeypatch, tmp_path):
    monkeypatch.setattr(sources.settings, "filesystem_roots", str(tmp_path))  # requirements pass: not enough
    monkeypatch.setattr(sources.settings, "data_dir", str(tmp_path))
    (tmp_path / "maildir").mkdir()
    assert schedule.due(db) == []
    add_item(db, "fs")
    add_item(db, "email")  # the Enron demo is never synced unattended
    db.execute(text("INSERT INTO sync_state (connector, account, cursor) VALUES ('gcal', 'me@x.com', '{}')"))
    assert [row["id"] for row in schedule.due(db)] == ["gcal", "fs"]


def test_a_run_reports_each_sources_outcome_and_errors(db, monkeypatch, tmp_path):
    monkeypatch.setattr(sources.settings, "filesystem_roots", str(tmp_path))
    add_item(db, "fs")
    add_item(db, "ai_chats", "claude_code/s/2026-01-01")
    db.execute(text("UPDATE source_items SET raw_meta = '{\"tool\": \"claude_code\"}' WHERE source = 'ai_chats'"))
    monkeypatch.setattr(sources, "check_requirements", lambda sid: (True, None))

    def fake_sync(sid, session, account=None, opts=None, progress=None, limit=None):
        if sid == "ai_chats":
            raise sources.SyncFailed("No Claude Code or Codex sessions found on this Mac.", {"accounts": {}})
        return {"inserted": 0, "updated": 0, "unchanged": 4, "deleted": 0, "redactions": 0, "accounts": {}}

    monkeypatch.setattr(sources, "sync", fake_sync)
    ran = []
    monkeypatch.setattr(sources, "run_pipeline", lambda s, progress: ran.append(1) or rebuilt())
    result = schedule.run(db)
    assert result["fs"]["status"] == "succeeded" and result["fs"]["result"]["unchanged"] == 4
    assert result["ai_chats"] == {"status": "failed", "result": {"accounts": {}},
                                  "error": "No Claude Code or Codex sessions found on this Mac."}
    assert result["pipeline"]["status"] == "succeeded" and ran == [1]  # never rebuilt before
    assert schedule.run(db)["pipeline"] == {"skipped": "nothing new since the last rebuild"}
    from igneous.graph import dirty

    dirty.mark_rebuild(db)  # a deletion no rebuild has covered yet
    assert schedule.run(db)["pipeline"]["status"] == "succeeded" and ran == [1, 1]


def test_a_build_of_some_stages_is_not_a_rebuild(db):
    """`igneous pipeline --stage`, `igneous extract` and `igneous resolve` run as builds too, so the Sources page
    shows them, but they leave the other stages behind: what was synced before them still needs the rebuild."""
    def build(job_id: str, minutes_ago: int, result: dict) -> None:
        db.execute(text("""INSERT INTO jobs (id, kind, source, status, started_at, finished_at, result)
                           VALUES (:i, 'pipeline', 'pipeline', 'succeeded', now() - make_interval(mins => :m), now(),
                                   CAST(:r AS jsonb))"""), {"i": job_id, "m": minutes_ago, "r": json.dumps(result)})

    build("rebuild", 30, rebuilt())
    add_item(db, "fs")
    db.execute(text("UPDATE source_items SET updated_at = now() - interval '20 minutes'"))
    assert schedule._pipeline_due(db, changed=False)  # synced after the rebuild started
    build("resolve-stage", 10, rebuilt("resolve", "corrections"))
    build("extract", 8, rebuilt("redact", "extract", "corrections"))
    build("igneous-resolve", 5, {"rescored": True, "same_as": 2, "clusters": 1})
    assert schedule._pipeline_due(db, changed=False)  # none of them read it into the graph
    build("rebuild-with-extract", 2, rebuilt(*pipeline.NAMES))
    assert not schedule._pipeline_due(db, changed=False)


def running(s, job_id: str, kind: str, host: str | None, pid: int | None) -> None:
    s.execute(text("INSERT INTO jobs (id, kind, source, owner_host, owner_pid) VALUES (:i, :k, :i, :h, :p)"),
              {"i": job_id, "k": kind, "h": host, "p": pid})


def status(s, job_id: str) -> tuple[str, str | None]:
    return tuple(s.execute(text("SELECT status, error FROM jobs WHERE id = :i"), {"i": job_id}).one())


def test_only_jobs_whose_process_died_are_failed(db):
    gone = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    dead_pid = int(gone.stdout)
    running(db, "scheduler", "sync", jobs.HOST, os.getppid())     # another live process: the scheduled sync
    running(db, "elsewhere", "sync", "another-host", 1)            # can't be checked from here
    running(db, "crashed", "import", jobs.HOST, dead_pid)
    running(db, "old-me", "pipeline", jobs.HOST, os.getpid())     # this pid, but an earlier process's job
    running(db, "unowned", "sync", None, None)
    assert jobs.fail_interrupted(db) == 3
    assert status(db, "scheduler") == ("running", None) and status(db, "elsewhere") == ("running", None)
    assert status(db, "crashed") == ("failed", jobs.INTERRUPTED["import"])
    assert "nothing was stored. Import again." in status(db, "crashed")[1]
    assert "Press Rebuild again" in status(db, "old-me")[1]
    assert "Sync again; it continues where it stopped" in status(db, "unowned")[1]


def test_a_job_that_finishes_while_jobs_are_checked_keeps_how_it_ended(db, monkeypatch):
    """The page and every job start check for dead jobs while this process's own jobs run: one that finishes between
    the check's read and its write (it then looks like a dead process's) is never failed."""
    running(db, "finishing", "sync", jobs.HOST, os.getpid())

    def finished_meanwhile(job) -> bool:
        db.execute(text("UPDATE jobs SET status = 'succeeded' WHERE id = :i"), {"i": job.id})
        return True

    monkeypatch.setattr(jobs, "_dead", finished_meanwhile)
    assert jobs.fail_interrupted(db) == 0 and status(db, "finishing") == ("succeeded", None)


def test_a_job_records_its_owner_and_runs_inline(committed_db):
    from igneous.db import SessionLocal

    job = jobs.run("sync", "jobtest-inline", lambda s, p: {"inserted": 1, "updated": 0, "unchanged": 0})
    assert job["status"] == "succeeded" and job["result"]["inserted"] == 1
    assert (job["owner_host"], job["owner_pid"]) == (jobs.HOST, os.getpid())
    with SessionLocal() as s:  # a finished job is never "interrupted"
        assert jobs.fail_interrupted(s) == 0
