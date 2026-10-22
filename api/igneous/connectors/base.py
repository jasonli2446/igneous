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


@dataclass
class CompletedScan:
    """Final source snapshot event; only it permits reconciliation for ``source``.

    ``external_ids`` contains every observed path, including files a connector chose not
    to emit as a SourceItem. It must be the final event from a connector sync.
    """
    cursor: dict
    source: str
    external_ids: frozenset[str]
    allow_empty_prune: bool = False
    external_id_prefix: str | None = None  # constrain reconciliation for account-scoped snapshots


@dataclass
class Tombstone:
    """A source item (or an account-safe external-ID prefix) removed by a change feed."""
    source: str
    external_id: str
    prefix: bool = False


class Connector(Protocol):
    name: str

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint | CompletedScan]: ...


UPSERT = text("""
INSERT INTO source_items (source, external_id, parent_id, content_hash, mime, title, body_text,
                          raw_meta, occurred_at)
VALUES (:source, :external_id,
        (SELECT id FROM source_items WHERE source = :source AND external_id = :parent_external_id),
        :content_hash, :mime, :title, :body_text, CAST(:raw_meta AS jsonb), :occurred_at)
ON CONFLICT (source, external_id) DO UPDATE SET
    content_hash = EXCLUDED.content_hash, mime = EXCLUDED.mime, title = EXCLUDED.title,
    body_text = EXCLUDED.body_text, raw_meta = EXCLUDED.raw_meta, occurred_at = EXCLUDED.occurred_at,
    parent_id = EXCLUDED.parent_id, item_class = NULL, updated_at = now()
WHERE source_items.content_hash <> EXCLUDED.content_hash
RETURNING (xmax = 0) AS inserted
""")


def _no_nul(t: str | None) -> str | None:
    return t.replace("\x00", "") if t else t


def load_cursor(s: Session, connector: str, account: str) -> dict:
    row = s.execute(text("SELECT cursor FROM sync_state WHERE connector = :c AND account = :a"),
                    {"c": connector, "a": account}).first()
    return row[0] if row else {}


def save_cursor(s: Session, connector: str, account: str, cursor: dict) -> None:
    s.execute(text("""
        INSERT INTO sync_state (connector, account, cursor) VALUES (:c, :a, CAST(:cur AS jsonb))
        ON CONFLICT (connector, account) DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now()
    """), {"c": connector, "a": account, "cur": json.dumps(cursor)})


def _reconcile_completed_scan(s: Session, source: str, external_ids: frozenset[str],
                              allow_empty_prune: bool, external_id_prefix: str | None = None) -> int:
    """Remove items absent from a completed, non-suspicious source snapshot."""
    if not external_ids and not allow_empty_prune:
        return 0
    scope = ""
    values = {"source": source, "external_ids": sorted(external_ids), "prefix": external_id_prefix}
    if external_id_prefix is not None:
        # Use a literal prefix comparison rather than LIKE: account IDs may contain wildcard characters.
        scope = " AND left(external_id, char_length(:prefix)) = :prefix"
    if external_ids:
        result = s.execute(text(f"""
            DELETE FROM source_items
            WHERE source = :source{scope} AND external_id <> ALL(CAST(:external_ids AS text[]))
        """), values)
    else:
        result = s.execute(text(f"DELETE FROM source_items WHERE source = :source{scope}"), values)
    return result.rowcount


def run_sync(s: Session, connector: Connector, account: str = "default", limit: int | None = None) -> dict:
    """Upsert every item; reconcile deletions only after an explicit completed scan.

    Checkpoints deliberately do not imply a complete scan. Thus, an exception, process
    stop, or ``--limit`` before ``CompletedScan`` can commit discovered additions and
    updates but can never prune unseen source items.
    """
    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    cursor = load_cursor(s, connector.name, account)
    seen = 0
    for obj in connector.sync(cursor):
        if isinstance(obj, CompletedScan):
            if limit is None:
                stats["deleted"] += _reconcile_completed_scan(
                    s, obj.source, obj.external_ids, obj.allow_empty_prune, obj.external_id_prefix)
            save_cursor(s, connector.name, account, obj.cursor)
            s.commit()
            break
        if isinstance(obj, Tombstone):
            where = "left(external_id, char_length(:external_id)) = :external_id" if obj.prefix else "external_id = :external_id"
            result = s.execute(text(f"DELETE FROM source_items WHERE source = :source AND {where}"),
                               {"source": obj.source, "external_id": obj.external_id})
            stats["deleted"] += result.rowcount
            continue
        if isinstance(obj, Checkpoint):
            save_cursor(s, connector.name, account, obj.cursor)
            s.commit()
            if limit is not None and seen >= limit:
                break
            continue
        row = s.execute(UPSERT, {
            "source": obj.source, "external_id": obj.external_id,
            "parent_external_id": obj.parent_external_id, "content_hash": obj.content_hash,
            # Postgres text can't hold NUL; some PDFs and Office files extract with them.
            "mime": obj.mime, "title": _no_nul(obj.title), "body_text": _no_nul(obj.body_text),
            "raw_meta": json.dumps(obj.raw_meta, default=str).replace("\\u0000", ""), "occurred_at": obj.occurred_at,
        }).first()
        if row is None:
            stats["unchanged"] += 1
        elif row.inserted:
            stats["inserted"] += 1
        else:
            stats["updated"] += 1
        seen += 1
    if stats["deleted"]:
        # Deleted rows cannot reach spine; force dependent evidence stages to rebuild.
        from igneous.graph import dirty
        dirty.mark_rebuild(s)
    s.commit()
    return stats
