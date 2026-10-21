"""Privacy-preserving full-scan connector for locally mounted filesystems."""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from igneous.connectors.base import Checkpoint, CompletedScan, SourceItem
from igneous.connectors.redact import redact
from igneous.parsers import parse_document

CHECKPOINT_EVERY = 100
IGNORED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".tox", ".venv", "__pycache__", "node_modules", "vendor",
    "dist", "build", ".next", ".pytest_cache", ".mypy_cache",
    # Agent worktrees are full copies of a repo (every file would be imported once per worktree), and the
    # .claude folder holds session state, not documents.
    ".claude", "worktrees",
})
# These files are never read or represented, even as metadata. They commonly hold credentials.
SECRET_NAMES = re.compile(r"^(?:\.env(?:\..*)?|id_.*|credentials.*)$", re.I)
SECRET_SUFFIXES = frozenset({".key", ".pem"})


def _root_key(root: Path) -> str:
    return hashlib.sha256(str(root).encode()).hexdigest()[:12]


def _is_secret(path: Path) -> bool:
    return bool(SECRET_NAMES.match(path.name)) or path.suffix.lower() in SECRET_SUFFIXES


def _fingerprint(stat: os.stat_result) -> list[int]:
    return [stat.st_mtime_ns, stat.st_size]


def _git_remote(directory: Path) -> str | None:
    """Read only the configured origin URL; worktree gitdirs are supported too."""
    git = directory / ".git"
    try:
        if git.is_file():
            target = git.read_text(errors="replace").strip().removeprefix("gitdir:").strip()
            git = (directory / target).resolve()
        config = git / "config"
        for line in config.read_text(errors="replace").splitlines():
            if line.strip().startswith("url ="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


class FilesystemConnector:
    """Index configured roots, with document text only for parseable knowledge documents.

    Every non-secret file has a metadata item so location queries can find it.  The cursor
    holds mtime+size fingerprints, letting a complete reconciliation walk avoid reopening
    unchanged bodies while still noticing deletions.
    """

    name = "fs"

    def __init__(self, roots: Path | Iterable[Path]):
        roots = (roots,) if isinstance(roots, Path) else tuple(roots)
        self.roots = tuple(sorted({Path(root).expanduser().resolve() for root in roots}, key=str))
        if not self.roots:
            raise ValueError("at least one filesystem root is required")
        if missing := [str(root) for root in self.roots if not root.is_dir()]:
            raise ValueError(f"filesystem roots must be directories: {', '.join(missing)}")

    def _files(self, directory: Path) -> Iterator[Path]:
        for path in sorted(directory.iterdir(), key=lambda entry: entry.name):
            if path.is_symlink() or _is_secret(path):
                continue
            if path.is_dir():
                if path.name not in IGNORED_DIRECTORIES:
                    yield from self._files(path)
            else:
                yield path

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint | CompletedScan]:
        previous = cursor.get("files", {}) if isinstance(cursor.get("files", {}), dict) else {}
        observed: set[str] = set()
        next_cursor: dict[str, list[int]] = {}
        processed, complete = 0, True
        for root in self.roots:
            root_key = _root_key(root)
            for path in self._files(root):
                try:
                    stat = path.stat()
                except OSError:
                    complete = False
                    continue
                relative_path = path.relative_to(root).as_posix()
                external_id = f"{root_key}/{relative_path}"
                observed.add(external_id)
                fingerprint = _fingerprint(stat)
                next_cursor[external_id] = fingerprint
                if previous.get(external_id) == fingerprint:
                    continue
                folders = external_id.split("/")[:-1]
                hierarchy = ["/".join(folders[:index]) for index in range(1, len(folders) + 1)]
                meta = {"path": external_id, "folders": hierarchy, "size": stat.st_size,
                        "model_opt_in": False}
                body, mime, parser = None, None, None
                try:
                    data = path.read_bytes()
                except OSError:
                    complete = False
                    continue
                parsed = parse_document(data, path.name)
                if parsed.status == "ok":
                    body, redactions = redact(parsed.text)
                    mime, parser = parsed.mime, parsed.parser
                    meta.update({"parser": parser, "boundaries": parsed.boundaries, "redactions": redactions})
                else:
                    meta.update({"parser": None, "parse_status": parsed.status})
                yield SourceItem(
                    source=self.name, external_id=external_id, title=path.name, body_text=body,
                    occurred_at=datetime.fromtimestamp(stat.st_mtime, UTC), mime=mime, raw_meta=meta,
                    content_hash=hashlib.sha256(
                        data + b"\0" + external_id.encode() + b"\0" + str(fingerprint).encode()).hexdigest(),
                )
                processed += 1
                if processed % CHECKPOINT_EVERY == 0:
                    yield Checkpoint({"files": next_cursor})
            # A repo is one evidence-bearing item, not an import of every source file in .git.
            for directory, directories, filenames in os.walk(root):
                has_git = ".git" in filenames or ".git" in directories
                # Do not descend into repositories' own metadata or ordinary build trees.
                directories[:] = [d for d in directories if d not in IGNORED_DIRECTORIES]
                if not has_git:
                    continue
                directory_path = Path(directory)
                remote = _git_remote(directory_path)
                if remote is None:
                    continue
                relative = directory_path.relative_to(root).as_posix()
                repo_id = f"{root_key}/.repos/{relative}"
                observed.add(repo_id)
                try:
                    git = directory_path / ".git"
                    stat = git.stat() if git.is_file() else (git / "config").stat()
                except OSError:
                    complete = False
                    continue
                fingerprint = _fingerprint(stat)
                next_cursor[repo_id] = fingerprint
                if previous.get(repo_id) == fingerprint:
                    continue
                name = directory_path.name
                yield SourceItem(source=self.name, external_id=repo_id, title=name, body_text=None,
                                 occurred_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                                 raw_meta={"path": f"{root_key}/{relative}",
                                           "folders": [root_key, f"{root_key}/{relative}"],
                                           "kind": "repo", "repo": name, "remote": remote,
                                           "model_opt_in": False},
                                 content_hash=hashlib.sha256((repo_id + str(fingerprint) + remote).encode()).hexdigest())
        if complete:
            yield CompletedScan({"files": next_cursor}, self.name, frozenset(observed))
