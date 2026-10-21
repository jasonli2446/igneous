"""Read Apple Notes' local SQLite store without modifying it."""
from __future__ import annotations

import gzip
import hashlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from igneous.connectors.base import Checkpoint, CompletedScan, SourceItem
from igneous.connectors.redact import redact

DEFAULT_DB = Path.home() / "Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)


def apple_time(value: object) -> datetime | None:
    try:
        return APPLE_EPOCH + timedelta(seconds=float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _varint(data: bytes, offset: int) -> tuple[int, int] | None:
    value, shift = 0, 0
    while offset < len(data) and shift < 64:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    return None


def _protobuf_strings(data: bytes, depth: int = 0) -> list[str]:
    """Extract UTF-8 fields from Apple's compressed protobuf without schema assumptions."""
    if depth > 8:
        return []
    out: list[str] = []
    offset = 0
    while offset < len(data):
        key = _varint(data, offset)
        if key is None:
            break
        tag, offset = key
        wire = tag & 7
        if wire == 0:
            value = _varint(data, offset)
            if value is None:
                break
            _, offset = value
        elif wire == 1:
            offset += 8
        elif wire == 5:
            offset += 4
        elif wire == 2:
            length = _varint(data, offset)
            if length is None:
                break
            size, offset = length
            field, offset = data[offset:offset + size], offset + size
            try:
                text = field.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            if text and all(char.isprintable() or char in "\n\t\r" for char in text):
                out.append(text)
            out.extend(_protobuf_strings(field, depth + 1))
        else:
            break
    return out


def decode_body(data: bytes | None) -> str | None:
    if not data:
        return None
    try:
        raw = gzip.decompress(data)
    except (OSError, EOFError):
        return None
    # Order is meaningful in protobuf's text-storage fields; exact repeats are formatting artifacts.
    strings = _protobuf_strings(raw)
    body = "\n".join(dict.fromkeys(text.strip() for text in strings if text.strip()))
    return body or None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _note_query(connection: sqlite3.Connection) -> tuple[str, str, str, str, tuple[str, str, str, str, str | None]] | None:
    """Probe stable CoreData names rather than pinning a single macOS Notes schema."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    objects = next((name for name in tables if "CLOUDSYNCINGOBJECT" in name), None)
    data_table = next((name for name in tables if "NOTEDATA" in name), None)
    if not objects or not data_table:
        return None
    note_columns, data_columns = _columns(connection, objects), _columns(connection, data_table)
    title = next((c for c in ("ZTITLE1", "ZTITLE2", "ZTITLE") if c in note_columns), None)
    blob = next((c for c in ("ZDATA", "ZMERGEABLEDATA") if c in data_columns), None)
    # Modern CoreData stores the relationship as ZICNOTEDATA.ZNOTE -> object.Z_PK. Older layouts may share it.
    data_join = next((c for c in ("ZNOTE", "ZOWNER", "ZICCLOUDSYNCINGOBJECT") if c in data_columns), None)
    object_join = "Z_PK" if data_join and "Z_PK" in note_columns else data_join if data_join in note_columns else None
    if not title or not blob or not data_join or not object_join:
        return None
    identifier = next((c for c in ("ZIDENTIFIER", "ZUNIQUEIDENTIFIER") if c in note_columns), "Z_PK")
    modified = next((c for c in ("ZMODIFICATIONDATE", "ZCREATIONDATE") if c in note_columns), "Z_PK")
    locked = next((c for c in note_columns if "PASSWORD" in c or "LOCKED" in c or "ENCRYPT" in c), None)
    return objects, data_table, title, blob, (identifier, modified, data_join, object_join, locked)


class AppleNotesConnector:
    """Read unlocked Notes.app notes. It is opt-in and never writes the Apple database."""

    name = "notes"

    def __init__(self, path: Path = DEFAULT_DB):
        self.path = Path(path).expanduser()

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint | CompletedScan]:
        previous = cursor.get("notes", {}) if isinstance(cursor.get("notes", {}), dict) else {}
        observed, current = set(), {}
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            spec = _note_query(connection)
            if spec is None:
                return
            objects, data_table, title, blob, details = spec
            identifier, modified, data_join, object_join, locked = details
            lock_select = f', n."{locked}"' if locked else ""
            query = (f'SELECT n."Z_PK", n."{identifier}", n."{title}", n."{modified}", d."{blob}"{lock_select} '
                     f'FROM "{objects}" n JOIN "{data_table}" d ON d."{data_join}" = n."{object_join}" '
                     f'WHERE n."{title}" IS NOT NULL')
            for row in connection.execute(query):
                primary_key, identity, title_value, modified_value, blob_value, *lock_value = row
                if lock_value and lock_value[0]:
                    continue
                external_id = str(identity if identity is not None else primary_key)
                observed.add(external_id)
                fingerprint = hashlib.sha256(bytes(blob_value or b"")).hexdigest()
                current[external_id] = fingerprint
                if previous.get(external_id) == fingerprint:
                    continue
                body = decode_body(blob_value)
                if not body:
                    continue
                body, redactions = redact(body)
                yield SourceItem(source=self.name, external_id=external_id, title=str(title_value), body_text=body,
                                 occurred_at=apple_time(modified_value), mime="text/plain",
                                 raw_meta={"path": f"notes/{external_id}", "folders": ["notes"],
                                           "parser": "apple-notes", "redactions": redactions,
                                           "model_opt_in": False}, content_hash=fingerprint)
        finally:
            connection.close()
        yield CompletedScan({"notes": current}, self.name, frozenset(observed))
