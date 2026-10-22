"""Item classification: pure function, synthetic headers only (no database)."""
from igneous.connectors.email_maildir import parse_message
from igneous.graph.item_class import CONVERSATION, EVENT, MARKETING, NOTIFICATION, classify

ME = frozenset({"me@example.com"})


def mail(frm="friend@example.org", labels=(), to=("me@example.com",), title="Lunch?", body="see you at noon", **meta):
    return classify("gmail", title, {"from": [{"name": "", "email": frm}], "to": [{"email": t} for t in to],
                                     "cc": [], "labels": list(labels), **meta}, body, ME)


def test_gmail_categories():
    assert mail(labels=["CATEGORY_PROMOTIONS"]) == MARKETING
    assert mail(labels=["CATEGORY_UPDATES"], frm="orders@shop.example") == NOTIFICATION
    assert mail(labels=["CATEGORY_SOCIAL"]) == NOTIFICATION
    assert mail(labels=["INBOX", "CATEGORY_PERSONAL"]) == CONVERSATION


def test_small_personal_reply_in_updates_stays_conversation():
    assert mail(labels=["CATEGORY_UPDATES"], in_reply_to="<a@b>") == CONVERSATION
    assert mail(labels=["CATEGORY_UPDATES"], in_reply_to="<a@b>", body="unsubscribe here") == NOTIFICATION


def test_user_authored_is_always_conversation():
    assert mail(labels=["SENT", "CATEGORY_UPDATES"]) == CONVERSATION
    assert mail(frm="me@example.com", labels=["CATEGORY_PROMOTIONS"]) == CONVERSATION


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
    assert classify("imessage", "x", {"participants": ["22000"], "chat": "22000"}, "", ME) == NOTIFICATION
    assert classify("imessage", "x", {"participants": ["+16145551234"]}, "", ME) == CONVERSATION
    assert classify("gcal", "Standup", {}, "", ME) == EVENT
    assert classify("takeout_activity", "Search", {}, "", ME) == NOTIFICATION
    assert classify("takeout_chrome", "Chrome", {}, "", ME) == NOTIFICATION
    assert classify("fs", "notes.md", {}, "", ME) is None


def test_parser_stores_bulk_markers_only_when_present():
    raw = b"From: A <a@x.org>\nTo: b@y.org\nSubject: Hi\nDate: Mon, 1 Jan 2001 10:00:00 +0000\n"
    plain = parse_message(raw + b"\nbody\n", "p").raw_meta
    bulk = parse_message(raw + b"List-Unsubscribe: <mailto:u@x.org>\nPrecedence: Bulk\n\nbody\n", "b").raw_meta
    assert "list_unsubscribe" not in plain and "precedence" not in plain
    assert bulk["list_unsubscribe"] is True and bulk["precedence"] == "bulk"
