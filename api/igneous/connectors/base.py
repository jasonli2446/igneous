"""Connector contract and the sync runner that makes every connector resumable and idempotent."""
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class SourceItem:
    source: str
    external_id: str
    title: str | None
    body_text: str | None
    occurred_at: datetime | None = None
    mime: str | None = None
    raw_meta: dict = field(default_factory=dict)
    parent_external_id: str | None = None  # attachments point at their message
    content_hash: str = ""

    def __post_init__(self):
        if not self.content_hash:
            h = hashlib.sha256()
            h.update((self.title or "").encode())
            h.update((self.body_text or "").encode())
            h.update(json.dumps(self.raw_meta, sort_keys=True, default=str).encode())
            self.content_hash = h.hexdigest()


@dataclass
class Checkpoint:
    """Yielded by a connector when everything before it is safe to skip on resume."""
    cursor: dict


class Connector(Protocol):
    name: str

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint]: ...


UPSERT = text("""
INSERT INTO source_items (source, external_id, parent_id, content_hash, mime, title, body_text,
                          raw_meta, occurred_at)
VALUES (:source, :external_id,
        (SELECT id FROM source_items WHERE source = :source AND external_id = :parent_external_id),
        :content_hash, :mime, :title, :body_text, CAST(:raw_meta AS jsonb), :occurred_at)
ON CONFLICT (source, external_id) DO UPDATE SET
    content_hash = EXCLUDED.content_hash, mime = EXCLUDED.mime, title = EXCLUDED.title,
    body_text = EXCLUDED.body_text, raw_meta = EXCLUDED.raw_meta, occurred_at = EXCLUDED.occurred_at,
    parent_id = EXCLUDED.parent_id, updated_at = now()
WHERE source_items.content_hash <> EXCLUDED.content_hash
RETURNING (xmax = 0) AS inserted
""")


def load_cursor(s: Session, connector: str, account: str) -> dict:
    row = s.execute(text("SELECT cursor FROM sync_state WHERE connector = :c AND account = :a"),
                    {"c": connector, "a": account}).first()
    return row[0] if row else {}


def save_cursor(s: Session, connector: str, account: str, cursor: dict) -> None:
    s.execute(text("""
        INSERT INTO sync_state (connector, account, cursor) VALUES (:c, :a, CAST(:cur AS jsonb))
        ON CONFLICT (connector, account) DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now()
    """), {"c": connector, "a": account, "cur": json.dumps(cursor)})


def run_sync(s: Session, connector: Connector, account: str = "default", limit: int | None = None) -> dict:
    """Upsert every item; unchanged content is a no-op. Commits at each checkpoint so a crash resumes."""
    stats = {"inserted": 0, "updated": 0, "unchanged": 0}
    cursor = load_cursor(s, connector.name, account)
    seen = 0
    for obj in connector.sync(cursor):
        if isinstance(obj, Checkpoint):
            save_cursor(s, connector.name, account, obj.cursor)
            s.commit()
            if limit is not None and seen >= limit:
                break
            continue
        row = s.execute(UPSERT, {
            "source": obj.source, "external_id": obj.external_id,
            "parent_external_id": obj.parent_external_id, "content_hash": obj.content_hash,
            "mime": obj.mime, "title": obj.title, "body_text": obj.body_text,
            "raw_meta": json.dumps(obj.raw_meta, default=str), "occurred_at": obj.occurred_at,
        }).first()
        if row is None:
            stats["unchanged"] += 1
        elif row.inserted:
            stats["inserted"] += 1
        else:
            stats["updated"] += 1
        seen += 1
    s.commit()
    return stats
