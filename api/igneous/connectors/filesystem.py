"""Deterministic, full-scan connector for locally mounted filesystems."""
from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from igneous.connectors.base import Checkpoint, CompletedScan, SourceItem
from igneous.parsers import parse_document

CHECKPOINT_EVERY = 100
IGNORED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".tox", ".venv", "__pycache__", "node_modules", "vendor",
    "dist", "build", ".next", ".pytest_cache", ".mypy_cache",
})
IGNORED_EXTENSIONS = frozenset({
    ".7z", ".avi", ".bin", ".bmp", ".class", ".dll", ".dmg", ".doc", ".eot", ".exe",
    ".gif", ".gz", ".ico", ".iso", ".jar", ".jpeg", ".jpg", ".lock", ".mov", ".mp3",
    ".mp4", ".otf", ".png", ".pyc", ".so", ".tar", ".tgz", ".ttf", ".wav",
    ".webm", ".webp", ".woff", ".woff2", ".zip",
})


def _raise_walk_error(error: OSError) -> None:
    raise error


class FilesystemConnector:
    """Import parseable files beneath configured roots on every sync.

    A filesystem has no durable incremental cursor: each invocation walks complete,
    sorted trees. This is intentional so the final ``CompletedScan`` can safely
    reconcile files removed since the previous successful scan.
    """

    name = "fs"

    def __init__(self, roots: Path | Iterable[Path]):
        roots = (roots,) if isinstance(roots, Path) else tuple(roots)
        self.roots = tuple(sorted({Path(root).expanduser().resolve() for root in roots}, key=str))
        if not self.roots:
            raise ValueError("at least one filesystem root is required")
        if missing := [str(root) for root in self.roots if not root.is_dir()]:
            raise ValueError(f"filesystem roots must be directories: {', '.join(missing)}")
        if len(self.roots) > 1 and len({root.name for root in self.roots}) != len(self.roots):
            raise ValueError("filesystem roots must have distinct directory names")

    def _files(self, root: Path) -> Iterator[Path]:
        files = []
        for directory, directories, filenames in os.walk(root, topdown=True, followlinks=False, onerror=_raise_walk_error):
            directories[:] = sorted(
                name for name in directories
                if name not in IGNORED_DIRECTORIES and not (Path(directory) / name).is_symlink()
            )
            files.extend(
                Path(directory) / filename for filename in filenames
                if (Path(directory) / filename).suffix.lower() not in IGNORED_EXTENSIONS
                and not (Path(directory) / filename).is_symlink()
            )
        yield from sorted(files, key=lambda path: path.relative_to(root).as_posix())

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint | CompletedScan]:
        del cursor  # Full scans, rather than path cursors, make deletion reconciliation sound.
        processed = 0
        complete = True
        multiple_roots = len(self.roots) > 1
        for root in self.roots:
            for path in self._files(root):
                try:
                    data = path.read_bytes()
                    stat = path.stat()
                except OSError:
                    # An incomplete observation must never become permission to prune old rows.
                    complete = False
                    continue
                parsed = parse_document(data, path.name)
                if parsed.status != "ok":
                    continue
                relative_path = path.relative_to(root).as_posix()
                external_id = f"{root.name}/{relative_path}" if multiple_roots else relative_path
                folders = external_id.split("/")[:-1]
                hierarchy = ["/".join(folders[:index]) for index in range(1, len(folders) + 1)]
                yield SourceItem(
                    source=self.name,
                    external_id=external_id,
                    title=path.name,
                    body_text=parsed.text,
                    occurred_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                    mime=parsed.mime,
                    raw_meta={
                        "path": external_id,
                        "folders": hierarchy,
                        "parser": parsed.parser,
                        "boundaries": parsed.boundaries,
                    },
                    # Identity is the file's bytes, not just its extracted text or metadata.
                    content_hash=hashlib.sha256(data).hexdigest(),
                )
                processed += 1
                if processed % CHECKPOINT_EVERY == 0:
                    yield Checkpoint({})
        if complete:
            yield CompletedScan({})
