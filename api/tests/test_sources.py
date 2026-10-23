"""Sources registry shape and the background job runner. No network; jobs use a fake connector and the test database."""
import time

import pytest
from sqlalchemy import text

from igneous import jobs, sources
from igneous.connectors.base import Checkpoint, SourceItem
from igneous.db import SessionLocal

KINDS = {"google", "local", "remote", "import", "planned"}


def test_registry_shape():
    ids = [s["id"] for s in sources.SOURCES]
    assert len(ids) == len(set(ids))
    assert {"gmail", "gcal", "gcontacts", "drive", "imessage", "maccontacts", "ai_chats", "fs", "email",
            "chatgpt", "takeout", "claude_ai", "github"} <= set(ids)
    for s in sources.SOURCES:
        assert s["kind"] in KINDS
        assert s["name"] and s["reads"] and s["group"]
        assert isinstance(s["steps"], list) and s["steps"] and all(isinstance(x, str) for x in s["steps"])


def test_every_syncable_source_can_be_built(monkeypatch, tmp_path):
    monkeypatch.setattr(sources.settings, "data_dir", str(tmp_path))
    assert sources.SYNCABLE == {"gmail", "gcal", "gcontacts", "drive", "imessage", "maccontacts", "ai_chats", "fs",
                                "notes", "email", "github"}
    for sid in sources.SYNCABLE:
        ok, message = sources.check_requirements(sid)
        assert isinstance(ok, bool) and (ok or message)


def test_requirements_report_missing_client_secret_and_blocked_messages(monkeypatch, tmp_path):
    monkeypatch.setattr(sources.settings, "data_dir", str(tmp_path))
    ok, message = sources.check_requirements("gmail")
    assert not ok and "client_secret.json" in message
    (tmp_path / "google").mkdir()
    (tmp_path / "google" / "client_secret.json").write_text("{}")
    assert sources.check_requirements("gmail") == (True, None)

    monkeypatch.setattr(sources.imessage, "DEFAULT_DB", tmp_path / "missing.db")
    ok, message = sources.check_requirements("imessage")
    assert not ok and "not found" in message

    blocked = tmp_path / "chat.db"
    blocked.write_text("not sqlite")
    monkeypatch.setattr(sources.imessage, "DEFAULT_DB", blocked)
    ok, message = sources.check_requirements("imessage")
    assert not ok and "Full Disk Access" in message


def test_fs_accepts_configured_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(sources.settings, "filesystem_roots", str(tmp_path))
    assert sources.check_requirements("fs") == (True, None)


class FakeConnector:
    name = "fake"

    def __init__(self, n: int, fail: bool = False):
        self.n, self.fail = n, fail

    def sync(self, cursor):
        for i in range(self.n):
            yield SourceItem(source="fake", external_id=f"jobtest/{i}", title=f"t{i}", body_text="x")
        if self.fail:
            raise RuntimeError("boom")
        yield Checkpoint({"n": self.n})


@pytest.fixture
def db(test_db):
    yield
    with SessionLocal() as s:
        s.execute(text("DELETE FROM source_items WHERE source = 'fake'"))
        s.execute(text("DELETE FROM sync_state WHERE connector = 'fake'"))
        s.execute(text("DELETE FROM jobs WHERE source LIKE 'jobtest%'"))
        s.commit()


def wait_done(job_id: str) -> dict:
    for _ in range(100):
        with SessionLocal() as s:
            job = jobs.get(s, job_id)
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job never finished")


def test_job_runs_in_background_and_records_counts(db):
    job = jobs.start("sync", "jobtest-ok", lambda s, p: jobs.run_connector(s, FakeConnector(3), "default", p))
    assert job["status"] == "running"
    done = wait_done(job["id"])
    assert done["status"] == "succeeded" and done["error"] is None
    assert done["result"]["inserted"] == 3 and done["progress"] == {"items": 3}
    assert done["finished_at"] is not None


def test_failed_job_keeps_error_text(db):
    job = jobs.start("sync", "jobtest-bad", lambda s, p: jobs.run_connector(s, FakeConnector(1, fail=True), "x", p))
    done = wait_done(job["id"])
    assert done["status"] == "failed" and done["error"] == "boom"


def test_one_running_job_per_source(db):
    release = []

    def slow(s, p):
        while not release:
            time.sleep(0.02)

    first = jobs.start("sync", "jobtest-busy", slow)
    with pytest.raises(jobs.JobBusy):
        jobs.start("sync", "jobtest-busy", slow)
    release.append(1)
    wait_done(first["id"])
    again = jobs.start("sync", "jobtest-busy", lambda s, p: {})  # free again once finished
    wait_done(again["id"])


def test_interrupted_jobs_are_marked_failed_on_startup(db):
    with SessionLocal() as s:
        jobs.ensure_table(s)
        s.execute(text("INSERT INTO jobs (id, kind, source) VALUES ('jobtest-stale', 'sync', 'jobtest-stale')"))
        s.commit()
        assert jobs.fail_interrupted(s) >= 1
        assert jobs.get(s, "jobtest-stale")["status"] == "failed"


def test_describe_lists_every_source_with_status(db):
    with SessionLocal() as s:
        rows = sources.describe(s)
    assert [r["id"] for r in rows] == [s["id"] for s in sources.SOURCES]
    for r in rows:
        assert r["status"] in {"not_set_up", "ready", "syncing", "needs_attention"}
        assert isinstance(r["items"], int)
    github = next(r for r in rows if r["id"] == "github")
    assert github["can_sync"] is False and github["coming_soon"] is False
