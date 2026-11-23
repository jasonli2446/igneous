"""Item classification: the pure classifier on synthetic headers, and its mirror on the graph (thread flags)."""
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from igneous.connectors.email_maildir import parse_message
from igneous.graph import item_class
from igneous.graph.item_class import CONVERSATION, EVENT, MARKETING, NOTIFICATION, classify


def mail(frm="friend@example.org", labels=(), to=("me@example.com",), title="Lunch?", body="see you at noon", **meta):
    return classify("gmail", title, {"from": [{"name": "", "email": frm}], "to": [{"email": t} for t in to],
                                     "cc": [], "labels": list(labels), **meta}, body)


def test_gmail_categories():
    assert mail(labels=["CATEGORY_PROMOTIONS"]) == MARKETING
    assert mail(labels=["CATEGORY_UPDATES"], frm="orders@shop.example") == NOTIFICATION
    assert mail(labels=["CATEGORY_SOCIAL"]) == NOTIFICATION
    assert mail(labels=["INBOX", "CATEGORY_PERSONAL"]) == CONVERSATION


def test_small_personal_reply_in_updates_stays_conversation():
    assert mail(labels=["CATEGORY_UPDATES"], in_reply_to="<a@b>") == CONVERSATION
    assert mail(labels=["CATEGORY_UPDATES"], in_reply_to="<a@b>", body="unsubscribe here") == NOTIFICATION


def test_what_the_record_says_you_wrote_is_always_conversation():
    assert mail(labels=["SENT", "CATEGORY_UPDATES"]) == CONVERSATION
    assert mail(labels=["DRAFT"], body="unsubscribe") == CONVERSATION
    # From the account the message was read from.
    assert mail(frm="me@example.com", labels=["CATEGORY_PROMOTIONS"], account="Me@Example.com") == CONVERSATION
    assert mail(frm="me@example.com", labels=["CATEGORY_PROMOTIONS"]) == MARKETING  # nothing in the record says so


def test_bulk_headers_and_unsubscribe():
    assert mail(list_unsubscribe=True) == MARKETING
    assert mail(precedence="bulk") == MARKETING
    assert mail(body="Great deals! Click to unsubscribe") == MARKETING


def test_service_senders():
    assert mail(frm="noreply@bank.example") == NOTIFICATION
    assert mail(frm="notifications@github.com") == NOTIFICATION
    assert mail(frm="receipts@store.example") == NOTIFICATION


def test_calendar_responses_and_invites():
    for t in ("Accepted: Standup @ Tue", "Declined: 1:1", "Updated invitation: Sync", "Alex has joined your meeting"):
        assert mail(title=t) == NOTIFICATION, t
    assert mail(title="Invitation: Dinner @ Fri") == EVENT


def test_other_sources():
    assert classify("imessage", "x", {"participants": ["22000"], "chat": "22000"}, "") == NOTIFICATION
    assert classify("imessage", "x", {"participants": ["+16145551234"]}, "") == CONVERSATION
    assert classify("gcal", "Standup", {}, "") == EVENT
    assert classify("takeout_activity", "Search", {}, "") == NOTIFICATION
    assert classify("takeout_chrome", "Chrome", {}, "") == NOTIFICATION
    assert classify("fs", "notes.md", {}, "") is None


def test_parser_stores_bulk_markers_only_when_present():
    raw = b"From: A <a@x.org>\nTo: b@y.org\nSubject: Hi\nDate: Mon, 1 Jan 2001 10:00:00 +0000\n"
    plain = parse_message(raw + b"\nbody\n", "p").raw_meta
    bulk = parse_message(raw + b"List-Unsubscribe: <mailto:u@x.org>\nPrecedence: Bulk\n\nbody\n", "b").raw_meta
    assert "list_unsubscribe" not in plain and "precedence" not in plain
    assert bulk["list_unsubscribe"] is True and bulk["precedence"] == "bulk"


# --- the graph mirror ----------------------------------------------------------------------------------------------

T0 = datetime(2025, 1, 6, 9, tzinfo=UTC)


def put(s, source, external_id, title, meta, when, body="hello"):
    s.execute(text("""
        INSERT INTO source_items (source, external_id, content_hash, title, body_text, raw_meta, occurred_at)
        VALUES (:s, :e, :e, :t, :b, CAST(:m AS jsonb), :w)
    """), {"s": source, "e": external_id, "t": title, "b": body, "m": json.dumps(meta), "w": when})


def email(s, n, frm, to, title, body="hello", subject_thread=True, **meta):
    put(s, "email", f"box/inbox/{n}", title, {"from": [{"email": frm}], "to": [{"email": t} for t in to],
                                              "fingerprint": f"fp-{n}", **meta}, T0 + timedelta(hours=n), body)


def flags(s, title):
    return s.execute(text("SELECT props->>'conversational', props->>'hidden' FROM nodes WHERE type = 'Thread' "
                          "AND name = :n"), {"n": title}).one()


def message(s, title):
    return s.execute(text("SELECT props->>'item_class', props->>'hidden' FROM nodes WHERE type = 'Message' "
                          "AND name = :n"), {"n": title}).one()


def test_classes_and_flags_are_written_with_the_nodes(db):
    from igneous.graph import spine

    email(db, 1, "news@shop.test", ["me@x.test"], "Big sale", body="Click to unsubscribe")
    email(db, 2, "ann@x.test", ["me@x.test", "bo@x.test"], "Offsite")
    email(db, 3, "bo@x.test", ["me@x.test", "ann@x.test"], "Re: Offsite")
    spine.run(db)
    assert db.execute(text("SELECT external_id, item_class FROM source_items ORDER BY id")).all() == [
        ("box/inbox/1", MARKETING), ("box/inbox/2", CONVERSATION), ("box/inbox/3", CONVERSATION)]
    assert message(db, "Big sale") == (MARKETING, "true")
    assert flags(db, "Offsite") == ("true", "false")        # two conversation messages, three people
    assert message(db, "Re: Offsite") == (CONVERSATION, "false")


def test_a_github_group_thread_counts_everyone_who_wrote_in_it(db):
    from igneous.graph import spine

    for n, login in enumerate(["sam", "pat", "lee"]):
        put(db, "github", f"me/repo/issue/7/{n}", "Flaky test", {"repo": "me/repo", "kind": "issue", "number": 7,
                                                                 "author": {"login": login}}, T0 + timedelta(hours=n))
    spine.run(db)
    # Nobody RECEIVES GitHub activity: the thread's people are its writers.
    assert flags(db, "Flaky test") == ("true", "false")


def test_your_own_messages_and_the_threads_you_answered_count_as_conversations(db):
    from igneous.graph import spine

    email(db, 1, "alerts@bank.test", ["me@x.test"], "Statement ready")  # a notification nobody answered
    email(db, 2, "alerts@bank.test", ["me@x.test"], "Card locked")
    spine.run(db)
    assert flags(db, "Statement ready") == ("false", "true")
    me = db.execute(text("INSERT INTO nodes (type, key, name, props) VALUES ('Person', 'me', 'Me', '{\"self\": true}') "
                         "RETURNING id")).scalar_one()
    you = db.execute(text("SELECT id FROM nodes WHERE key = 'me@x.test'")).scalar_one()
    db.execute(text("INSERT INTO edges (src, dst, type, extractor) VALUES (:a, :b, 'SAME_AS', 'self')"),
               {"a": min(me, you), "b": max(me, you)})
    email(db, 3, "me@x.test", ["alerts@bank.test"], "Re: Card locked")   # you wrote back
    spine.run(db)
    assert message(db, "Re: Card locked") == (CONVERSATION, "false")
    assert flags(db, "Card locked")[0] == "true"                         # answered: a conversation, though 1:1
    # Recomputed for every thread once your identity is known (the stage after resolve).
    assert item_class.refresh_thread_flags(db) == 0                      # already current
