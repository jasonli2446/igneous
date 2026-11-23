"""The Sources registry, the one path every sync takes (CLI, API, scheduler), and the background job runner.
No network: Google and GitHub are faked, and jobs run fake connectors against the test database."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from typer.testing import CliRunner

from igneous import jobs, sources
from igneous.cli import app as cli
from igneous.connectors import secrets
from igneous.connectors.base import Checkpoint, SourceItem
from igneous.connectors.email_maildir import MaildirConnector
from igneous.connectors.imessage import MessagesConnector
from igneous.db import SessionLocal
from igneous.main import app

KINDS = {"google", "local", "remote", "import"}


def test_registry_shape():
    ids = [s["id"] for s in sources.SOURCES]
    assert len(ids) == len(set(ids))
    assert {"gmail", "gcal", "gcontacts", "drive", "imessage", "maccontacts", "ai_chats", "fs", "email",
            "chatgpt", "takeout", "claude_ai", "github"} <= set(ids)
    for s in sources.SOURCES:
        assert s["kind"] in KINDS
        assert s["name"] and s["reads"] and s["group"] and s["cli"]
        assert isinstance(s["steps"], list) and s["steps"] and all(isinstance(x, str) for x in s["steps"])
        assert ("build" in s) == (s["kind"] != "import")  # every source you sync builds its own connector
    assert sources.SYNCABLE == ("gmail", "gcal", "gcontacts", "drive", "github", "imessage", "maccontacts",
                                "ai_chats", "fs", "notes", "email")


def test_every_local_source_builds_its_connector_with_the_shared_defaults(db, monkeypatch, tmp_path):
    monkeypatch.setattr(sources.settings, "filesystem_roots", str(tmp_path))
    for sid in sources.SYNCABLE:
        entry = sources.BY_ID[sid]
        if entry["kind"] != "local":
            continue
        ((account, make),) = entry["build"](db, None, sources.SyncOptions())
        connector = make()
        assert account == "default" and connector.name == sid
    ((_, make),) = sources.BY_ID["imessage"]["build"](db, None, sources.SyncOptions())
    assert make().since_days == 90  # one first-sync window, whoever asks
    ((_, make),) = sources.BY_ID["email"]["build"](db, None, sources.SyncOptions())
    assert make().mailboxes == list(sources.DEMO_MAILBOXES)


def test_account_sources_need_an_account_and_build_one_connector_per_account(db, monkeypatch):
    with pytest.raises(sources.SetupNeeded, match="connect google"):
        sources.BY_ID["gmail"]["build"](db, None, sources.SyncOptions())
    with pytest.raises(sources.SetupNeeded, match="connect github"):
        sources.BY_ID["github"]["build"](db, None, sources.SyncOptions())
    secrets.save(db, "google", "a@x.com", "{}")
    secrets.save(db, "google", "b@y.com", "{}")
    secrets.save(db, "github", "octo", "github_pat_" + "x" * 30)
    from igneous.connectors import google_auth
    monkeypatch.setattr(google_auth, "load", lambda s, account: (account, None))
    monkeypatch.setattr("igneous.connectors.gmail.build", lambda *a, **k: object())
    pairs = sources.BY_ID["gmail"]["build"](db, None, sources.SyncOptions(all_mail=True))
    assert [name for name, _ in pairs] == ["a@x.com", "b@y.com"]
    gmail = pairs[0][1]()
    assert (gmail.account, gmail.since_days, gmail.all_mail) == ("a@x.com", 90, True)
    ((name, make),) = sources.BY_ID["github"]["build"](db, None, sources.SyncOptions())
    assert name == "octo" and make().account == "octo"


def test_options_a_source_does_not_take_are_refused():
    sources.check_options("email", sources.SyncOptions(mailboxes=("lay-k",)))
    with pytest.raises(ValueError, match="--mailbox doesn't apply to fs"):
        sources.check_options("fs", sources.SyncOptions(mailboxes=("lay-k",)))
    with pytest.raises(ValueError, match="fs has no accounts"):
        sources.check_options("fs", sources.SyncOptions(), account="me@x.com")
    with pytest.raises(ValueError, match="not a source you can sync; choose one of gmail"):
        sources.check_options("chatgpt", sources.SyncOptions())


class Fixed:
    """A connector that stores `n` items, or fails."""

    def __init__(self, name: str, n: int, fail: str | None = None):
        self.name, self.n, self.fail = name, n, fail

    def sync(self, cursor):
        if self.fail:
            raise RuntimeError(self.fail)
        for i in range(self.n):
            yield SourceItem(self.name, f"{self.name}/{i}", f"t{i}", "x")
        yield Checkpoint({"n": self.n})


def test_one_failing_account_does_not_stop_the_others(db, monkeypatch):
    entry = {**sources.BY_ID["github"], "build": lambda s, account, opts: [
        ("bad", lambda: Fixed("github", 0, "GitHub rejected the token")), ("good", lambda: Fixed("github", 2))]}
    monkeypatch.setitem(sources.BY_ID, "github", entry)
    with pytest.raises(sources.SyncFailed, match="bad: GitHub rejected the token") as failure:
        sources.sync("github", db)
    assert failure.value.result["inserted"] == 2 and list(failure.value.result["accounts"]) == ["good"]


def test_the_cli_and_the_api_sync_through_the_registry(db, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sources, "sync", lambda *a, **k: calls.append((a[0], a[2:], k)) or {"accounts": {}})
    assert CliRunner().invoke(cli, ["sync", "imessage", "--since-days", "30"]).exit_code == 0
    assert calls[0][0] == "imessage" and calls[0][1][1] == sources.SyncOptions(since_days=30)
    started = []
    monkeypatch.setattr(jobs, "start", lambda kind, source, runner, account=None: started.append(runner) or {
        **{k: None for k in ("id", "kind", "source", "account", "status", "started_at", "finished_at", "progress",
                             "error", "result")}})
    monkeypatch.setattr(sources, "check_requirements", lambda sid: (True, None))
    assert TestClient(app).post("/sources/imessage/sync").status_code == 200
    started[0](db, lambda *a, **k: None)
    assert calls[1][0] == "imessage" and calls[1][2]["progress"] is not None


def said(result) -> str:
    """CLI output as one line of words: the error box wraps at the terminal's width."""
    return " ".join(result.output.replace("│", " ").split())


def test_igneous_sync_validates_the_source_and_lists_the_choices(test_db):
    result = CliRunner().invoke(cli, ["sync", "contacts"])
    assert result.exit_code == 2 and "choose one of gmail, gcal" in said(result)
    assert "One of: gmail, gcal, gcontacts, drive, github" in said(CliRunner().invoke(cli, ["sync", "--help"]))
    result = CliRunner().invoke(cli, ["sync", "fs", "--mailbox", "lay-k"])
    assert result.exit_code == 2 and "--mailbox doesn't apply to fs" in said(result)


def test_igneous_sync_email_reads_the_demo_mailbox_by_default(db, tmp_path, monkeypatch):
    for box in ("allen-p", "lay-k"):
        (tmp_path / "maildir" / box / "inbox").mkdir(parents=True)
        (tmp_path / "maildir" / box / "inbox" / "1.").write_text(f"Message-ID: <{box}@x>\nSubject: hi\n\nbody\n")
    monkeypatch.setattr(sources.settings, "data_dir", str(tmp_path))
    result = CliRunner().invoke(cli, ["sync", "email"])
    assert result.exit_code == 0, result.output
    assert db.execute(text("SELECT external_id FROM source_items")).scalars().all() == ["allen-p/inbox/1."]
    assert MaildirConnector  # the default is DEMO_MAILBOXES, shared with `make demo` and the Sources page


def test_get_sync_is_gone(test_db):
    assert TestClient(app).get("/sync").status_code == 404


def test_igneous_connect_github_reads_the_token_from_stdin(db, monkeypatch):
    from igneous.connectors import github

    seen = []
    monkeypatch.setattr(github, "account_for", lambda token: seen.append(token) or "octo")
    result = CliRunner().invoke(cli, ["connect", "github"], input="github_pat_" + "t" * 30 + "\n")
    assert result.exit_code == 0 and "connected octo" in result.output
    assert secrets.accounts(db, "github") == ["octo"] and secrets.load(db, "github", "octo")[1] == "github_pat_" + "t" * 30
    monkeypatch.setattr(github, "account_for", lambda token: (_ for _ in ()).throw(github.GithubError("rejected")))
    assert CliRunner().invoke(cli, ["connect", "github"], input="x").exit_code == 2


def test_igneous_import_ai_export(db, tmp_path):
    export = tmp_path / "conversations.json"
    export.write_text(json.dumps([{"uuid": "u1", "name": "Plan", "chat_messages": [
        {"sender": "human", "text": "hello", "created_at": "2025-04-01T10:00:00Z"}]}]))
    result = CliRunner().invoke(cli, ["import", "ai-export", str(export)])
    assert result.exit_code == 0 and "claude_ai: 1 conversations; 1 new" in result.output
    assert db.execute(text("SELECT external_id FROM source_items")).scalars().all() == ["claude_ai/u1/2025-04-01"]


def test_requirements_report_missing_client_secret_and_blocked_messages(monkeypatch, tmp_path):
    monkeypatch.setattr(sources.settings, "data_dir", str(tmp_path))
    ok, message = sources.check_requirements("gmail")
    assert not ok and "client_secret.json" in message
    (tmp_path / "google").mkdir()
    (tmp_path / "google" / "client_secret.json").write_text("{}")
    assert sources.check_requirements("gmail") == (True, None)

    monkeypatch.setattr(sources.sys, "platform", "linux")
    assert sources.check_requirements("notes") == (False, "Apple Notes is read on macOS only.")
    monkeypatch.setattr(sources.sys, "platform", "darwin")
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


def wait_done(job_id: str) -> dict:
    for _ in range(100):
        with SessionLocal() as s:
            job = jobs.get(s, job_id)
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job never finished")


def test_job_runs_in_background_and_records_counts(committed_db):
    job = jobs.start("sync", "jobtest-ok", lambda s, p: jobs.run_connector(s, FakeConnector(3), "default", p))
    assert job["status"] == "running"
    done = wait_done(job["id"])
    assert done["status"] == "succeeded" and done["error"] is None
    assert done["result"]["inserted"] == 3 and done["progress"] == {"items": 3}
    assert done["finished_at"] is not None


def test_failed_job_keeps_error_text_and_what_did_sync(committed_db):
    job = jobs.start("sync", "jobtest-bad", lambda s, p: jobs.run_connector(s, FakeConnector(1, fail=True), "x", p))
    done = wait_done(job["id"])
    assert done["status"] == "failed" and done["error"] == "boom" and done["result"] is None

    def partly(s, p):
        raise sources.SyncFailed("b@y.com: token expired", {"inserted": 2, "accounts": {"a@x.com": {}}})
    done = wait_done(jobs.start("sync", "jobtest-partly", partly)["id"])
    assert done["error"] == "b@y.com: token expired" and done["result"]["inserted"] == 2


def test_one_running_job_per_source(committed_db):
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


def test_interrupted_jobs_are_marked_failed_on_startup(committed_db):
    with SessionLocal() as s:
        s.execute(text("INSERT INTO jobs (id, kind, source) VALUES ('jobtest-stale', 'sync', 'jobtest-stale')"))
        s.commit()
        assert jobs.fail_interrupted(s) >= 1
        assert jobs.get(s, "jobtest-stale")["status"] == "failed"


def test_describe_lists_every_source_with_status(committed_db):
    with SessionLocal() as s:
        rows = sources.describe(s)
    assert [r["id"] for r in rows] == [s["id"] for s in sources.SOURCES]
    for r in rows:
        assert r["status"] in {"not_set_up", "ready", "syncing", "needs_attention"}
        assert isinstance(r["items"], int) and "build" not in r
    github = next(r for r in rows if r["id"] == "github")
    assert github["can_sync"] is False


def test_a_saved_github_token_is_an_account(db):
    secrets.save(db, "github", "octo", "github_pat_" + "x" * 30)
    github = next(r for r in sources.describe(db) if r["id"] == "github")
    assert [a["account"] for a in github["accounts"]] == ["octo"] and github["can_sync"] is True
    assert MessagesConnector  # imported for the build test above


def test_accounts_can_be_disconnected_from_the_page_and_the_cli(db):
    secrets.save(db, "google", "me@x.com", "{}")
    secrets.save(db, "github", "octo", "github_pat_" + "x" * 30)
    client = TestClient(app)
    assert client.delete("/sources/google/accounts/me@x.com").json() == {"removed": "me@x.com"}
    assert client.delete("/sources/google/accounts/me@x.com").status_code == 404
    rows = {r["id"]: r for r in sources.describe(db)}
    assert rows["gmail"]["accounts"] == [] and rows["gmail"]["status"] == "not_set_up"
    result = CliRunner().invoke(cli, ["disconnect", "github", "octo"])
    assert result.exit_code == 0 and secrets.accounts(db, "github") == []
    assert CliRunner().invoke(cli, ["disconnect", "github", "octo"]).exit_code == 2


def test_connecting_google_without_a_client_file_comes_back_to_the_page(monkeypatch, tmp_path):
    monkeypatch.setattr(sources.settings, "data_dir", str(tmp_path))
    response = TestClient(app).get("/sources/google/connect", follow_redirects=False)
    assert response.status_code == 307
    location = response.headers["location"]
    assert location.startswith(f"{sources.settings.web_url}/settings?connect_error=") and "client_secret.json" in location


def test_the_page_describes_what_the_build_does():
    """The copy names the same numbers the connectors use, so the two can't disagree."""
    imessage, email = sources.BY_ID["imessage"], sources.BY_ID["email"]
    assert f"reads the last {sources.FIRST_SYNC_DAYS['imessage']} days" in " ".join(imessage["steps"])
    assert all(box in email["reads"] for box in sources.DEMO_MAILBOXES)
    assert "Files and Folders" in " ".join(sources.BY_ID["fs"]["steps"])
    assert "--path" not in sources.BY_ID["fs"]["cli"].split(" (")[0]  # the default command reads the configured folders
