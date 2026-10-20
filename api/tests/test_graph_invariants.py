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
        ("illegal_edge_endpoints", 3),
    ]


def test_message_cardinality_reports_each_failed_constraint():
    violations = invariants.message_cardinality_violations([
        {"id": 10, "sent_count": 0, "received_count": 0},
        {"id": 11, "sent_count": 2, "received_count": 1},
    ])

    assert [(v.code, v.node_id) for v in violations] == [
        ("message_sent_cardinality", 10),
        ("message_received_cardinality", 10),
        ("message_sent_cardinality", 11),
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
            self.calls = 0

        def execute(self, statement):
            self.calls += 1
            return Result([
                [{"id": 1, "type": "SENT", "src_type": "Person", "dst_type": "Message"}],
                [{"id": 2, "sent_count": 1, "received_count": 0}],
                [{"id": 3, "src": 2, "dst": 4}],
            ][self.calls - 1])

    session = Session()
    violations = invariants.check_graph(session)

    assert session.calls == 3
    assert [v.code for v in violations] == ["message_received_cardinality", "reply_crosses_threads"]


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
