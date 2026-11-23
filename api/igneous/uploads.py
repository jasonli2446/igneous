"""Uploaded export files on their way to an import job.

`stage()` copies a request's uploads into a folder of their own in a worker thread (a multi-GB copy must not stall
the API's event loop). The import job that reads them removes the folder when it ends; if the job never starts (the
input is rejected, or that import is already running) the folder goes at once; and a folder left by a process that
died is removed the next time the API starts (`sweep`).
"""
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

from starlette.concurrency import run_in_threadpool

ROOT = Path(tempfile.gettempdir()) / "igneous-uploads"
_MINE: set[Path] = set()  # folders this process staged and has not removed


class Staged:
    """The staged uploads of one request, as a context manager around checking them and starting their job: on
    leaving it the folder is removed, unless `hand_off` gave it to the job."""

    def __init__(self, folder: Path | None, files: list[Path]):
        self.folder, self.files, self._handed_off = folder, files, False

    def removing_after(self, runner: Callable) -> Callable:
        """`runner`, removing the folder once it returns or fails."""
        def run(*args):
            try:
                return runner(*args)
            finally:
                self.remove()
        return run

    def hand_off(self) -> None:
        self._handed_off = True

    def remove(self) -> None:
        if self.folder is not None:
            shutil.rmtree(self.folder, ignore_errors=True)
            _MINE.discard(self.folder)

    def __enter__(self) -> "Staged":
        return self

    def __exit__(self, *exc) -> None:
        if not self._handed_off:
            self.remove()


def _copy(uploads: list, prefix: str) -> Staged:
    folder = ROOT / f"{os.getpid()}-{prefix}-{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True)
    _MINE.add(folder)
    staged = Staged(folder, [])
    try:
        for i, upload in enumerate(uploads):
            path = folder / f"{i}-{Path(upload.filename or 'upload').name}"
            with open(path, "wb") as out:
                shutil.copyfileobj(upload.file, out, 1024 * 1024)
            staged.files.append(path)
    except BaseException:
        staged.remove()
        raise
    return staged


async def stage(uploads: list, prefix: str) -> Staged:
    """Copy `uploads` (Starlette UploadFiles) into a new folder, off the event loop."""
    if not uploads:
        return Staged(None, [])
    return await run_in_threadpool(_copy, uploads, prefix)


def sweep() -> int:
    """Remove the folders of processes that are gone. A folder named for this process's pid that it didn't stage
    was left by an earlier process with the same pid (a restarted container)."""
    from igneous.jobs import _alive

    removed = 0
    for folder in ROOT.glob("*-*") if ROOT.is_dir() else []:
        pid = folder.name.split("-", 1)[0]
        if not pid.isdigit() or folder in _MINE:
            continue
        if int(pid) == os.getpid() or not _alive(int(pid)):
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed
