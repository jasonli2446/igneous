"""The databases of the macOS apps Igneous reads (Messages, Contacts, Notes), opened read-only (CO-2).

macOS shows them only to an app with Full Disk Access: without it, opening one is refused, and the error says how to
grant it. A file that opens but holds no database SQLite can read (damaged, or not a database) is a different error,
and says that instead.
"""
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

FULL_DISK_ACCESS = ("If macOS is blocking it, turn on Full Disk Access for the app that runs Igneous (System Settings, "
                    "Privacy & Security, Full Disk Access), restart that app, and sync again.")
# SQLite's result codes for a file this process may not open, which is how macOS refuses an app without access.
_REFUSED = {sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_PERM, sqlite3.SQLITE_AUTH, sqlite3.SQLITE_READONLY}


@contextmanager
def read_only(path: Path | str) -> Iterator[sqlite3.Connection]:
    """A read-only connection to the SQLite database at `path`, closed on leaving. Raises FileNotFoundError when
    there is no such file, PermissionError (saying what to do) when this process may not open it, and
    sqlite3.DatabaseError (naming the file) when it holds no database SQLite can read."""
    path = Path(path).expanduser().absolute()
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)  # percent-encoded: any name is safe
    except sqlite3.Error as e:
        raise _failure(path, e) from e
    try:
        try:
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        except sqlite3.Error as e:
            raise _failure(path, e) from e
        yield connection
    finally:
        connection.close()


def _failure(path: Path, e: sqlite3.Error) -> Exception:
    """What a failure to open or first read `path` means, as the error to raise."""
    if getattr(e, "sqlite_errorcode", 0) & 0xFF not in _REFUSED:
        return sqlite3.DatabaseError(f"{path} is not a database Igneous can read ({e}).")
    try:
        path.stat()
    except FileNotFoundError:
        return FileNotFoundError(f"{path} not found.")
    except OSError:
        pass  # refused even a look at it: that is macOS's protection
    return PermissionError(f"Can't open {path} ({e}). {FULL_DISK_ACCESS}")
