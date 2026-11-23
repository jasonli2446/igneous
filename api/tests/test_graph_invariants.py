import json
from contextlib import nullcontext
from datetime import UTC, datetime

from sqlalchemy import text
from typer.testing import CliRunner

from igneous.graph import invariants

WHEN = datetime(2025, 3, 1, 12, tzinfo=UTC)


def test_endpoint_violations_reject_illegal_and_derived_edges():
    violations = invariants.endpoint_violations([
        {"id": 1, "type": "SENT", "src_type": "Person", "dst_type": "Message"},
        {"id": 2, "type": "RECEIVED", "src_type": "Person", "dst_type": "Message"},
        {"id": 3, "type": "CORRESPONDS", "src_type": "Person", "dst_type": "Person"},
        {"id": 4, "type": "RELATED_TO", "src_type": "Topic", "dst_type": "Event"},
    ])

    assert [(v.code, v.edge_id) for v in violations] == [
        ("illegal_edge_endpoints", 2),
        ("unknown_edge_type", 3),
    ]
    assert violations[1].message == "CORRESPONDS is not a persisted edge type"


def test_message_participants_come_from_each_records_own_headers():
    mail = {"from": [{"email": "a@x.test"}], "to": [{"email": "b@x.test"}], "cc": [{"email": "c@x.test"}]}
    violations = invariants.message_cardinality_violations([
        {"id": 10, "source": "email", "metas": [mail], "sent": ["a@x.test"], "received": ["b@x.test", "c@x.test"]},
        # Mail with no To or Cc has no recipient: legal, not a violation.
        {"id": 11, "source": "email", "metas": [{"from": [{"email": "a@x.test"}]}], "sent": ["a@x.test"],
         "received": []},
        {"id": 12, "source": "email", "metas": [mail], "sent": [], "received": ["b@x.test"]},
        {"id": 13, "source": "imessage", "metas": [{"participants": ["6145551234"], "senders": ["6145551234", "me"]}],
         "sent": ["+16145551234", "me"], "received": ["+16145551234", "me"]},
        # An AI session is you talking to an assistant: nobody receives it. GitHub activity isn't checked.
        {"id": 14, "source": "ai_chats", "metas": [{}], "sent": ["me"], "received": []},
        {"id": 15, "source": "github", "metas": [{"author": {"login": "sam"}}], "sent": ["github:sam", "github:pat"],
         "received": []},
        {"id": 16, "source": "email", "metas": None, "sent": [], "received": []},  # a phantom, reported elsewhere
    ])

    assert [(v.code, v.node_id) for v in violations] == [
        ("message_sent_cardinality", 12),
        ("message_received_cardinality", 12),
    ]
    assert violations[0].details == {"missing": ["a@x.test"], "unexpected": []}
    assert violations[1].details == {"missing": ["c@x.test"], "unexpected": []}


def test_edges_the_code_writes_on_purpose_are_legal():
    rows = [{"id": i, "type": t, "src_type": a, "dst_type": b} for i, (t, a, b) in enumerate([
        ("INTRODUCED", "Person", "Person"), ("HAS_COMMITMENT", "Message", "Commitment"),
        ("HAS_DECISION", "Message", "Decision"), ("CONTAINS", "Folder", "Message"),
        ("PART_OF", "Project", "Organization"),
    ])]
    assert invariants.endpoint_violations(rows) == []


def _put(s, source, external_id, title, meta, when=WHEN, body="body"):
    return s.execute(text("""
        INSERT INTO source_items (source, external_id, content_hash, title, body_text, raw_meta, occurred_at)
        VALUES (:s, :e, :e, :t, :b, CAST(:m AS jsonb), :w) RETURNING id"""),
        {"s": source, "e": external_id, "t": title, "b": body, "m": json.dumps(meta), "w": when}).scalar_one()


def test_a_spine_build_of_every_message_source_passes_check_graph(db):
    """The checker and the spine agree: one record of each kind, built for real, is a legal graph, including mail
    nobody was addressed to."""
    from igneous.graph import resolve, self_identity, spine

    items = [
        ("gmail", "g1", "Plans", {"from": [{"email": "sam@x.test", "name": "Sam"}], "to": [{"email": "me@x.test"}],
                                  "message_id": "<g1@x.test>", "gmail_thread_id": "t1", "fingerprint": "fp-g1"}),
        ("gmail", "g2", "Re: Plans", {"from": [{"email": "me@x.test"}], "to": [{"email": "sam@x.test"}],
                                      "message_id": "<g2@x.test>", "in_reply_to": "<g1@x.test>",
                                      "gmail_thread_id": "t1", "fingerprint": "fp-g2", "labels": ["SENT"]}),
        ("email", "box/inbox/1", "Re: Plans", {"from": [{"email": "me@x.test"}], "to": [{"email": "sam@x.test"}],
                                              "message_id": "<e1@x.test>", "fingerprint": "fp-e1"}),
        ("email", "box/inbox/2", "Memo", {"from": [{"email": "sam@x.test"}], "message_id": "<e2@x.test>",
                                         "fingerprint": "fp-e2"}),
        ("imessage", "c1/2025-03-01", "Pat", {"chat": "c1", "participants": ["+15550001111"],
                                              "senders": ["+15550001111", "me"]}),
        ("takeout_chat", "g/2025-03-01", "Chat", {"group": "g", "participants": ["a@x.test", "me@x.test"],
                                                  "senders": ["a@x.test"]}),
        ("ai_chats", "s1/2025-03-01", "Fix the build", {"tool": "claude-code", "session": "s1", "repo": "igneous"}),
        ("github", "igneous/issues/1", "Bug", {"repo": "igneous", "kind": "issue", "number": 1,
                                               "author": {"login": "sam"}, "comments": [{"login": "pat"}]}),
        ("gcal", "cal/ev1", "Review", {"attendees": [{"email": "sam@x.test", "response": "accepted"}],
                                       "organizer": {"email": "me@x.test"}}),
        ("gcontacts", "acct/people/1", "Sam Lee", {"emails": ["sam@x.test"], "phones": ["+15550001111"]}),
        ("fs", "home/notes/a.txt", "a.txt", {"path": "home/notes/a.txt", "folders": ["home", "home/notes"]}),
        ("takeout_maps", "visits/2025-03-01", "Visits", {"kind": "visits", "visits": [
            {"key": "place:1", "name": "Cafe", "start": WHEN.isoformat()}]}),
    ]
    for source, external_id, title, meta in items:
        _put(db, source, external_id, title, meta)
    assert spine.run(db) == len(items)
    self_identity.run(db)
    resolve.refresh_canonical(db)
    sources = set(db.execute(text("SELECT DISTINCT props->>'source' FROM nodes WHERE type = 'Message'")).scalars())
    assert {"gmail", "email", "imessage", "takeout_chat", "ai_chats", "github"} <= sources
    assert [v.as_dict() for v in invariants.check_graph(db)] == []


def _codes(s):
    return sorted((v.code, v.node_id or v.edge_id) for v in invariants.check_graph(s)
                  if v.code not in ("message_received_cardinality", "message_sent_cardinality"))


def test_check_graph_reports_what_the_spine_never_builds(db):
    def node(type_, key, props=None):
        return db.execute(text("INSERT INTO nodes (type, key, name, props) VALUES (:t, :k, :k, CAST(:p AS jsonb)) "
                               "RETURNING id"), {"t": type_, "k": key, "p": json.dumps(props or {})}).scalar_one()

    def edge(a, b, type_, extractor="spine"):
        return db.execute(text("INSERT INTO edges (src, dst, type, extractor) VALUES (:a, :b, :t, :x) RETURNING id"),
                          {"a": a, "b": b, "t": type_, "x": extractor}).scalar_one()

    phantom = node("Message", "<never-ingested@x.test>")       # a reply parent no record holds, in no thread
    a, b = node("Person", "a@x.test"), node("Person", "b@x.test")
    backwards = edge(b, a, "SAME_AS", "user")                   # stored src > dst, and canonical never formed
    unsupported = edge(a, phantom, "SENT")                      # a spine edge no record supports
    _put(db, "myspace", "1", "?", {})                           # a source the catalog doesn't know
    assert _codes(db) == sorted([
        ("canonical_split", backwards), ("message_without_thread", phantom), ("phantom_message", phantom),
        ("same_as_malformed", backwards), ("spine_edge_unsupported", unsupported), ("unknown_source", None)])


def test_canonical_must_be_the_clusters_same_as_forms(db):
    from igneous.graph import resolve

    def person(key, name, props=None):
        return db.execute(text("INSERT INTO nodes (type, key, name, props) VALUES ('Person', :k, :n, "
                               "CAST(:p AS jsonb)) RETURNING id"),
                          {"k": key, "n": name, "p": json.dumps(props or {})}).scalar_one()

    def link(a, b, extractor):
        return db.execute(text("INSERT INTO edges (src, dst, type, extractor) VALUES (:a, :b, 'SAME_AS', :x) "
                               "RETURNING id"), {"a": min(a, b), "b": max(a, b), "x": extractor}).scalar_one()

    me, mine, mo = person("me", "Me", {"self": True}), person("mo@x.test", "Mo Self"), person("mo@y.test", "Mo Self")
    liz, larry, taylor = person("liz@x.test", "Liz Taylor"), person("larry@x.test", "Larry Taylor"), \
        person("taylor@x.test", "T Taylor")
    jo, jo2 = person("jo@x.test", "Jo Park"), person("jpark@x.test", "Jo Park")
    ann, ann2 = person("ann@x.test", "Ann Lee"), person("ann@y.test", "Ann Lee")
    db.execute(text("""INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, source)
                       VALUES ('Person:ann@x.test', 'Person:ann@y.test', 'SAME_AS', 'er', '', 'drop', 'user')"""))
    link(me, mine, "self")
    link(liz, taylor, "er")
    link(jo, jo2, "user")
    refused = [link(larry, taylor, "er"),  # would put Liz and Larry in one cluster
               link(mine, mo, "er"),       # scoring never adds anyone to your own cluster
               link(ann, ann2, "er")]      # you said they are two people
    resolve.refresh_canonical(db)
    assert _codes(db) == []                # canonical is exactly what SAME_AS forms, refusals included

    db.execute(text("DELETE FROM canonical WHERE node_id = :n"), {"n": jo2})
    trusted = db.execute(text("SELECT id FROM edges WHERE extractor = 'user'")).scalar_one()
    assert ("canonical_split", trusted) in _codes(db)  # a link you stated, kept apart
    assert not {("canonical_split", r) for r in refused} & set(_codes(db))
    db.execute(text("DELETE FROM edge_verdicts"))
    assert ("canonical_split", refused[2]) in _codes(db)  # without your verdict, scoring would have joined them


def test_replies_thread_violations_include_edge_endpoints():
    (violation,) = invariants.replies_thread_violations([{"id": 7, "src": 11, "dst": 12}])

    assert violation.code == "reply_crosses_threads"
    assert violation.edge_id == 7
    assert violation.details == {"src": 11, "dst": 12}


def test_check_graph_cli_prints_json_violations_and_fails(monkeypatch):
    from igneous import cli

    monkeypatch.setattr(cli, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(invariants, "check_graph", lambda session: [invariants.Violation(
        code="message_sent_cardinality", message="Message has 0 senders; its records name 1", node_id=9,
    )])

    result = CliRunner().invoke(cli.app, ["check-graph"])

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "code": "message_sent_cardinality",
        "message": "Message has 0 senders; its records name 1",
        "node_id": 9,
    }
