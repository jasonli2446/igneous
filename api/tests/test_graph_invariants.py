import json
from contextlib import nullcontext

from typer.testing import CliRunner

from igneous.graph import invariants


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


def test_message_cardinality_reports_each_failed_constraint():
    violations = invariants.message_cardinality_violations([
        {"id": 10, "source": "email", "sent_count": 0, "received_count": 0},
        {"id": 11, "source": "email", "sent_count": 2, "received_count": 1},
        {"id": 12, "source": "imessage", "sent_count": 2, "received_count": 3},
        {"id": 13, "source": "imessage", "sent_count": 0, "received_count": 1},
        # An AI session is you talking to an assistant: nobody receives it. GitHub activity isn't checked.
        {"id": 14, "source": "ai_chats", "sent_count": 1, "received_count": 0},
        {"id": 15, "source": "ai_chats", "sent_count": 2, "received_count": 0},
        {"id": 16, "source": "github", "sent_count": 3, "received_count": 0},
    ])

    assert [(v.code, v.node_id) for v in violations] == [
        ("message_sent_cardinality", 10),
        ("message_received_cardinality", 10),
        ("message_sent_cardinality", 11),
        ("message_sent_cardinality", 13),
        ("message_sent_cardinality", 15),
    ]
    assert violations[0].details == {"sent_count": 0, "expected": 1}
    assert violations[3].details == {"sent_count": 0, "minimum": 1}


def test_edges_the_code_writes_on_purpose_are_legal():
    rows = [{"id": i, "type": t, "src_type": a, "dst_type": b} for i, (t, a, b) in enumerate([
        ("INTRODUCED", "Person", "Person"), ("HAS_COMMITMENT", "Message", "Commitment"),
        ("HAS_DECISION", "Message", "Decision"), ("CONTAINS", "Folder", "Message"),
        ("PART_OF", "Project", "Organization"),
    ])]
    assert invariants.endpoint_violations(rows) == []


def test_a_spine_build_of_every_message_source_passes_check_graph(test_db):
    """The checker and the spine agree: one record of each kind, built for real, is a legal graph (#181)."""
    from datetime import UTC, datetime

    from sqlalchemy import text

    from igneous.db import SessionLocal
    from igneous.graph import spine

    when = datetime(2025, 3, 1, 12, tzinfo=UTC)
    items = [
        ("gmail", "g1", "Plans", {"from": [{"email": "sam@x.test", "name": "Sam"}], "to": [{"email": "me@x.test"}],
                                  "message_id": "<g1@x.test>", "gmail_thread_id": "t1"}),
        ("email", "e1", "Re: Plans", {"from": [{"email": "me@x.test"}], "to": [{"email": "sam@x.test"}],
                                      "message_id": "<e1@x.test>", "fingerprint": "fp-e1"}),
        ("imessage", "c1/2025-03-01", "Pat", {"chat": "c1", "participants": ["+15550001111"],
                                              "senders": ["+15550001111", "me"]}),
        ("takeout_chat", "g/2025-03-01", "Chat", {"group": "g", "participants": ["a@x.test", "me@x.test"],
                                                  "senders": ["a@x.test"]}),
        ("ai_chats", "s1/2025-03-01", "Fix the build", {"tool": "claude-code", "session": "s1", "repo": "igneous"}),
        ("github", "igneous/issues/1", "Bug", {"repo": "igneous", "kind": "issue", "number": 1,
                                               "author": {"login": "sam"}, "comments": [{"login": "pat"}]}),
        ("gcal", "cal/ev1", "Review", {"attendees": [{"email": "sam@x.test", "response": "accepted"}],
                                       "organizer": {"email": "me@x.test"}}),
        ("fs", "home/notes/a.txt", "a.txt", {"path": "home/notes/a.txt", "folders": ["home", "home/notes"]}),
        ("takeout_maps", "visits/2025-03-01", "Visits", {"kind": "visits", "visits": [
            {"key": "place:1", "name": "Cafe", "start": when.isoformat()}]}),
    ]
    with SessionLocal() as s:
        s.execute(text("TRUNCATE source_items, nodes, edges, edge_evidence, chunks, canonical, pipeline_state, "
                       "dirty_nodes, dirty_rebuild RESTART IDENTITY CASCADE"))
        for source, external_id, title, meta in items:
            s.execute(text("""
                INSERT INTO source_items (source, external_id, content_hash, title, body_text, raw_meta, occurred_at)
                VALUES (:s, :e, :e, :t, 'body', CAST(:m AS jsonb), :w)"""),
                      {"s": source, "e": external_id, "t": title, "m": json.dumps(meta), "w": when})
        s.commit()
        assert spine.run(s) == len(items)
        types = set(s.execute(text("SELECT DISTINCT n.props->>'source' FROM nodes n WHERE n.type = 'Message'")).scalars())
        assert {"gmail", "email", "imessage", "takeout_chat", "ai_chats", "github"} <= types
        assert [v.as_dict() for v in invariants.check_graph(s)] == []
        s.execute(text("TRUNCATE source_items, nodes, edges, edge_evidence, chunks, canonical, pipeline_state, "
                       "dirty_nodes, dirty_rebuild RESTART IDENTITY CASCADE"))
        s.commit()


def test_replies_thread_violations_include_edge_endpoints():
    (violation,) = invariants.replies_thread_violations([{"id": 7, "src": 11, "dst": 12}])

    assert violation.code == "reply_crosses_threads"
    assert violation.edge_id == 7
    assert violation.details == {"src": 11, "dst": 12}


def test_check_graph_runs_all_read_only_checks():
    class Result:
        def __init__(self, rows):
            self.rows = rows

        def mappings(self):
            return self.rows

    class Session:
        def __init__(self):
            self.rows = {
                str(invariants._ENDPOINT_ROWS): [{"id": 1, "type": "SENT", "src_type": "Person", "dst_type": "Message"}],
                str(invariants._MESSAGE_CARDINALITIES): [{"id": 2, "source": "email", "sent_count": 1, "received_count": 0}],
                str(invariants._REPLIES_OUTSIDE_THREAD): [{"id": 3, "src": 2, "dst": 4}],
            }

        def execute(self, statement):
            return Result(self.rows[str(statement)])

    violations = invariants.check_graph(Session())

    assert [v.code for v in violations] == ["message_received_cardinality", "reply_crosses_threads"]


def test_cardinality_and_thread_queries_skip_expected_partial_messages():
    assert "m.source_item_id IS NOT NULL" in str(invariants._MESSAGE_CARDINALITIES)
    replies_query = str(invariants._REPLIES_OUTSIDE_THREAD)
    assert "child.source_item_id IS NOT NULL" in replies_query
    assert "parent.source_item_id IS NOT NULL" in replies_query
    assert "r.extractor <> 'spine-quoted'" in replies_query


def test_check_graph_cli_prints_json_violations_and_fails(monkeypatch):
    from igneous import cli

    monkeypatch.setattr(cli, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(invariants, "check_graph", lambda session: [invariants.Violation(
        code="message_sent_cardinality", message="Message has 0 SENT edges; expected exactly 1", node_id=9,
    )])

    result = CliRunner().invoke(cli.app, ["check-graph"])

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "code": "message_sent_cardinality",
        "message": "Message has 0 SENT edges; expected exactly 1",
        "node_id": 9,
    }
