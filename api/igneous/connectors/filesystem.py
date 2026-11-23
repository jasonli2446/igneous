"""Folders on this computer: a metadata item for every non-secret file, the text of readable documents, and one
item per git checkout. Only a document the parsers read and no larger than MAX_DOCUMENT_BYTES is ever opened; system
files (.DS_Store, Thumbs.db, lock files) and folders (trash, indexes) are skipped.

An item's id is `<anchor key>/<path below the anchor>`. The anchor is the outermost configured folder that contains
the file, or, for a file in no configured folder, the outermost scanned one; its key is a hash of its path, so ids
name no home directory and a file keeps one id whichever folder's scan reaches it. A completed scan reconciles only
the folders it scanned, and with them the ids an earlier scan gave their files under a folder's own key (a nested
root, or `--path` inside a configured folder), so a file is never stored twice.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from igneous.connectors.base import Checkpoint, CompletedScan, Every, SourceItem
from igneous.connectors.redact import url_without_credentials
from igneous.parsers import MAX_DOCUMENT_BYTES, parse_document, supported

IGNORED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".tox", ".venv", "__pycache__", "node_modules", "vendor",
    "dist", "build", ".next", ".pytest_cache", ".mypy_cache",
    # Agent worktrees are full copies of a repo (every file would be imported once per worktree), and the
    # .claude folder holds session state, not documents.
    ".claude", "worktrees",
    # What operating systems keep beside your files: indexes, trash, version stores.
    ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems", ".DocumentRevisions-V100",
    "$RECYCLE.BIN", "System Volume Information",
})
# These files are never read or represented, even as metadata. They commonly hold credentials.
SECRET_NAMES = re.compile(r"^(?:\.env(?:\..*)?|id_.*|credentials.*)$", re.I)
SECRET_SUFFIXES = frozenset({".key", ".pem"})
# Nor are these: folder settings, thumbnails and lock files the system or an editor writes, not your documents.
NOISE_NAMES = re.compile(r"^(?:\.DS_Store|\._.*|Thumbs\.db|desktop\.ini|\.localized|Icon\r|~\$.*|\.~lock\..*#)$",
                         re.I)


def _root_key(root: Path) -> str:
    return hashlib.sha256(str(root).encode()).hexdigest()[:12]


def _is_secret(name: str) -> bool:
    return bool(SECRET_NAMES.match(name)) or Path(name).suffix.lower() in SECRET_SUFFIXES


def _fingerprint(stat: os.stat_result) -> list[int]:
    return [stat.st_mtime_ns, stat.st_size]


def _depth(path: Path) -> tuple[int, str]:
    return len(path.parts), str(path)


def _below(path: Path, anchor: Path) -> str:
    below = path.relative_to(anchor).as_posix()
    return "" if below == "." else below


def _in(external_id: str, scopes: Iterable[str]) -> bool:
    return any(external_id == s or external_id.startswith(s + "/") for s in scopes)


def _git_remote(directory: Path) -> str | None:
    """The first remote URL in a checkout's git config, without the user name and token an HTTPS remote may
    embed. A worktree's `.git` file points at its gitdir, whose `commondir` holds the shared config."""
    git = directory / ".git"
    try:
        if git.is_file():
            git = (directory / git.read_text(errors="replace").strip().removeprefix("gitdir:").strip()).resolve()
            if (git / "commondir").is_file():
                git = (git / (git / "commondir").read_text(errors="replace").strip()).resolve()
        for line in (git / "config").read_text(errors="replace").splitlines():
            if line.strip().startswith("url ="):
                return url_without_credentials(line.split("=", 1)[1])
    except OSError:
        return None
    return None


# (id, the item to store or None when there is nothing new, fingerprint to record or None to read it again)
_Seen = tuple[str, SourceItem | None, list[int] | None]


@dataclass(frozen=True)
class _Root:
    """A scanned folder and where it sits below its anchor: ids are `key/below/<path in the folder>`."""
    path: Path
    key: str
    below: str  # "" when the folder is its own anchor

    def id(self, relative: str) -> str:
        """The id of `relative` ("." for the folder itself) below this folder, without the key."""
        if relative == ".":
            return self.below or "."
        return f"{self.below}/{relative}" if self.below else relative

    @property
    def scopes(self) -> tuple[str, ...]:
        if not self.below:
            return (self.key,)
        return (f"{self.key}/{self.below}", f"{self.key}/.repos/{self.below}")

    def scopes_of(self, directory: Path) -> tuple[str, ...]:
        """The scopes of this folder or one inside it: its files and its checkouts."""
        relative = directory.relative_to(self.path).as_posix()
        if relative == ".":
            return self.scopes
        below = self.id(relative)
        return f"{self.key}/{below}", f"{self.key}/.repos/{below}"


class FilesystemConnector:
    """Scan `roots`. `anchors` are the configured folders (by default, `roots`): a scanned folder inside one of them
    keeps the ids that folder's own scan gives its files, and a configured folder inside a scanned folder outside
    them all is scanned as a root of its own, for the same reason. A folder inside another scanned one with the same
    anchor is scanned as part of it.

    The cursor maps each stored id to the mtime and size it was stored at, so an unchanged file is never reopened;
    a scan replaces only its own folders' entries. A folder that cannot be listed stays as it was: its items are
    kept and its cursor entries carried over.
    """

    name = "fs"

    def __init__(self, roots: Path | Iterable[Path], anchors: Iterable[Path] = ()):
        roots = (roots,) if isinstance(roots, Path) else tuple(roots)
        resolved = {Path(root).expanduser().resolve() for root in roots}
        if not resolved:
            raise ValueError("at least one filesystem root is required")
        if missing := sorted(str(root) for root in resolved if not root.is_dir()):
            raise ValueError(f"filesystem roots must be directories: {', '.join(missing)}")
        configured = {Path(a).expanduser().resolve() for a in anchors} or set(resolved)
        # Outermost first: the configured folders, then the scanned folders in none of them.
        free = [r for r in resolved if not any(r.is_relative_to(c) for c in configured)]
        by_depth = [*sorted(configured, key=_depth), *sorted(free, key=_depth)]

        def anchor(path: Path) -> Path:
            return next(a for a in by_depth if path.is_relative_to(a))

        resolved |= {c for r in free for c in configured if c.is_relative_to(r) and c.is_dir()}
        outer = sorted((r for r in resolved
                        if not any(o != r and r.is_relative_to(o) and anchor(o) == anchor(r) for o in resolved)), key=str)
        self.roots = tuple(_Root(r, _root_key(anchor(r)), _below(r, anchor(r))) for r in outer)
        # The keys current ids are under (other syncs of these folders use them too): never an earlier scan's.
        self.in_use = {_root_key(a) for a in configured if anchor(a) == a} | {root.key for root in self.roots}

    def _walk(self, directory: Path, unreadable: list[Path], skip: set[Path],
              listed: list[Path]) -> Iterator[tuple[str, Path]]:
        """("file", path) for each file and ("repo", folder) for each git checkout below `directory`, in name order,
        leaving out the folders in `skip` (roots scanned on their own). Folders listed go to `listed`, and folders
        that cannot be listed to `unreadable`."""
        try:
            with os.scandir(directory) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
        except OSError:
            unreadable.append(directory)
            return
        listed.append(directory)
        if any(entry.name == ".git" for entry in entries):
            yield "repo", directory
        for entry in entries:
            if entry.name == ".git" or _is_secret(entry.name) or NOISE_NAMES.match(entry.name):
                continue
            try:
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if not is_dir:
                yield "file", Path(entry.path)
            elif entry.name not in IGNORED_DIRECTORIES and Path(entry.path) not in skip:
                yield from self._walk(Path(entry.path), unreadable, skip, listed)

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint | CompletedScan]:
        previous = cursor.get("files") if isinstance(cursor.get("files"), dict) else {}
        stored: dict[str, list[int]] = {}  # this scan: ids now stored (or unchanged), with their fingerprints
        observed: set[str] = set()
        kept: list[str] = []
        unreadable: list[Path] = []
        listed: list[Path] = []
        due = Every()  # the cursor holds every file: write it every so often, not every so many files
        for root in self.roots:
            inside = {o.path for o in self.roots if o.path != root.path and o.path.is_relative_to(root.path)}
            blocked: list[Path] = []
            for kind, path in self._walk(root.path, blocked, inside, listed):
                seen = (self._repo if kind == "repo" else self._file)(root, path, previous)
                if seen is None:
                    continue
                external_id, item, fingerprint = seen
                observed.add(external_id)
                if item is not None:
                    yield item
                if fingerprint is not None:  # recorded only once the item is stored
                    stored[external_id] = fingerprint
                if item is not None and due():
                    yield Checkpoint({"files": {**previous, **stored}})
            kept += [scope for folder in blocked for scope in root.scopes_of(folder)]
            unreadable += blocked
        scanned = [scope for root in self.roots for scope in root.scopes]
        # A folder listed here whose own key an earlier scan gave ids under (it was a root then: inside another one,
        # or synced with --path) has its files under the ids above now. What is stored under its key goes, except
        # below a folder that could not be listed.
        earlier = {i.split("/", 1)[0] for i in previous} - self.in_use
        for folder in listed if earlier else ():
            if (key := _root_key(folder)) in earlier:
                scanned.append(key)
                for below in (u.relative_to(folder).as_posix() for u in unreadable if u.is_relative_to(folder)):
                    kept += [f"{key}/{below}", f"{key}/.repos/{below}"]
        carried = {i: fp for i, fp in previous.items() if not _in(i, scanned) or _in(i, kept)}
        yield CompletedScan({"files": {**carried, **stored}}, self.name, frozenset(observed),
                            scopes=tuple(scanned), kept=tuple(kept))

    def _file(self, root: _Root, path: Path, previous: dict) -> _Seen | None:
        try:
            stat = path.stat()
        except OSError:
            return None  # gone since the folder was listed
        external_id = f"{root.key}/{root.id(path.relative_to(root.path).as_posix())}"
        fingerprint = _fingerprint(stat)
        if previous.get(external_id) == fingerprint:
            return external_id, None, fingerprint
        folders = external_id.split("/")[:-1]
        meta = {"path": external_id, "folders": ["/".join(folders[:i]) for i in range(1, len(folders) + 1)],
                "size": stat.st_size, "model_opt_in": False}
        body, mime = None, None
        # Only a document the parsers read, and not too large, is opened at all; anything else is its metadata.
        if not supported(path.name):
            meta.update({"parser": None, "parse_status": "unsupported"})
        elif stat.st_size > MAX_DOCUMENT_BYTES:
            meta.update({"parser": None, "parse_status": "too_large"})
        else:
            try:
                data = path.read_bytes()
            except OSError:
                return external_id, None, None  # its item stays as it was; read again next time
            parsed = parse_document(data, path.name)
            if parsed.status == "ok":
                body, mime = parsed.text, parsed.mime
                meta.update({"parser": parsed.parser, "boundaries": parsed.boundaries})
            else:
                meta.update({"parser": None, "parse_status": parsed.status})
        return external_id, SourceItem(
            source=self.name, external_id=external_id, title=path.name, body_text=body,
            occurred_at=datetime.fromtimestamp(stat.st_mtime, UTC), mime=mime, raw_meta=meta), fingerprint

    def _repo(self, root: _Root, directory: Path, previous: dict) -> _Seen | None:
        """A git checkout is one evidence-bearing item (its name and remote), not an import of its files."""
        remote = _git_remote(directory)
        if remote is None:
            return None
        below = root.id(directory.relative_to(root.path).as_posix())
        repo_id = f"{root.key}/.repos/{below}"
        try:
            git = directory / ".git"
            stat = git.stat() if git.is_file() else (git / "config").stat()
        except OSError:
            return repo_id, None, None
        fingerprint = _fingerprint(stat)
        if previous.get(repo_id) == fingerprint:
            return repo_id, None, fingerprint
        name = directory.name
        return repo_id, SourceItem(
            source=self.name, external_id=repo_id, title=name, body_text=None,
            occurred_at=datetime.fromtimestamp(stat.st_mtime, UTC),
            raw_meta={"path": f"{root.key}/{below}", "folders": [root.key, f"{root.key}/{below}"],
                      "kind": "repo", "repo": name, "remote": remote, "model_opt_in": False}), fingerprint
