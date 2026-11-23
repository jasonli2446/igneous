"""The spine: which records it builds, whatever order syncs commit in and wherever a run stops."""
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from igneous.connectors.base import UPSERT, SourceItem
from igneous.db import SessionLocal
from igneous.graph import spine
from igneous.rag import chunking

T0 = datetime(2025, 1, 6, 9, tzinfo=UTC)


def put(s, source, external_id, meta, title="x", when=T0, body="body"):
    """Write a record exactly as a connector's sync does (connectors/base.py)."""
    item = SourceItem(source=source, external_id=external_id, title=title, body_text=body, occurred_at=when,
                      raw_meta=meta)
    s.execute(UPSERT, {"source": source, "external_id": external_id, "content_hash": item.content_hash, "mime": None,
                       "title": title, "body_text": body, "raw_meta": json.dumps(meta), "occurred_at": when})
    return s.execute(text("SELECT id FROM source_items WHERE source = :s AND external_id = :e"),
                     {"s": source, "e": external_id}).scalar_one()


def mail(s, n, frm, to, title="Plans", when=None, body="body", **meta):
    """An Enron-style message: no reply headers; `n` names the record and, unless given, the fingerprint."""
    meta = {"from": [{"email": frm}], "to": [{"email": t} for t in to], "fingerprint": f"fp-{n}", **meta}
    return put(s, "email", f"box/inbox/{n}", meta, title, when or T0 + timedelta(hours=n), body)


def keys(s, type_):
    return set(s.execute(text("SELECT key FROM nodes WHERE type = :t"), {"t": type_}).scalars())


# --- which records ----------------------------------------------------------------------------------------------------

def test_syncs_that_commit_out_of_order_are_both_built(committed_db):
    """A sync that started first but commits last is still built: nothing depends on when a transaction began."""
    with SessionLocal() as slow, SessionLocal() as fast:
        mail(slow, 1, "a@x.test", ["b@x.test"], "From the slow sync")       # its transaction stays open
        time.sleep(0.05)
        mail(fast, 2, "c@x.test", ["b@x.test"], "From the fast sync")
        fast.commit()
        assert spine.run(committed_db) == 1 and chunking.run(committed_db) == 1
        slow.commit()
    assert spine.run(committed_db) == 1 and chunking.run(committed_db) == 1
    assert keys(committed_db, "Message") == {"fp-1", "fp-2"}
    assert committed_db.execute(text("SELECT count(DISTINCT source_item_id) FROM chunks")).scalar() == 2


def test_a_change_made_while_the_spine_reads_a_record_is_built_too(committed_db, monkeypatch):
    rid = mail(committed_db, 1, "a@x.test", ["b@x.test"], "Draft", fingerprint="fp")
    committed_db.commit()
    build = spine.build_email

    def edited_meanwhile(b, item):
        if item.title == "Draft":  # a sync rewrites the record after the spine read it, before it is marked
            with SessionLocal() as sync:
                mail(sync, 1, "a@x.test", ["b@x.test"], "Final version", fingerprint="fp")
                sync.commit()
        build(b, item)

    monkeypatch.setattr(spine, "build_email", edited_meanwhile)
    assert spine.run(committed_db) == 2   # built twice: what it read, then what changed
    assert committed_db.execute(text("SELECT name FROM nodes WHERE key = 'fp'")).scalar() == "Final version"
    assert committed_db.execute(text("SELECT spined_hash = content_hash FROM source_items WHERE id = :i"),
                                {"i": rid}).scalar()


def test_a_run_that_stops_leaves_every_record_it_did_not_commit_pending(db, monkeypatch):
    monkeypatch.setattr(spine, "BATCH", 5)
    for n in range(12):  # one sync: every record has the same updated_at
        mail(db, n, f"p{n}@x.test", ["me@x.test"], f"Note {n}")
    build = spine.build_email

    def crash(b, item):
        if item.title == "Note 7":
            raise RuntimeError("the process stopped")
        build(b, item)

    monkeypatch.setattr(spine, "build_email", crash)
    with pytest.raises(RuntimeError):
        spine.run(db)
    db.rollback()
    assert len(keys(db, "Message")) == 5   # the first batch committed
    monkeypatch.setattr(spine, "build_email", build)
    assert spine.run(db) == 7
    assert len(keys(db, "Message")) == 12
