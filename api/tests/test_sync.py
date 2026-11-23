"""The sync runner (connectors/base.py): what a sync stores, what it deletes, and when it commits."""
import pytest
from fakes import FakeDrive
from sqlalchemy import text

from igneous.connectors.base import Checkpoint, CompletedScan, SourceItem, Tombstone, run_sync
from igneous.connectors.drive import DriveConnector


class Events:
    """A connector that replays `events`; an exception among them is raised when reached."""

    def __init__(self, name: str, events: list):
        self.name, self.events = name, events

    def sync(self, cursor):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event


def store(s, source: str, *ids: str) -> None:
    run_sync(s, Events(source, [SourceItem(source, i, i, "body") for i in ids]))


def stored(s, source: str) -> set[str]:
    return set(s.execute(text("SELECT external_id FROM source_items WHERE source = :s"), {"s": source}).scalars())


def rebuild_marked(s) -> bool:
    """The deletion is recorded for the stages that skip work while nothing changed (graph/dirty.py)."""
    return s.execute(text("SELECT EXISTS (SELECT 1 FROM pipeline_changes WHERE reason = 'source records deleted')")
                     ).scalar_one()


def test_a_completed_scan_deletes_only_unobserved_items_inside_its_scopes(db):
    store(db, "fs", "a/1", "a/2", "a/keep/x", "ab/1", "b/1")
    scan = CompletedScan({}, "fs", frozenset({"a/1"}), scopes=("a",), kept=("a/keep",))
    assert run_sync(db, Events("fs", [scan]))["deleted"] == 1
    # "ab/1" shares the scope's spelling but not its path; "a/keep" could not be listed; "b" was not scanned.
    assert stored(db, "fs") == {"a/1", "a/keep/x", "ab/1", "b/1"}


def test_a_scan_without_scopes_reconciles_the_whole_source_but_never_on_an_empty_listing(db):
    store(db, "notes", "1", "2")
    run_sync(db, Events("notes", [CompletedScan({}, "notes", frozenset())]))
    assert stored(db, "notes") == {"1", "2"}
    run_sync(db, Events("notes", [CompletedScan({}, "notes", frozenset({"2"}))]))
    assert stored(db, "notes") == {"2"}


def test_a_prefix_tombstone_removes_one_scope(db):
    store(db, "github", "me/repo/issue/1", "me/repo", "me/repo2/issue/1")
    run_sync(db, Events("github", [Tombstone("github", "me/repo", prefix=True)]))
    assert stored(db, "github") == {"me/repo2/issue/1"}


def test_a_deletion_commits_with_its_rebuild_marker(db):
    store(db, "drive", "me/1", "me/2")
    sync = Events("drive", [Tombstone("drive", "me/1"), Checkpoint({"page": 2}), RuntimeError("network down")])
    with pytest.raises(RuntimeError):
        run_sync(db, sync)
    db.rollback()  # back to the last commit: the checkpoint
    assert stored(db, "drive") == {"me/2"}
    assert rebuild_marked(db)


def test_a_limited_sync_that_reaches_its_completed_scan_still_reconciles(db):
    store(db, "gcontacts", "me/people/1", "me/people/2")
    scan = CompletedScan({"synced": 1}, "gcontacts", frozenset({"me/people/1"}), scopes=("me",))
    stats = run_sync(db, Events("gcontacts", [SourceItem("gcontacts", "me/people/1", "One", ""), scan]), limit=50)
    assert stats["deleted"] == 1 and stored(db, "gcontacts") == {"me/people/1"} and rebuild_marked(db)


def test_backfilling_a_second_drive_account_keeps_the_first_accounts_files(db):
    doc = "application/vnd.google-apps.document"
    first = FakeDrive([{"id": "f1", "name": "Plan", "mimeType": doc}, {"id": "f2", "name": "Notes", "mimeType": doc}],
                      {"f1": b"plan", "f2": b"notes"})
    second = FakeDrive([{"id": "g1", "name": "Budget", "mimeType": doc}], {"g1": b"budget"})
    run_sync(db, DriveConnector(None, "a@x.com", first), account="a@x.com")
    run_sync(db, DriveConnector(None, "b@y.com", second), account="b@y.com")
    assert stored(db, "drive") == {"a@x.com/f1", "a@x.com/f2", "b@y.com/g1"}
    # The first account's own backfill still deletes its own missing files.
    first.items.pop()
    db.execute(text("DELETE FROM sync_state WHERE account = 'a@x.com'"))
    run_sync(db, DriveConnector(None, "a@x.com", first), account="a@x.com")
    assert stored(db, "drive") == {"a@x.com/f1", "b@y.com/g1"}
