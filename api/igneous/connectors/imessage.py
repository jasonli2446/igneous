"""macOS Messages (iMessage / SMS) connector. Reads ~/Library/Messages/chat.db read-only (CO-2).

One SourceItem per conversation per day: single texts are too short to search or extract from, and a day's
exchange is the natural unit people remember ("what did Sam and I decide on Tuesday"). The first sync reads
`since_days` back; the cursor is then the highest message ROWID read, so a re-sync re-emits the days of every row
added since, including older messages that reach this Mac late from another device.

Needs Full Disk Access for the process reading chat.db (System Settings -> Privacy & Security). Most text is
in `attributedBody` (an NSAttributedString typedstream), not `text`; decode_attributed handles it.
"""
import os
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from igneous.connectors.base import Checkpoint, Every, SourceItem

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
DEFAULT_DB = Path(os.path.expanduser("~/Library/Messages/chat.db"))


def apple_time(ns: int | None) -> datetime | None:
    if not ns:
        return None
    # Since macOS 10.13 dates are nanoseconds since 2001-01-01; older rows are seconds.
    return APPLE_EPOCH + timedelta(seconds=ns / 1e9 if ns > 1e11 else ns)


def decode_attributed(blob: bytes | None) -> str | None:
    """Pull the plain string out of an NSAttributedString typedstream.

    The payload follows the 'NSString' class name: a '+' marker, then a length byte (0x81 -> 2-byte length,
    0x82 -> 4-byte length, little-endian), then UTF-8 bytes."""
    if not blob:
        return None
    i = blob.find(b"NSString")
    j = blob.find(b"+", i) if i >= 0 else -1
    if j < 0 or j + 1 >= len(blob):
        return None
    n, k = blob[j + 1], j + 2
    if n == 0x81:
        n, k = int.from_bytes(blob[k:k + 2], "little"), k + 2
    elif n == 0x82:
        n, k = int.from_bytes(blob[k:k + 4], "little"), k + 4
    return blob[k:k + n].decode("utf-8", errors="replace").strip() or None


class MessagesConnector:
    name = "imessage"

    def __init__(self, db: Path = DEFAULT_DB, since_days: int | None = None):
        self.db = Path(db)
        self.since_days = since_days

    def _connect(self) -> sqlite3.Connection:
        try:
            return sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        except sqlite3.OperationalError as e:
            raise PermissionError(
                f"can't open {self.db} ({e}). Grant Full Disk Access to your terminal in System Settings -> "
                "Privacy & Security -> Full Disk Access, then restart it.") from e

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint]:
        db = self._connect()
        # Everything up to this row is read now; a row that arrives meanwhile is read next time.
        top = db.execute("SELECT COALESCE(max(ROWID), 0) FROM message").fetchone()[0]
        if "last_rowid" in cursor:
            # Rows added since, whatever their date: an older message that reaches this Mac late (from another
            # device, or a restored backup) is caught too.
            since, value = "m.ROWID > ?", cursor["last_rowid"]
        else:
            # A first sync reads by date (`last_date` is where a cursor of dates stopped).
            after = cursor.get("last_date", 0)
            if not after and self.since_days:
                after = int((datetime.now(UTC) - timedelta(days=self.since_days) - APPLE_EPOCH).total_seconds() * 1e9)
            since, value = "m.date > ?", after
        # Days touched; each is re-emitted whole so the item stays complete.
        changed = db.execute(f"""
            SELECT DISTINCT cmj.chat_id, date(m.date / 1000000000 + 978307200, 'unixepoch')
            FROM message m JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            WHERE {since} AND m.ROWID <= ? AND m.item_type = 0 AND m.associated_message_type = 0
        """, (value, top)).fetchall()
        if not changed:
            yield Checkpoint({"last_rowid": top})
            return

        chats = {r[0]: r for r in db.execute(
            "SELECT ROWID, chat_identifier, display_name, service_name, style FROM chat")}
        members: dict[int, list[str]] = defaultdict(list)
        for chat_id, handle in db.execute(
                "SELECT chj.chat_id, h.id FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id"):
            members[chat_id].append(handle)

        due = Every()
        for chat_id, day in sorted(changed, key=lambda r: r[1]):
            # The day's range of dates (nanoseconds since 2001), which the date index can serve.
            start = int((datetime.fromisoformat(day).replace(tzinfo=UTC) - APPLE_EPOCH).total_seconds()) * 10**9
            rows = db.execute("""
                SELECT m.date, m.is_from_me, m.text, m.attributedBody, h.id, m.cache_has_attachments
                FROM message m JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                WHERE cmj.chat_id = ? AND m.date >= ? AND m.date < ?
                  AND m.item_type = 0 AND m.associated_message_type = 0  -- skip tapbacks/system rows
                ORDER BY m.date
            """, (chat_id, start, start + 86400 * 10**9)).fetchall()
            lines, senders = [], set()
            for date, from_me, txt, attributed, handle, has_att in rows:
                body = txt or decode_attributed(attributed) or ("[attachment]" if has_att else None)
                if not body:
                    continue
                who = "me" if from_me else (handle or "unknown")
                senders.add(who)
                t = apple_time(date)
                lines.append(f"[{t:%H:%M}] {who}: {body}" if t else f"{who}: {body}")
            if due():
                yield Checkpoint(cursor)  # commits the days so far; the cursor moves when every day is read
            if not lines:
                continue
            c = chats.get(chat_id)
            identifier, display, service, style = (c[1], c[2], c[3], c[4]) if c else (str(chat_id), "", "", 0)
            people = members.get(chat_id, [])
            title = display or (", ".join(people[:4]) if people else identifier)
            yield SourceItem(
                source="imessage", external_id=f"{identifier}/{day}", title=f"{title} — {day}",
                body_text="\n".join(lines), occurred_at=apple_time(rows[0][0]), mime="text/plain",
                raw_meta={"chat": identifier, "service": service, "group": style == 43, "day": day,
                          "participants": people, "senders": sorted(senders), "messages": len(lines)})
        yield Checkpoint({"last_rowid": top})
