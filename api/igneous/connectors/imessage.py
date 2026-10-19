"""macOS Messages (iMessage / SMS) connector. Reads ~/Library/Messages/chat.db read-only (CO-2).

One SourceItem per conversation per day: single texts are too short to search or extract from, and a day's
exchange is the natural unit people remember ("what did Sam and I decide on Tuesday"). The cursor is the
newest message date seen, so re-syncs only re-emit the days that changed.

Needs Full Disk Access for the process reading chat.db (System Settings -> Privacy & Security). Most text is
in `attributedBody` (an NSAttributedString typedstream), not `text`; decode_attributed handles it.
"""
import os
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from igneous.connectors.base import Checkpoint, SourceItem

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
        after = cursor.get("last_date", 0)
        if not after and self.since_days:
            after = int((datetime.now(UTC) - timedelta(days=self.since_days) - APPLE_EPOCH).total_seconds() * 1e9)

        # Days touched since the cursor; each is re-emitted whole so the item stays complete.
        changed = db.execute("""
            SELECT DISTINCT cmj.chat_id, date(m.date / 1000000000 + 978307200, 'unixepoch')
            FROM message m JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            WHERE m.date > ? AND m.item_type = 0 AND m.associated_message_type = 0
        """, (after,)).fetchall()
        if not changed:
            yield Checkpoint({"last_date": after})
            return

        chats = {r[0]: r for r in db.execute(
            "SELECT ROWID, chat_identifier, display_name, service_name, style FROM chat")}
        members: dict[int, list[str]] = defaultdict(list)
        for chat_id, handle in db.execute(
                "SELECT chj.chat_id, h.id FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id"):
            members[chat_id].append(handle)

        newest = after
        for chat_id, day in sorted(changed, key=lambda r: r[1]):
            rows = db.execute("""
                SELECT m.date, m.is_from_me, m.text, m.attributedBody, h.id, m.cache_has_attachments
                FROM message m JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                WHERE cmj.chat_id = ? AND date(m.date / 1000000000 + 978307200, 'unixepoch') = ?
                  AND m.item_type = 0 AND m.associated_message_type = 0  -- skip tapbacks/system rows
                ORDER BY m.date
            """, (chat_id, day)).fetchall()
            lines, senders = [], set()
            for date, from_me, txt, attributed, handle, has_att in rows:
                body = txt or decode_attributed(attributed) or ("[attachment]" if has_att else None)
                if not body:
                    continue
                who = "me" if from_me else (handle or "unknown")
                senders.add(who)
                t = apple_time(date)
                lines.append(f"[{t:%H:%M}] {who}: {body}" if t else f"{who}: {body}")
                newest = max(newest, date or 0)
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
        yield Checkpoint({"last_date": newest})
