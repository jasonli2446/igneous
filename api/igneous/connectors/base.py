"""Connector contract and the sync runner that makes every connector resumable and idempotent.

A connector yields SourceItems with Checkpoints between them, and Tombstones or a final CompletedScan for what was
deleted. `run_sync` is the storage boundary: it stores each item without NUL characters and with every credential
in its title and text redacted (`redact.py`), then upserts it, skipping it when its content hash is unchanged.
"""
import hashlib
import json
from bisect import bisect_left
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.connectors import redact as rules
from igneous.connectors.redact import redact, redact_moving


@dataclass
class SourceItem:
    """One record. `raw_meta["boundaries"]`, when a parser recorded them, are offsets into `body_text` (pages,
    sections, slides, sheets); `raw_meta["redactions"]` counts the credentials removed from the item's text."""
    source: str
    external_id: str
    title: str | None
    body_text: str | None
    occurred_at: datetime | None = None
    mime: str | None = None
    raw_meta: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """What the upsert compares to skip an unchanged item: a hash of the title, text and metadata."""
        h = hashlib.sha256()
        h.update((self.title or "").encode())
        h.update((self.body_text or "").encode())
        h.update(json.dumps(self.raw_meta, sort_keys=True, default=str).encode())
        return h.hexdigest()


def _without_nul(value, positions: list[int] | None = None):
    """`value` without NUL characters, which Postgres text can't hold (some PDFs and Office files extract with
    them), in strings at any depth. With `positions` (offsets into a string), also where each lands."""
    if isinstance(value, str):
        if "\x00" not in value:
            return value if positions is None else (value, positions)
        nuls = [i for i, c in enumerate(value) if c == "\x00"]
        clean = value.replace("\x00", "")
        return clean if positions is None else (clean, [p - bisect_left(nuls, p) for p in positions])
    if isinstance(value, dict):
        return {_without_nul(k): _without_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_without_nul(v) for v in value]
    return value if positions is None else (value, positions)


def _count(redactions) -> int:
    """A redaction count as stored: a number, or (on items stored before counts were totals) one per kind."""
    if isinstance(redactions, dict):
        return sum(v for v in redactions.values() if isinstance(v, int))
    return redactions if isinstance(redactions, int) else 0


def _store(item: SourceItem, offsets: list[int]) -> tuple[SourceItem, list[int]]:
    """`stored(item)`, and where each of `offsets` (positions in its text) lands in the stored text."""
    meta = _without_nul(item.raw_meta)
    bounds = meta.get("boundaries") if isinstance(meta.get("boundaries"), list) else []
    marks = [b[k] for b in bounds for k in ("start", "end")] + list(offsets)
    body, marks = _without_nul(item.body_text, marks)
    body, body_found, marks = redact_moving(body, marks)
    title, title_found = redact(_without_nul(item.title))
    found = sum(body_found.values()) + sum(title_found.values())
    if bounds:
        meta["boundaries"] = [{**b, "start": marks[2 * i], "end": marks[2 * i + 1]} for i, b in enumerate(bounds)]
    if found:
        meta["redactions"] = _count(meta.get("redactions")) + found
        if bounds:
            meta["boundaries_redacted"] = True  # the chunker trusts boundaries that moved with the redactions
    return replace(item, title=title, body_text=body, raw_meta=meta), marks[2 * len(bounds):]


def stored(item: SourceItem) -> SourceItem:
    """The item as it is stored: no NUL characters, no credentials in its title or text (`redact.VERSION` of the
    rules), and its boundaries moved with the text. Removals are added to `raw_meta["redactions"]`."""
    return _store(item, [])[0]


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
INSERT INTO source_items (source, external_id, content_hash, mime, title, body_text, raw_meta, occurred_at,
                          redaction_version)
VALUES (:source, :external_id, :content_hash, :mime, :title, :body_text, CAST(:raw_meta AS jsonb), :occurred_at,
        :redaction_version)
ON CONFLICT (source, external_id) DO UPDATE SET
    content_hash = EXCLUDED.content_hash, mime = EXCLUDED.mime, title = EXCLUDED.title,
    body_text = EXCLUDED.body_text, raw_meta = EXCLUDED.raw_meta, occurred_at = EXCLUDED.occurred_at,
    redaction_version = EXCLUDED.redaction_version, item_class = NULL, updated_at = now()
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
    deletes an item the scan had not reached. `redactions` counts the credentials removed from the items read.
    """
    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0, "redactions": 0}
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
        item = stored(obj)
        row = s.execute(UPSERT, {
            "source": item.source, "external_id": item.external_id, "content_hash": item.content_hash,
            "mime": item.mime, "title": item.title, "body_text": item.body_text,
            "raw_meta": json.dumps(item.raw_meta, default=str), "occurred_at": item.occurred_at,
            "redaction_version": rules.VERSION,
        }).first()
        if row is None:
            stats["unchanged"] += 1
        elif row.inserted:
            stats["inserted"] += 1
        else:
            stats["updated"] += 1
        stats["redactions"] += _count(item.raw_meta.get("redactions"))
        seen += 1
    s.commit()
    return stats


# Offsets into an item's text that the graph stores: the spans of model-derived edges (and of edges the verifier
# dropped, which keep their columns).
_SPANS = text("""
    SELECT 'edges' AS tab, id, span_start, span_end FROM edges WHERE source_item_id = :i AND span_start IS NOT NULL
    UNION ALL
    SELECT 'edges_dropped', id, span_start, span_end FROM edges_dropped WHERE source_item_id = :i AND span_start IS NOT NULL
""")


def redact_stored(s: Session, batch: int = 500) -> dict:
    """Bring items stored under an earlier version of the redaction rules (`redact.VERSION`) up to the current one:
    the pipeline's first step, so the spine and the chunks only ever read redacted text.

    Each item gets the transform `run_sync` gives new ones (`stored`). One whose title or text changes is rewritten
    with its content hash, the spans graph edges hold into its text move with it, nodes named by its old title take
    the new one, and `updated_at` moves so the spine and the chunk stage redo it (its chunks, and so their
    embeddings, are made again); a rebuild is marked in the same transaction. Any other item only records the version
    and the hash of its stored content, so syncing it again reads as unchanged. With every item current, this is one
    index lookup.
    """
    from igneous.graph import dirty

    checked = changed = 0
    after = 0
    while True:
        rows = s.execute(text("""
            SELECT id, source, external_id, title, body_text, mime, occurred_at, raw_meta FROM source_items
            WHERE redaction_version < :v AND id > :after ORDER BY id LIMIT :n
        """), {"v": rules.VERSION, "after": after, "n": batch}).all()
        if not rows:
            break
        current: dict[int, str] = {}  # id -> hash of the items whose stored content stays as it is
        for row in rows:
            old = SourceItem(row.source, row.external_id, row.title, row.body_text, row.occurred_at, row.mime,
                             row.raw_meta or {})
            new = stored(old)
            values = {"i": row.id, "v": rules.VERSION, "h": new.content_hash}
            if (new.title, new.body_text, new.raw_meta) == (old.title, old.body_text, old.raw_meta):
                current[row.id] = new.content_hash
                continue
            if new.body_text != old.body_text and (spans := s.execute(_SPANS, {"i": row.id}).all()):
                _, moved = _store(old, [p for span in spans for p in (span.span_start, span.span_end)])
                for k, span in enumerate(spans):
                    s.execute(text(f"UPDATE {span.tab} SET span_start = :a, span_end = :b WHERE id = :id"),
                              {"a": moved[2 * k], "b": moved[2 * k + 1], "id": span.id})
            if new.title != old.title and old.title:
                s.execute(text("UPDATE nodes SET name = :new WHERE source_item_id = :i AND name = :old"),
                          {"i": row.id, "new": new.title or "", "old": old.title})
            s.execute(text("""
                UPDATE source_items SET title = :t, body_text = :b, raw_meta = CAST(:m AS jsonb), content_hash = :h,
                       redaction_version = :v, item_class = NULL, updated_at = now()
                WHERE id = :i"""), {**values, "t": new.title, "b": new.body_text,
                                    "m": json.dumps(new.raw_meta, default=str)})
            changed += 1
        s.execute(text("""
            UPDATE source_items si SET redaction_version = :v, content_hash = u.h
            FROM unnest(CAST(:ids AS bigint[]), CAST(:hashes AS text[])) AS u(id, h) WHERE si.id = u.id
        """), {"v": rules.VERSION, "ids": list(current), "hashes": list(current.values())})
        checked += len(rows)
        after = rows[-1].id
        if changed:
            dirty.mark_rebuild(s)
        s.commit()
    return {"checked": checked, "redacted": changed}
