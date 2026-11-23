"""The databases of the macOS apps Igneous reads (Messages, Contacts, Notes), opened read-only (CO-2).

macOS shows them only to an app with Full Disk Access: without it, opening or reading one fails, and the error says
how to grant it.
"""
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

FULL_DISK_ACCESS = ("If macOS is blocking it, turn on Full Disk Access for the app that runs Igneous (System Settings, "
                    "Privacy & Security, Full Disk Access), restart that app, and sync again.")


@contextmanager
def read_only(path: Path | str) -> Iterator[sqlite3.Connection]:
    """A read-only connection to the SQLite database at `path`, closed on leaving. Raises PermissionError, saying
    what to do, when this process can't open or read it."""
    uri = Path(path).expanduser().absolute().as_uri() + "?mode=ro"  # percent-encoded: any file name is safe
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        raise PermissionError(f"Can't open {path} ({e}). {FULL_DISK_ACCESS}") from e
    try:
        try:
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        except sqlite3.Error as e:
            raise PermissionError(f"Can't read {path} ({e}). {FULL_DISK_ACCESS}") from e
        yield connection
    finally:
        connection.close()
