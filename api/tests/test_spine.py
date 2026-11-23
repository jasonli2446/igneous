"""The spine: which records it builds, what each record contributed, what goes when a record changes or goes, the
replies it derives, and an incremental build equal to a build at once."""
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from igneous.connectors.base import UPSERT, SourceItem
from igneous.db import SessionLocal
from igneous.graph import invariants, quoted, spine
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


def gone(s, source, external_id):
    s.execute(text("DELETE FROM source_items WHERE source = :s AND external_id = :e"), {"s": source, "e": external_id})


def keys(s, type_):
    return set(s.execute(text("SELECT key FROM nodes WHERE type = :t"), {"t": type_}).scalars())


def edges(s, type_):
    return set(s.execute(text("""
        SELECT a.key, b.key FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst WHERE e.type = :t
    """), {"t": type_}).all())


def legal(s):
    from igneous.graph import resolve

    resolve.refresh_canonical(s, commit=False)
    assert [v.as_dict() for v in invariants.check_graph(s)] == []


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
    build = spine.BUILDERS["email"]

    def edited_meanwhile(b, item):
        if item.title == "Draft":  # a sync rewrites the record after the spine read it, before it is marked
            with SessionLocal() as sync:
                mail(sync, 1, "a@x.test", ["b@x.test"], "Final version", fingerprint="fp")
                sync.commit()
        build(b, item)

    monkeypatch.setitem(spine.BUILDERS, "email", edited_meanwhile)
    assert spine.run(committed_db) == 2   # built twice: what it read, then what changed
    assert committed_db.execute(text("SELECT name FROM nodes WHERE key = 'fp'")).scalar() == "Final version"
    assert committed_db.execute(text("SELECT spined_hash = content_hash FROM source_items WHERE id = :i"),
                                {"i": rid}).scalar()


def test_a_run_that_stops_leaves_every_record_it_did_not_commit_pending(db, monkeypatch):
    monkeypatch.setattr(spine, "BATCH", 5)
    for n in range(12):  # one sync: every record has the same updated_at
        mail(db, n, f"p{n}@x.test", ["me@x.test"], f"Note {n}")
    build = spine.BUILDERS["email"]

    def crash(b, item):
        if item.title == "Note 7":
            raise RuntimeError("the process stopped")
        build(b, item)

    monkeypatch.setitem(spine.BUILDERS, "email", crash)
    with pytest.raises(RuntimeError):
        spine.run(db)
    db.rollback()
    assert len(keys(db, "Message")) == 5   # the first batch committed
    monkeypatch.setitem(spine.BUILDERS, "email", build)
    assert spine.run(db) == 7
    assert len(keys(db, "Message")) == 12


def test_classes_are_committed_with_their_nodes(db, monkeypatch):
    """A batch's messages are classified in the transaction that builds them, so no reader sees one unclassified."""
    monkeypatch.setattr(spine, "BATCH", 2)
    mail(db, 1, "news@shop.test", ["me@x.test"], "Sale", body="unsubscribe here")
    mail(db, 2, "ann@x.test", ["me@x.test"], "Lunch")
    mail(db, 3, "bo@x.test", ["me@x.test"], "Later")
    build = spine.BUILDERS["email"]

    def crash(b, item):
        if item.title == "Later":
            raise RuntimeError("the process stopped")
        build(b, item)

    monkeypatch.setitem(spine.BUILDERS, "email", crash)
    with pytest.raises(RuntimeError):
        spine.run(db)
    db.rollback()
    assert db.execute(text("SELECT props->>'item_class', props->>'hidden' FROM nodes WHERE key = 'fp-1'")).one() \
        == ("marketing", "true")


def test_records_are_read_once(db, monkeypatch):
    put(db, "fs", "home/notes.txt", {"path": "home/notes.txt"}, "notes.txt")   # not communication: no class
    mail(db, 1, "a@x.test", ["b@x.test"])
    assert spine.run(db) == 2
    read = []
    monkeypatch.setattr(spine, "_process", lambda s, rows, cards: read.extend(rows))
    assert spine.run(db) == 0 and read == []


def test_every_stored_source_has_a_builder_or_builds_nothing_on_purpose():
    from igneous.source_catalog import CATALOG

    assert {s.value for s in CATALOG} == set(spine.BUILDERS)


# --- what each record contributed ------------------------------------------------------------------------------------

def occurrence(s, n, attendees, title="Design review", series="uid-series@google.com"):
    meta = {"event_key": f"gcal:{series}", "series_key": f"acct/cal/{series}", "recurring": True,
            "attendees": [{"email": a, "response": "accepted"} for a in attendees]}
    return put(s, "gcal", f"acct/cal/{series}_{n}", meta, title, T0 + timedelta(days=7 * n))


def attended(s):
    return {(p, f.date().isoformat(), t.date().isoformat()) for p, f, t in s.execute(text("""
        SELECT p.key, e.valid_from, e.valid_to FROM edges e JOIN nodes p ON p.id = e.src WHERE e.type = 'ATTENDED'
    """))}


def test_a_series_spans_its_occurrences_and_outlives_a_cancelled_one(db):
    first = occurrence(db, 1, ["ann@x.test", "bo@x.test"])
    occurrence(db, 2, ["ann@x.test", "bo@x.test"])
    occurrence(db, 3, ["ann@x.test"])
    spine.run(db)
    assert keys(db, "Event") == {"gcal:uid-series@google.com"}
    assert attended(db) == {("ann@x.test", "2025-01-13", "2025-01-27"), ("bo@x.test", "2025-01-13", "2025-01-20")}
    gone(db, "gcal", "acct/cal/uid-series@google.com_1")   # the first occurrence is cancelled
    spine.run(db)
    assert keys(db, "Event") == {"gcal:uid-series@google.com"}
    assert attended(db) == {("ann@x.test", "2025-01-20", "2025-01-27"), ("bo@x.test", "2025-01-20", "2025-01-20")}
    event = db.execute(text("SELECT source_item_id FROM nodes WHERE type = 'Event'")).scalar()
    assert event not in (first, None)
    gone(db, "gcal", "acct/cal/uid-series@google.com_2")
    spine.run(db)
    assert attended(db) == {("ann@x.test", "2025-01-27", "2025-01-27")}
    assert keys(db, "Person") == {"ann@x.test"}   # bo attended nothing that remains
    gone(db, "gcal", "acct/cal/uid-series@google.com_3")
    spine.run(db)
    assert keys(db, "Event") == set() and keys(db, "Person") == set()
    legal(db)


def test_an_update_replaces_exactly_what_the_record_built(db):
    occurrence(db, 1, ["ann@x.test", "bo@x.test"], series="offsite")
    put(db, "gcontacts", "acct/people/1", {"emails": ["jo@x.test"], "phones": ["614 555 0100", "614 555 0199"]},
        "Jo Park")
    put(db, "drive", "acct/f1", {"drive_id": "f1", "path": "drive/f1", "parents": ["old"],
                                 "parent_names": {"old": "Archive"}}, "plan.pdf")
    spine.run(db)
    assert len(edges(db, "SAME_AS")) == 2   # one card: its address and its two numbers
    occurrence(db, 1, ["ann@x.test"], series="offsite")                                   # bo removed
    put(db, "gcontacts", "acct/people/1", {"emails": ["jo@x.test"], "phones": ["614 555 0100"]}, "Jo Park")
    put(db, "drive", "acct/f1", {"drive_id": "f1", "path": "drive/f1", "parents": ["new"],
                                 "parent_names": {"new": "Current"}}, "plan.pdf")        # moved
    spine.run(db)
    assert attended(db) == {("ann@x.test", "2025-01-13", "2025-01-13")}
    assert keys(db, "Person") == {"ann@x.test", "jo@x.test", "+16145550100"}
    assert len(edges(db, "SAME_AS")) == 1
    assert edges(db, "CONTAINS") == {("drive:drive/new", "drive:f1")}
    assert keys(db, "Folder") == {"drive:drive/new"}
    assert db.execute(text("SELECT name FROM nodes WHERE type = 'Folder'")).scalar() == "Current"
    legal(db)


def test_a_shared_node_stays_while_any_record_supports_it(db):
    # One email filed in two folders (one fingerprint); a person in two messages; a Drive file two accounts see.
    a = mail(db, 1, "ann@x.test", ["bo@x.test"], "Budget", fingerprint="fp-budget")
    b = put(db, "email", "box/sent/1", {"from": [{"email": "ann@x.test"}], "to": [{"email": "bo@x.test"}],
                                       "fingerprint": "fp-budget"}, "Budget", T0 + timedelta(hours=1))
    mail(db, 2, "ann@x.test", ["cy@x.test"], "Lunch")
    for account in ("a@x.test", "b@x.test"):
        put(db, "drive", f"{account}/f1", {"drive_id": "f1", "path": "drive/f1", "parents": []}, "plan.pdf")
    spine.run(db)
    chunking.run(db)
    assert db.execute(text("SELECT source_item_id FROM nodes WHERE key = 'fp-budget'")).scalar() == a
    gone(db, "email", "box/inbox/1")
    gone(db, "drive", "a@x.test/f1")
    spine.run(db)
    assert {"fp-budget", "fp-2"} == keys(db, "Message") and keys(db, "File") == {"drive:f1"}
    assert ("ann@x.test", "fp-budget") in edges(db, "SENT") and ("fp-budget", "bo@x.test") in edges(db, "RECEIVED")
    # The message is read from its other copy now, and that copy is chunked in its place.
    assert db.execute(text("SELECT source_item_id FROM nodes WHERE key = 'fp-budget'")).scalar() == b
    assert chunking.run(db) == 2
    assert db.execute(text("SELECT count(*) FROM chunks WHERE source_item_id = :b"), {"b": b}).scalar() == 1
    gone(db, "email", "box/inbox/2")
    spine.run(db)
    assert keys(db, "Person") == {"ann@x.test", "bo@x.test"}   # cy was only in the lunch message
    legal(db)


def test_garbage_collection_keeps_the_owner_user_facts_and_other_stages_rows(db):
    put(db, "imessage", "c1/2025-01-06", {"chat": "c1", "participants": ["+16145550100"], "senders": ["me"]}, "Pat")
    mail(db, 1, "ann@x.test", ["bo@x.test"], "Kickoff")
    put(db, "linkedin", "connection/kim", {"kind": "connection", "name": "Kim Wu", "company": "Globex",
                                           "url": "https://www.linkedin.com/in/kim"}, "Kim Wu")
    spine.run(db)
    ids = dict(db.execute(text("SELECT key, id FROM nodes")).all())
    project = db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Project', 'project:kickoff', 'Kickoff') "
                              "RETURNING id")).scalar_one()
    db.execute(text("""INSERT INTO edges (src, dst, type, extractor) VALUES
                       (:ann, :p, 'INVOLVED_IN', 'user'), (:m, :p, 'PART_OF', 'entities'),
                       (:bo, :org, 'WORKED_AT', 'organizations')"""),
               {"ann": ids["ann@x.test"], "bo": ids["bo@x.test"], "m": ids["fp-1"], "p": project,
                "org": ids["org:globex"]})
    for source, external_id in (("imessage", "c1/2025-01-06"), ("email", "box/inbox/1"),
                                ("linkedin", "connection/kim")):
        gone(db, source, external_id)
    spine.run(db)
    left = keys(db, "Person") | keys(db, "Organization") | keys(db, "Project") | keys(db, "Message")
    # The owner stays, Ann has a fact you stated, and another stage still uses Bo and Globex. A message is its record:
    # it goes with it, and so does what another stage derived from it (PART_OF).
    assert left == {"me", "ann@x.test", "bo@x.test", "org:globex", "project:kickoff"}
    assert keys(db, "Thread") == set() and edges(db, "PART_OF") == set()
    db.execute(text("DELETE FROM edges WHERE extractor = 'organizations'"))   # that stage rebuilt without them
    spine.run(db)
    assert keys(db, "Person") | keys(db, "Organization") == {"me", "ann@x.test"}


def test_a_removed_linkedin_connection_takes_only_what_it_contributed(db):
    mail(db, 1, "sam@acme.test", ["me@x.test"], "Hello")
    put(db, "linkedin", "connection/sam-lee", {"kind": "connection", "name": "Sam Lee", "email": "sam@acme.test",
                                               "url": "https://www.linkedin.com/in/sam-lee", "company": "Globex"},
        "Sam Lee")
    spine.run(db)
    assert edges(db, "MEMBER_OF") == {("sam@acme.test", "org:globex")}   # keyed by the company, not acme.test
    assert ("linkedin:sam-lee", "sam@acme.test") in edges(db, "SAME_AS") | {
        (b, a) for a, b in edges(db, "SAME_AS")}
    gone(db, "linkedin", "connection/sam-lee")
    spine.run(db)
    assert keys(db, "Person") == {"sam@acme.test", "me@x.test"} and keys(db, "Organization") == set()
    assert edges(db, "SAME_AS") == set() and edges(db, "SENT") == {("sam@acme.test", "fp-1")}
    legal(db)


def test_names_and_props_from_every_record_of_a_batch_are_kept(db):
    """A later record's name and props for a node built earlier in the same batch are not dropped."""
    mail(db, 1, "jane@acme.test", ["me@x.test"], "Hello")   # names her "Jane" from her address first
    put(db, "linkedin", "connection/jane-roe", {"kind": "connection", "name": "Jane Roe", "email": "jane@acme.test",
                                                "url": "https://www.linkedin.com/in/jane-roe", "position": "CTO"},
        "Jane Roe")
    spine.run(db)
    name, props = db.execute(text("SELECT name, props FROM nodes WHERE key = 'jane@acme.test'")).one()
    assert name == "Jane Roe" and props["position"] == "CTO" and props["named_by"] == "linkedin"


def test_a_shared_identifier_on_two_peoples_cards_belongs_to_neither(db):
    home = {"phones": ["(614) 555-0100"]}
    put(db, "gcontacts", "acct/people/1", {"emails": ["ann@x.test"], **home}, "Ann Lee")
    put(db, "gcontacts", "acct/people/2", {"emails": ["bo@x.test"], **home}, "Bo Lee")
    spine.run(db)
    assert edges(db, "SAME_AS") == set()   # the home phone joins neither card's identities
    assert db.execute(text("SELECT name, props->>'named_by' FROM nodes WHERE key = '+16145550100'")).one() \
        == ("+16145550100", None)
    gone(db, "gcontacts", "acct/people/2")   # now only Ann's card carries it
    spine.run(db)
    assert db.execute(text("SELECT name, props->>'named_by' FROM nodes WHERE key = '+16145550100'")).one() \
        == ("Ann Lee", "contacts")
    assert len(edges(db, "SAME_AS")) == 1
    put(db, "gcontacts", "acct/people/3", {"emails": ["cy@x.test"], **home}, "Cy Lee")   # and now Cy's too
    spine.run(db)
    assert edges(db, "SAME_AS") == set()
    assert db.execute(text("SELECT props->>'named_by' FROM nodes WHERE key = '+16145550100'")).scalar() is None
    legal(db)


def test_a_nameless_card_names_nobody(db):
    put(db, "gcontacts", "acct/people/1", {"emails": ["jo@x.test", "jo@home.test"]}, "jo@x.test")
    spine.run(db)
    assert set(db.execute(text("SELECT name FROM nodes WHERE type = 'Person'")).scalars()) == {"jo@x.test",
                                                                                               "jo@home.test"}
    assert len(edges(db, "SAME_AS")) == 1   # still one person's two addresses


# --- replies -----------------------------------------------------------------------------------------------------------

def gmail(s, n, frm, to, title, **meta):
    return put(s, "gmail", f"m{n}", {"from": [{"email": frm}], "to": [{"email": t} for t in to],
                                     "fingerprint": f"fp-{n}", "message_id": f"<{n}@x.test>",
                                     "gmail_thread_id": "t1", **meta}, title, T0 + timedelta(hours=n))


def test_in_reply_to_links_the_ingested_parent_whenever_it_arrives(db):
    gmail(db, 2, "bo@x.test", ["ann@x.test"], "Re: Plans", in_reply_to="<1@x.test>")
    gmail(db, 3, "ann@x.test", ["bo@x.test"], "Re: Plans", in_reply_to="<2@x.test>")
    spine.run(db)
    assert edges(db, "REPLIES_TO") == {("fp-3", "fp-2")}   # the first message isn't here yet: no placeholder
    gmail(db, 1, "ann@x.test", ["bo@x.test"], "Plans")
    spine.run(db)
    assert edges(db, "REPLIES_TO") == {("fp-3", "fp-2"), ("fp-2", "fp-1")}
    assert keys(db, "Message") == {"fp-1", "fp-2", "fp-3"}
    legal(db)


def quoting(s, n, when, quoted_sent="Mon 8/20/2001 10:20 AM"):
    body = (f"Sounds good.\n\n -----Original Message-----\nFrom: Rice, Ken \nSent: {quoted_sent} \n"
            "To: Skilling, Jeff \nSubject: RE: Astros tickets\n\nThanks for the tickets.")
    return mail(s, n, "jeff.skilling@x.test", ["ken.rice@x.test"], "RE: Astros tickets", when=when, body=body)


def ken(s, n, when):
    return put(s, "email", f"box/ken/{n}", {"from": [{"email": "ken.rice@x.test"}], "to": [{"email": "jeff@x.test"}],
                                           "fingerprint": f"ken-{n}", "x_from": "Rice, Ken"}, "Astros tickets", when)


def quoted_replies(s):
    return {(c, p) for c, p in s.execute(text("""
        SELECT a.key, b.key FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst
        WHERE e.type = 'REPLIES_TO' AND e.extractor = :x"""), {"x": quoted.EXTRACTOR})}


def test_a_quote_links_the_one_message_it_names_and_is_replaced_when_that_changes(db):
    sent = datetime(2001, 8, 20, 15, 20, tzinfo=UTC)   # 10:20 AM in Houston (UTC-5)
    quoting(db, 1, sent + timedelta(hours=3))
    spine.run(db)
    assert quoted_replies(db) == set()                  # its parent isn't here yet
    ken(db, 1, sent)
    spine.run(db)
    assert quoted_replies(db) == {("fp-1", "ken-1")}    # linked when the parent arrives
    ken(db, 2, sent + timedelta(hours=2))               # 10:20 AM in Los Angeles: just as good a match
    spine.run(db)
    assert quoted_replies(db) == set()                  # two candidates: no link rather than a guess
    gone(db, "email", "box/ken/2")
    spine.run(db)
    assert quoted_replies(db) == {("fp-1", "ken-1")}
    legal(db)


def test_inferred_replies_are_the_same_built_at_once_or_in_steps(db):
    """Within a thread, a "Re:" answers the latest earlier message from someone else, even one that answers another."""
    mail(db, 1, "alice@x.test", ["bob@x.test"], "Budget")
    mail(db, 2, "bob@x.test", ["alice@x.test"], "Re: Budget")
    mail(db, 3, "alice@x.test", ["bob@x.test"], "Re: Budget")
    spine.run(db)
    at_once = edges(db, "REPLIES_TO")
    db.execute(text("TRUNCATE source_items, nodes, edges, spine_gc_nodes, spine_gc_edges RESTART IDENTITY CASCADE"))
    mail(db, 1, "alice@x.test", ["bob@x.test"], "Budget")
    mail(db, 2, "bob@x.test", ["alice@x.test"], "Re: Budget")
    spine.run(db)
    mail(db, 3, "alice@x.test", ["bob@x.test"], "Re: Budget")
    spine.run(db)
    assert edges(db, "REPLIES_TO") == at_once == {("fp-2", "fp-1"), ("fp-3", "fp-2")}


# --- incremental equals at once --------------------------------------------------------------------------------------

CORPUS = [
    ("email", "box/inbox/1", {"from": [{"email": "ann@x.test", "name": "Ann Lee"}], "to": [{"email": "bo@x.test"}],
                              "cc": [{"email": "cy@x.test"}], "fingerprint": "fp-1"}, "Budget", 1, "first"),
    ("email", "box/sent/1", {"from": [{"email": "ann@x.test"}], "to": [{"email": "bo@x.test"}],
                             "cc": [{"email": "cy@x.test"}], "fingerprint": "fp-1"}, "Budget", 1, "first"),
    ("email", "box/inbox/2", {"from": [{"email": "bo@x.test"}], "to": [{"email": "ann@x.test"}],
                              "cc": [{"email": "cy@x.test"}], "fingerprint": "fp-2"}, "Re: Budget", 2, "ok"),
    ("email", "box/inbox/3", {"from": [{"email": "ann@x.test"}], "to": [{"email": "bo@x.test"}],
                              "cc": [{"email": "cy@x.test"}], "fingerprint": "fp-3"}, "Re: Budget", 3,
     "Done.\n -----Original Message-----\nFrom: Bo, Bob \nSent: Mon 1/6/2025 6:00 AM \nSubject: Re: Budget\n\nok"),
    ("gmail", "g1", {"from": [{"email": "dee@x.test", "name": "Dee"}], "to": [{"email": "ann@x.test"}],
                     "message_id": "<g1@x.test>", "fingerprint": "fp-g1", "gmail_thread_id": "t1",
                     "in_reply_to": "<g0@x.test>"}, "Re: Trip", 4, "sure"),
    ("gmail", "g0", {"from": [{"email": "ann@x.test"}], "to": [{"email": "dee@x.test", "name": "Dee Dee Ray"}],
                     "message_id": "<g0@x.test>", "fingerprint": "fp-g0", "gmail_thread_id": "t1",
                     "labels": ["SENT"]}, "Trip", 3, "go?"),
    ("gcal", "acct/cal/s_1", {"event_key": "gcal:s", "recurring": True,
                              "attendees": [{"email": "ann@x.test", "response": "accepted"},
                                            {"email": "bo@x.test", "response": "declined"}]}, "Standup", 5, ""),
    ("gcal", "acct/cal/s_2", {"event_key": "gcal:s", "recurring": True,
                              "attendees": [{"email": "ann@x.test", "response": "accepted"},
                                            {"email": "bo@x.test", "response": "accepted"}]}, "Standup", 30, ""),
    ("gcontacts", "acct/people/1", {"emails": ["bo@x.test"], "phones": ["6145550100"]}, "Bob Bo", None, ""),
    ("linkedin", "connection/cy", {"kind": "connection", "name": "Cy Young", "email": "cy@x.test",
                                   "url": "https://www.linkedin.com/in/cy", "company": "Globex Inc"}, "Cy Young", None,
     ""),
    ("imessage", "c1/2025-01-07", {"chat": "c1", "participants": ["6145550100"], "senders": ["6145550100", "me"]},
     "Bob Bo", 24, "hi"),
    ("drive", "a@x.test/f1", {"drive_id": "f1", "path": "drive/f1", "parents": ["p"]}, "plan.pdf", 6, "plan"),
    ("drive", "b@x.test/f1", {"drive_id": "f1", "path": "drive/f1", "parents": ["p"],
                              "parent_names": {"p": "Plans"}}, "plan.pdf", 6, "plan"),
    # A reply ingested before the message it quotes (its sender's folder synced later).
    ("email", "box/inbox/9", {"from": [{"email": "ann@x.test"}], "to": [{"email": "dee@x.test"}],
                              "fingerprint": "fp-9"}, "RE: Lunch", 31,
     "Yes!\n -----Original Message-----\nFrom: Ray, Dee \nSent: Tue 1/7/2025 9:00 AM \nSubject: Lunch\n\nNoon?"),
    ("email", "box/dee/1", {"from": [{"email": "dee@x.test"}], "to": [{"email": "ann@x.test"}],
                            "fingerprint": "fp-8", "x_from": "Ray, Dee"}, "Lunch", 29, "Noon?"),
]


def snapshot(s) -> dict:
    return {
        "nodes": sorted(s.execute(text("SELECT type, key, name, props::text FROM nodes")).all()),
        "edges": sorted(s.execute(text("""
            SELECT a.key, b.key, e.type, e.extractor, e.valid_from, e.valid_to, e.confidence
            FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst""")).all()),
        "items": sorted(s.execute(text("SELECT external_id, item_class FROM source_items")).all()),
    }


def build(s, steps):
    s.execute(text("TRUNCATE source_items, nodes, edges, spine_gc_nodes, spine_gc_edges RESTART IDENTITY CASCADE"))
    for step in steps:
        for source, external_id, meta, title, hours, body in step:
            put(s, source, external_id, meta, title, None if hours is None else T0 + timedelta(hours=hours), body)
        spine.run(s)
    legal(s)
    return snapshot(s)


def test_an_incremental_build_equals_one_build_of_the_same_records(db, monkeypatch):
    at_once = build(db, [CORPUS])
    assert build(db, [CORPUS[:3], CORPUS[3:7], CORPUS[7:]]) == at_once
    assert build(db, [[r] for r in CORPUS]) == at_once
    # What an upgrade does: every record pending again with no support recorded, built in small batches.
    monkeypatch.setattr(spine, "BATCH", 2)  # the last reply and the message it quotes in different batches
    db.execute(text("TRUNCATE spine_node_support, spine_edge_support, spine_quotes"))
    db.execute(text("UPDATE source_items SET spined_hash = NULL"))
    spine.run(db)
    assert snapshot(db) == at_once
    # And what the corpus is meant to show is there: copies are one message, replies of every kind, a series.
    assert {(e[0], e[1], e[3]) for e in at_once["edges"] if e[2] == "REPLIES_TO"} == {
        ("fp-g1", "fp-g0", "spine"), ("fp-2", "fp-1", "spine-inferred"), ("fp-3", "fp-2", "spine-quoted"),
        ("fp-9", "fp-8", "spine-quoted")}
    assert sum(1 for n in at_once["nodes"] if n[0] == "Message") == 8


# --- upgrading a database built before provenance ------------------------------------------------------------------

def test_an_existing_database_records_support_once_and_sheds_what_nothing_supports(test_db, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from conftest import API, TEST_DB
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session

    from igneous.config import settings

    name = f"{TEST_DB}_upgrade"
    url = make_url(settings.database_url).set(database=name).render_as_string(hide_password=False)
    admin = create_engine(make_url(settings.database_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "migrations"))
    monkeypatch.setattr(settings, "database_url", url)  # migrations/env.py connects to settings.database_url
    engine = create_engine(url)
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{name}"'))
        command.upgrade(config, "0008")
        with engine.begin() as c:  # what the watermark-era spine and chunk left behind
            for n, updated in ((1, "2025-01-01"), (2, "2025-03-01")):
                c.execute(text("""
                    INSERT INTO source_items (id, source, external_id, content_hash, title, body_text, raw_meta,
                                              occurred_at, updated_at)
                    VALUES (:n, 'email', :e, :e, 'Plans', 'body', CAST(:m AS jsonb), '2025-01-01', :u)
                """), {"n": n, "e": f"box/inbox/{n}", "u": updated, "m": json.dumps({
                    "from": [{"email": "ann@x.test"}], "to": [{"email": "bo@x.test"}], "fingerprint": f"fp-{n}",
                    **({"in_reply_to": "<lost@x.test>"} if n == 2 else {})})})
            c.execute(text("""
                INSERT INTO source_items (id, source, external_id, content_hash, title, raw_meta, updated_at)
                VALUES (3, 'linkedin', 'connection/cy', 'li', 'Cy Young', CAST(:m AS jsonb), '2025-01-01')
            """), {"m": json.dumps({"kind": "connection", "name": "Cy Young", "email": "cy@globex.test",
                                    "company": "Globex"})})
            c.execute(text("INSERT INTO pipeline_state (stage, watermark) VALUES ('spine', '2025-03-01'), "
                           "('chunk', '2025-02-01')"))
            c.execute(text("""
                INSERT INTO nodes (id, type, key, name, props, source_item_id) VALUES
                    (1, 'Message', 'fp-1', 'Plans', '{}', 1), (2, 'Message', 'fp-2', 'Plans', '{}', 2),
                    (3, 'Message', '<lost@x.test>', '(not ingested)', '{}', NULL),
                    (4, 'Person', 'ann@x.test', 'Ann', '{}', NULL),
                    (5, 'Person', 'cy@globex.test', 'Cy Young', '{}', 3),
                    (6, 'Organization', 'globex.test', 'Globex', '{"source": "linkedin"}', NULL)
            """))
            c.execute(text("""
                INSERT INTO edges (src, dst, type, extractor, source_item_id) VALUES
                    (4, 1, 'SENT', 'spine', 1), (2, 3, 'REPLIES_TO', 'spine', 2), (5, 6, 'MEMBER_OF', 'linkedin', 3)
            """))
            for table in ("source_items", "nodes"):
                c.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT max(id) FROM {table}))"))
        command.upgrade(config, "head")
        with engine.connect() as c:
            # Chunk keeps its progress; every record waits for the spine to record what it supports.
            assert c.execute(text("SELECT external_id, spined_hash, chunked_hash FROM source_items ORDER BY id")).all() \
                == [("box/inbox/1", None, "box/inbox/1"), ("box/inbox/2", None, None), ("connection/cy", None, "li")]
            assert c.execute(text("SELECT count(*) FROM nodes WHERE name = '(not ingested)'")).scalar() == 0
            assert c.execute(text("SELECT to_regclass('pipeline_state')")).scalar() is None
        with Session(engine) as s:
            assert spine.run(s) == 3
            assert keys(s, "Message") == {"fp-1", "fp-2"}
            assert keys(s, "Organization") == {"org:globex"}   # the address-keyed company had no record left
            assert edges(s, "MEMBER_OF") == {("cy@globex.test", "org:globex")}
            assert s.execute(text("SELECT count(*) FROM spine_gc_nodes")).scalar() == 0
            legal(s)
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
