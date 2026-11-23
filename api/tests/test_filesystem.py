"""Local files (connectors/filesystem.py): what a scan stores, which ids it gives, and what it may delete."""
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from igneous.connectors.base import CompletedScan, SourceItem, run_sync
from igneous.connectors.filesystem import FilesystemConnector, _root_key


def emitted(connector, cursor=None) -> list:
    return list(connector.sync(cursor or {}))


def stored(s) -> set[str]:
    return set(s.execute(text("SELECT external_id FROM source_items WHERE source = 'fs'")).scalars())


def write(path: Path, content: str = "text") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_walks_sorted_parseable_files_and_emits_hierarchy(tmp_path):
    write(tmp_path / "z-last.txt", "last")
    write(tmp_path / "projects" / "igneous" / "plan.md", "# Plan\nship")
    write(tmp_path / "node_modules" / "ignored.txt", "ignore")
    write(tmp_path / ".git" / "config", "ignore")
    (tmp_path / "photo.jpg").write_bytes(b"not an image parser input")

    events = emitted(FilesystemConnector(tmp_path))
    items = [event for event in events if isinstance(event, SourceItem)]
    key = _root_key(tmp_path)
    assert [item.external_id for item in items] == [f"{key}/photo.jpg", f"{key}/projects/igneous/plan.md",
                                                    f"{key}/z-last.txt"]
    assert items[0].body_text is None and items[0].raw_meta["parse_status"] == "unsupported"
    plan = items[1]
    assert plan.body_text == "# Plan\nship" and plan.mime == "text/markdown"
    assert plan.raw_meta["path"] == f"{key}/projects/igneous/plan.md"
    assert plan.raw_meta["folders"] == [key, f"{key}/projects", f"{key}/projects/igneous"]
    assert plan.raw_meta["parser"] == "text" and plan.raw_meta["boundaries"] == []
    assert isinstance(events[-1], CompletedScan) and events[-1].scopes == (key,)


def test_namespaces_multiple_roots_and_rejects_missing_ones(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    write(first / "same.txt", "one")
    write(second / "same.txt", "two")
    initial = [e for e in emitted(FilesystemConnector([second, first])) if isinstance(e, SourceItem)]
    assert [item.external_id for item in initial] == [f"{_root_key(first)}/same.txt", f"{_root_key(second)}/same.txt"]
    with pytest.raises(ValueError, match="must be directories"):
        FilesystemConnector(tmp_path / "does-not-exist")


def test_secret_files_are_never_stored_and_a_checkout_is_one_item(tmp_path):
    (tmp_path / "binary.bin").write_bytes(b"\x00binary")
    write(tmp_path / ".env", "PASSWORD=not-stored")
    write(tmp_path / "igneous" / ".git" / "config", '[remote "origin"]\n\turl = git@github.com:a/igneous.git\n')
    connector = FilesystemConnector(tmp_path)
    events = emitted(connector)
    items = [event for event in events if isinstance(event, SourceItem)]
    assert {item.title for item in items} == {"binary.bin", "igneous"}
    assert next(item for item in items if item.title == "binary.bin").body_text is None
    repo = next(item for item in items if item.title == "igneous")
    assert repo.raw_meta["kind"] == "repo" and repo.raw_meta["remote"] == "git@github.com:a/igneous.git"
    assert repo.external_id == f"{_root_key(tmp_path)}/.repos/igneous"
    assert not [e for e in emitted(connector, events[-1].cursor) if isinstance(e, SourceItem)]  # nothing changed


def test_syncing_one_folder_reconciles_only_that_folder_and_keeps_the_configured_ids(db, tmp_path):
    root = tmp_path / "Documents"
    write(root / "a.txt")
    write(root / "notes" / "old.md")
    run_sync(db, FilesystemConnector([root]))
    key = _root_key(root)
    assert stored(db) == {f"{key}/a.txt", f"{key}/notes/old.md"}

    (root / "notes" / "old.md").unlink()
    write(root / "notes" / "new.md")
    stats = run_sync(db, FilesystemConnector([root / "notes"], anchors=[root]))
    # The rest of Documents stays; the new note has the id a scan of all of Documents gives it.
    assert stored(db) == {f"{key}/a.txt", f"{key}/notes/new.md"} and stats["deleted"] == 1
    stats = run_sync(db, FilesystemConnector([root]))
    assert stats == {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0}  # the cursor kept every entry


def test_a_folder_inside_another_root_is_scanned_once(tmp_path):
    root = tmp_path / "Documents"
    write(root / "notes" / "n.md")
    items = [e for e in emitted(FilesystemConnector([root, root / "notes"])) if isinstance(e, SourceItem)]
    assert [item.external_id for item in items] == [f"{_root_key(root)}/notes/n.md"]


def test_an_unreadable_folder_keeps_its_items_and_the_rest_reconciles(db, tmp_path, monkeypatch):
    root = tmp_path / "home"
    write(root / "Desktop" / "d.txt")
    write(root / "Documents" / "gone.txt")
    write(root / "Documents" / "kept.txt")
    run_sync(db, FilesystemConnector([root]))
    key = _root_key(root)

    (root / "Documents" / "gone.txt").unlink()
    (root / "Desktop" / "d.txt").unlink()
    blocked = str(root / "Desktop")
    scandir = os.scandir

    def no_desktop(path):
        if str(path) == blocked:
            raise PermissionError("Operation not permitted")  # macOS without Files and Folders access
        return scandir(path)

    monkeypatch.setattr(os, "scandir", no_desktop)
    events = emitted(FilesystemConnector([root]))
    assert events[-1].kept == (f"{key}/Desktop", f"{key}/.repos/Desktop")
    run_sync(db, FilesystemConnector([root]))
    assert stored(db) == {f"{key}/Desktop/d.txt", f"{key}/Documents/kept.txt"}


def test_a_file_that_cannot_be_read_keeps_its_item_and_is_read_again(db, tmp_path, monkeypatch):
    root = tmp_path / "docs"
    locked = write(root / "locked.txt", "first")
    run_sync(db, FilesystemConnector([root]))
    write(locked, "second version")
    read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda p: (_ for _ in ()).throw(PermissionError()) if p == locked
                        else read_bytes(p))
    run_sync(db, FilesystemConnector([root]))
    assert stored(db) == {f"{_root_key(root)}/locked.txt"}
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    run_sync(db, FilesystemConnector([root]))
    body = db.execute(text("SELECT body_text FROM source_items WHERE source = 'fs'")).scalar_one()
    assert body == "second version"
