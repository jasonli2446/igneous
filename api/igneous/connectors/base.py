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
    """The last event of a sync that listed everything in its scopes. Only this event deletes items: the items of
    `source` inside `scopes` that the scan did not observe.

    A scope is an external id that covers itself and every id below it (`me@x.com` covers `me@x.com/...`), so a
    scan of one account, folder or mailbox reconciles only that part of the source. No scopes means the whole
    source. `kept` scopes are parts the scan could not list (a folder it may not read): their items stay.
    `external_ids` holds every id observed, including ids the connector chose not to store as items.
    """
    cursor: dict
    source: str
    external_ids: frozenset[str]
    scopes: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    allow_empty_prune: bool = False


@dataclass
class Tombstone:
    """An item a change feed reports removed. With `prefix`, every item in the scope `external_id` names (the id
    and everything below it, as in a CompletedScan)."""
    source: str
    external_id: str
    prefix: bool = False


class Connector(Protocol):
    name: str

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint | CompletedScan | Tombstone]: ...


UPSERT = text("""
INSERT INTO source_items (source, external_id, content_hash, mime, title, body_text, raw_meta, occurred_at)
VALUES (:source, :external_id, :content_hash, :mime, :title, :body_text, CAST(:raw_meta AS jsonb), :occurred_at)
ON CONFLICT (source, external_id) DO UPDATE SET
    content_hash = EXCLUDED.content_hash, mime = EXCLUDED.mime, title = EXCLUDED.title,
    body_text = EXCLUDED.body_text, raw_meta = EXCLUDED.raw_meta, occurred_at = EXCLUDED.occurred_at,
    item_class = NULL, updated_at = now()
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


def _in_scopes(param: str) -> str:
    """SQL: external_id lies in one of the scopes bound to :param. A literal comparison, not LIKE: account names
    and paths may contain wildcard characters."""
    return (f"EXISTS (SELECT 1 FROM unnest(CAST(:{param} AS text[])) AS scope(id) WHERE external_id = scope.id "
            f"OR left(external_id, char_length(scope.id) + 1) = scope.id || '/')")


def _reconcile(s: Session, scan: CompletedScan) -> int:
    """Delete the items in a completed scan's scopes that it did not observe."""
    if not scan.external_ids and not scan.allow_empty_prune:
        return 0  # an empty listing is likelier a failure than a source that emptied
    where = ["source = :source",
             "NOT EXISTS (SELECT 1 FROM unnest(CAST(:observed AS text[])) AS seen(id) WHERE seen.id = external_id)"]
    if scan.scopes:
        where.append(_in_scopes("scopes"))
    if scan.kept:
        where.append(f"NOT {_in_scopes('kept')}")
    return s.execute(text(f"DELETE FROM source_items WHERE {' AND '.join(where)}"), {
        "source": scan.source, "observed": sorted(scan.external_ids), "scopes": list(scan.scopes),
        "kept": list(scan.kept)}).rowcount


def _delete(s: Session, tombstone: Tombstone) -> int:
    where = _in_scopes("scopes") if tombstone.prefix else "external_id = :external_id"
    return s.execute(text(f"DELETE FROM source_items WHERE source = :source AND {where}"), {
        "source": tombstone.source, "external_id": tombstone.external_id, "scopes": [tombstone.external_id]}).rowcount


def _deleted(s: Session, n: int) -> int:
    """Deleted items can't reach the spine, so the stages that derive from evidence rebuild. The marker joins the
    deleting transaction: no deletion commits without it."""
    if n:
        from igneous.graph import dirty
        dirty.mark_rebuild(s)
    return n


def run_sync(s: Session, connector: Connector, account: str = "default", limit: int | None = None) -> dict:
    """Upsert every item; delete only what a Tombstone names or a CompletedScan did not observe.

    A Checkpoint commits what came before it and never implies a complete scan, so an exception, a stopped process
    or `limit` (which stops at the first checkpoint after that many items) can store additions and updates but never
    deletes an item the scan had not reached.
    """
    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    cursor = load_cursor(s, connector.name, account)
    seen = 0
    for obj in connector.sync(cursor):
        if isinstance(obj, CompletedScan):
            stats["deleted"] += _deleted(s, _reconcile(s, obj))
            save_cursor(s, connector.name, account, obj.cursor)
            s.commit()
            break
        if isinstance(obj, Tombstone):
            stats["deleted"] += _deleted(s, _delete(s, obj))
            continue
        if isinstance(obj, Checkpoint):
            save_cursor(s, connector.name, account, obj.cursor)
            s.commit()
            if limit is not None and seen >= limit:
                break
            continue
        row = s.execute(UPSERT, {
            "source": obj.source, "external_id": obj.external_id, "content_hash": obj.content_hash,
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
    s.commit()
    return stats
