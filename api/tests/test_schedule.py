"""The scheduled sync (schedule.py) and job ownership (jobs.py): what runs unattended, and what a restart may fail."""
import os
import subprocess
import sys

from sqlalchemy import text

from igneous import jobs, schedule, sources


def add_item(s, source: str, external_id: str = "x") -> None:
    s.execute(text("INSERT INTO source_items (source, external_id, content_hash) VALUES (:s, :i, 'h')"),
              {"s": source, "i": external_id})


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
    monkeypatch.setattr(sources, "run_pipeline", lambda s, progress: ran.append(1) or {"spine": 0})
    result = schedule.run(db)
    assert result["fs"]["status"] == "succeeded" and result["fs"]["result"]["unchanged"] == 4
    assert result["ai_chats"] == {"status": "failed", "result": {"accounts": {}},
                                  "error": "No Claude Code or Codex sessions found on this Mac."}
    assert result["pipeline"]["status"] == "succeeded" and ran == [1]  # never rebuilt before
    assert schedule.run(db)["pipeline"] == {"skipped": "nothing new since the last rebuild"}
    from igneous.graph import dirty

    dirty.mark_rebuild(db)  # a deletion no rebuild has covered yet
    assert schedule.run(db)["pipeline"]["status"] == "succeeded" and ran == [1, 1]


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


def test_a_job_records_its_owner_and_runs_inline(committed_db):
    from igneous.db import SessionLocal

    job = jobs.run("sync", "jobtest-inline", lambda s, p: {"inserted": 1, "updated": 0, "unchanged": 0})
    assert job["status"] == "succeeded" and job["result"]["inserted"] == 1
    assert (job["owner_host"], job["owner_pid"]) == (jobs.HOST, os.getpid())
    with SessionLocal() as s:  # a finished job is never "interrupted"
        assert jobs.fail_interrupted(s) == 0
