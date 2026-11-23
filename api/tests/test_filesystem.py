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
    # Nothing changed: no file is read again. The checkout's config is read on every scan (an unchanged item
    # stores nothing), so its item comes again.
    assert [e.title for e in emitted(connector, events[-1].cursor) if isinstance(e, SourceItem)] == ["igneous"]


def test_a_checkout_sits_in_every_folder_above_it(db, tmp_path):
    """Its folders run from the scanned folder down to the checkout, as a file's do; a checkout that is the scanned
    folder itself is in that folder only."""
    nested = tmp_path / "docs" / "Projects" / "tidepool"
    write(nested / ".git" / "config", '[remote "origin"]\n\turl = git@github.com:a/tidepool.git\n')
    write(nested / "README.md", "# Tidepool")
    key = _root_key(tmp_path / "docs")
    items = {e.external_id: e for e in emitted(FilesystemConnector(tmp_path / "docs")) if isinstance(e, SourceItem)}
    repo, readme = items[f"{key}/.repos/Projects/tidepool"], items[f"{key}/Projects/tidepool/README.md"]
    assert repo.raw_meta["folders"] == readme.raw_meta["folders"] == [key, f"{key}/Projects", f"{key}/Projects/tidepool"]
    assert repo.raw_meta["path"] == f"{key}/Projects/tidepool"

    checkout = tmp_path / "tidepool"
    write(checkout / ".git" / "config", '[remote "origin"]\n\turl = git@github.com:a/tidepool.git\n')
    (own,) = [e for e in emitted(FilesystemConnector(checkout)) if isinstance(e, SourceItem)]
    assert own.raw_meta["folders"] == [_root_key(checkout)]  # not a folder named "."

    # A checkout's item stored with other folders is stored again with these on the next scan; then it is unchanged.
    run_sync(db, FilesystemConnector(tmp_path / "docs"))
    db.execute(text("UPDATE source_items SET raw_meta = jsonb_set(raw_meta, '{folders}', CAST(:f AS jsonb)), "
                    "content_hash = 'earlier' WHERE external_id = :i"),
               {"f": f'["{key}", "{key}/Projects/tidepool"]', "i": repo.external_id})
    assert run_sync(db, FilesystemConnector(tmp_path / "docs"))["updated"] == 1
    assert run_sync(db, FilesystemConnector(tmp_path / "docs"))["updated"] == 0


def test_a_checkouts_remote_is_origins(tmp_path):
    from igneous.connectors.filesystem import _git_remote

    token = "ghp_" + "T" * 36
    config = write(tmp_path / "repo" / ".git" / "config", "\n".join([
        "[core]", "\trepositoryformatversion = 0",
        '[submodule "vendor/lib"]', "\turl = https://example.com/lib.git",
        '[remote "upstream"]', "\turl = https://github.com/them/repo.git",
        '[remote "origin"]', f"\turl=https://me:{token}@github.com/me/repo.git"]))
    assert _git_remote(tmp_path / "repo") == "https://github.com/me/repo.git"  # origin's, and no credentials
    config.write_text('[remote "upstream"]\n\turl = https://github.com/them/repo.git\n')
    assert _git_remote(tmp_path / "repo") == "https://github.com/them/repo.git"
    config.write_text('[submodule "lib"]\n\turl = https://example.com/lib.git\n')
    assert _git_remote(tmp_path / "repo") is None  # a submodule's url is not the checkout's remote


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
    assert stats["inserted"] == stats["updated"] == stats["unchanged"] == 0  # the cursor kept every entry


def test_a_configured_folder_inside_a_path_keeps_its_ids(db, tmp_path):
    documents, notes = tmp_path / "Documents", tmp_path / "Documents" / "notes"
    write(notes / "n.md")
    write(documents / "d.md")
    run_sync(db, FilesystemConnector([notes]))  # notes is the configured folder
    # --path of a folder holding it: notes is read as itself, so its file is not stored a second time.
    stats = run_sync(db, FilesystemConnector([documents], anchors=[notes]))
    assert stored(db) == {f"{_root_key(notes)}/n.md", f"{_root_key(documents)}/d.md"} and stats["deleted"] == 0


def test_ids_an_earlier_scan_gave_under_a_folders_own_key_are_replaced(db, tmp_path, monkeypatch):
    """A folder synced on its own (`--path`, or a root nested in another) had its files under its own key. A scan
    of the folder holding it stores them under that folder's key and removes the earlier ids, except below a folder
    it could not list."""
    documents, notes = tmp_path / "Documents", tmp_path / "Documents" / "notes"
    write(notes / "n.md")
    write(notes / "locked" / "l.md")
    write(documents / "d.md")
    run_sync(db, FilesystemConnector([notes]))
    old, new = _root_key(notes), _root_key(documents)
    assert stored(db) == {f"{old}/n.md", f"{old}/locked/l.md"}

    blocked = str(notes / "locked")
    scandir = os.scandir

    def no_locked(path):
        if str(path) == blocked:
            raise PermissionError("Operation not permitted")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", no_locked)
    run_sync(db, FilesystemConnector([documents]))
    assert stored(db) == {f"{new}/d.md", f"{new}/notes/n.md", f"{old}/locked/l.md"}  # l.md: not listed, kept
    monkeypatch.setattr(os, "scandir", scandir)
    run_sync(db, FilesystemConnector([documents]))
    assert stored(db) == {f"{new}/d.md", f"{new}/notes/n.md", f"{new}/notes/locked/l.md"}
    stats = run_sync(db, FilesystemConnector([documents]))
    assert stats["inserted"] == stats["updated"] == stats["deleted"] == 0


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


def test_only_readable_documents_within_the_size_cap_are_opened(tmp_path, monkeypatch):
    from igneous.connectors import filesystem

    monkeypatch.setattr(filesystem, "MAX_DOCUMENT_BYTES", 10)
    write(tmp_path / "small.md", "# short")
    write(tmp_path / "big.txt", "x" * 11)
    (tmp_path / "movie.mp4").write_bytes(b"\x00" * 64)
    opened = []
    read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda p: opened.append(p.name) or read_bytes(p))
    items = {e.title: e for e in emitted(FilesystemConnector(tmp_path)) if isinstance(e, SourceItem)}
    assert opened == ["small.md"]  # neither the video nor the oversized text is read at all
    assert items["big.txt"].body_text is None and items["big.txt"].raw_meta["parse_status"] == "too_large"
    assert items["movie.mp4"].raw_meta["parse_status"] == "unsupported" and items["movie.mp4"].raw_meta["size"] == 64
    assert items["small.md"].body_text == "# short"


def test_system_files_and_folders_are_skipped(tmp_path):
    for name in (".DS_Store", "._notes.md", "Thumbs.db", "desktop.ini", "~$report.docx", ".~lock.sheet.ods#"):
        write(tmp_path / name)
    write(tmp_path / ".Trashes" / "old.txt")
    write(tmp_path / "notes.md")
    items = [e.title for e in emitted(FilesystemConnector(tmp_path)) if isinstance(e, SourceItem)]
    assert items == ["notes.md"]


def test_checkpoints_come_by_time_not_by_count(tmp_path, monkeypatch):
    from igneous.connectors import base

    for i in range(5):
        write(tmp_path / f"{i}.txt")
    events = emitted(FilesystemConnector(tmp_path))
    assert [type(e).__name__ for e in events].count("Checkpoint") == 0  # a quick scan writes its cursor once
    monkeypatch.setattr(base, "CHECKPOINT_SECONDS", 0)
    events = emitted(FilesystemConnector(tmp_path))
    assert [type(e).__name__ for e in events].count("Checkpoint") == 5


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


@pytest.mark.parametrize("relative", ["Projects/tidepool", "."])
def test_a_repository_anchor_contains_its_checkout_only(db, tmp_path, relative):
    from igneous.graph import spine

    root = tmp_path / ("Documents" if relative != "." else "tidepool")
    checkout = root / relative
    write(checkout / ".git" / "config", '[remote "origin"]\n\turl = git@github.com:a/tidepool.git\n')
    write(checkout / "README.md", "# Tidepool")
    write(root / "notes.md", "Unrelated notes")
    run_sync(db, FilesystemConnector(root))
    spine.run(db)
    key = _root_key(root)
    expected = f"fs:{key}" + (f"/{relative}" if relative != "." else "")
    assert db.execute(text("""
        SELECT child.key FROM edges e JOIN nodes parent ON parent.id = e.src JOIN nodes child ON child.id = e.dst
        WHERE parent.key = 'repo:tidepool' AND e.type = 'CONTAINS'
    """)).scalars().all() == [expected]


@pytest.mark.parametrize("nested", [False, True])
def test_the_configured_anchor_name_travels_without_its_absolute_path(db, tmp_path, nested):
    import json

    from igneous.graph import spine

    root = tmp_path / "Documents"
    child = root / "Projects"
    write(child / "plan.md", "# Plan")
    connector = FilesystemConnector(child if nested else root, anchors=[root])
    (item,) = [e for e in emitted(connector) if isinstance(e, SourceItem)]
    assert item.raw_meta["root_name"] == "Documents"
    assert str(tmp_path) not in json.dumps(item.raw_meta)
    run_sync(db, connector)
    spine.run(db)
    assert db.execute(text("SELECT name FROM nodes WHERE key = :k"), {"k": f"fs:{_root_key(root)}"}).scalar_one() \
        == "Documents"


def test_an_unchanged_file_is_refreshed_once_from_a_cursor_without_folder_names(db, tmp_path):
    from igneous.connectors.base import save_cursor, store
    from igneous.graph import spine

    root = tmp_path / "Documents"
    file = write(root / "notes.md", "Notes")
    connector = FilesystemConnector(root)
    (item,) = [e for e in emitted(connector) if isinstance(e, SourceItem)]
    item.raw_meta.pop("root_name", None)
    store(db, item)
    spine.run(db)
    assert db.execute(text("SELECT name FROM nodes WHERE type = 'Folder'")).scalar_one() == _root_key(root)
    stat = file.stat()
    cursor = {"files": {item.external_id: [stat.st_mtime_ns, stat.st_size]}}
    save_cursor(db, "fs", "default", cursor)
    assert run_sync(db, connector)["updated"] == 1
    assert spine.run(db) == 1
    assert db.execute(text("SELECT name FROM nodes WHERE type = 'Folder'")).scalar_one() == "Documents"
    assert run_sync(db, connector)["unchanged"] == 0  # the current cursor never reopens this file
    assert spine.run(db) == 0
