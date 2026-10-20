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
    ])

    assert [(v.code, v.node_id) for v in violations] == [
        ("message_sent_cardinality", 10),
        ("message_received_cardinality", 10),
        ("message_sent_cardinality", 11),
        ("message_sent_cardinality", 13),
    ]


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
