"""Timeline and commitment tools read the same cited graph as the HTTP API."""
import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from igneous import mcp_server
from igneous.graph.history import commitments, store_claims


def node(s, kind, name):
    return s.execute(text("INSERT INTO nodes (type,key,name) VALUES (:t,:n,:n) RETURNING id"),
                     {"t": kind, "n": name}).scalar_one()


def edge(s, a, b, kind, item=None):
    return s.execute(text("""INSERT INTO edges (src,dst,type,extractor,source_item_id)
                            VALUES (:a,:b,:t,'spine',:i) RETURNING id"""),
                     {"a": a, "b": b, "t": kind, "i": item}).scalar_one()


def promise(s, label, year, *, status="open"):
    body = f"I will send {label} on Friday."
    when = datetime(year, 1, 1, tzinfo=UTC)
    sid = s.execute(text("""
        INSERT INTO source_items (source,external_id,content_hash,title,body_text,occurred_at,raw_meta)
        VALUES ('gmail',:label,:label,'PRIVATE_TITLE',:body,:at,'{"token":"PRIVATE_TOKEN"}') RETURNING id
    """), {"label": label, "body": body, "at": when}).scalar_one()
    mid = s.execute(text("""INSERT INTO nodes (type,key,name,source_item_id)
                            VALUES ('Message',:k,:k,:i) RETURNING id"""), {"k": label, "i": sid}).scalar_one()
    store_claims(s, SimpleNamespace(id=sid, node_id=mid, occurred_at=when), body,
                 {"commitments": [{"who": "Sam Lee", "what": label, "by_when": "Friday", "quote": body}]},
                 "fixture-model")
    claim = s.execute(text("SELECT id FROM nodes WHERE type='Commitment' AND source_item_id=:i"),
                      {"i": sid}).scalar_one()
    s.execute(text("UPDATE nodes SET props=props || jsonb_build_object('status',CAST(:v AS text)) WHERE id=:i"),
              {"i": claim, "v": status})
    return sid, mid, claim


@pytest.fixture
def graph(db, monkeypatch):
    from igneous import llm, providers

    def forbidden(*_args, **_kwargs):
        raise AssertionError("history tools must never call a model")

    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")  # what the fixture's records are: the tools may share them
    monkeypatch.setattr(llm, "chat", forbidden)
    monkeypatch.setattr(providers, "chat", forbidden)
    monkeypatch.setattr(llm, "structured", forbidden)
    s = db
    sam = node(s, "Person", "Sam Lee")
    alias = node(s, "Person", "sam@example.test")
    alex = node(s, "Person", "Alex Example")
    project = node(s, "Project", "Widget Tracker")
    thread = node(s, "Thread", "Widget chat")
    org = node(s, "Organization", "Example University")
    empty = node(s, "Organization", "Empty organization")
    s.execute(text("INSERT INTO canonical VALUES (:alias,:head)"), {"alias": alias, "head": sam})
    sid, msg, claim = promise(s, "the draft", 2024)
    edge(s, alias, msg, "SENT", sid)
    edge(s, msg, alex, "RECEIVED", sid)
    edge(s, msg, project, "PART_OF", sid)
    edge(s, msg, thread, "IN_THREAD", sid)
    other_sid, reply, resolved = promise(s, "the release", 2025, status="resolved")
    edge(s, alex, reply, "SENT", other_sid)
    edge(s, reply, alias, "RECEIVED", other_sid)
    edge(s, reply, project, "PART_OF", other_sid)
    _, unrelated, _ = promise(s, "an unrelated task", 2026)
    other = node(s, "Project", "Other project")
    edge(s, unrelated, other, "PART_OF")
    offer = s.execute(text("""
        INSERT INTO source_items (source,external_id,content_hash,title,body_text,occurred_at)
        VALUES ('gmail','offer','offer','Your offer','Welcome to Example University, Sam.','2023-01-01') RETURNING id
    """)).scalar_one()
    worked = s.execute(text("""
        INSERT INTO edges (src,dst,type,extractor,source_item_id,valid_from,valid_to)
        VALUES (:sam,:org,'WORKED_AT','organizations',:sid,'2023-01-01','2024-01-01') RETURNING id
    """), {"sam": alias, "org": org, "sid": sid}).scalar_one()
    s.execute(text("INSERT INTO edge_evidence (edge_id,source_item_id,kind,detail) VALUES (:e,:i,'offer','{}')"),
              {"e": worked, "i": offer})  # the record that names the organization
    yield SimpleNamespace(s=s, sam=sam, alias=alias, project=project, thread=thread, org=org, empty=empty,
                          sid=sid, msg=msg, claim=claim, resolved=resolved, offer=offer)


def call(name, **args):
    result = asyncio.run(mcp_server.server.call_tool(name, args))
    assert not result.is_error
    return json.loads(result.content[0].text)


def test_timeline_protocol_resolves_aliases_and_keeps_source_text_out(graph):
    result = call("timeline", node_id=graph.alias, other_node_id=graph.org)
    assert {k: result["a"][k] for k in ("id", "type")} == {"id": graph.sam, "type": "Person"}
    assert result["mode"] == "relationship" and len(result["edges"]) == 1
    link = result["edges"][0]
    assert link["type"] == "WORKED_AT" and link["direction"] == "outgoing"
    assert link["counterpart"]["name"] == "Example University"
    assert link["interval"]["from"].startswith("2023-01-01")
    assert link["provenance"]["evidence"]["source_item_id"] == graph.sid
    for private in ("PRIVATE_TITLE", "PRIVATE_TOKEN", "I will send", "Your offer", "Welcome to"):
        assert private not in json.dumps(result)


def test_timeline_activity_is_bounded_and_reports_truncation(graph):
    result = call("timeline", node_id=graph.sam, limit=1)
    assert result["mode"] == "activity" and result["truncated"]
    assert len(result["edges"]) == 1 and result["edges"][0]["type"] == "RECEIVED"


@pytest.mark.parametrize("kind", ["sam", "alias", "project", "thread", "msg", "org"])
def test_commitments_scope_is_applied_before_limit_with_real_citations(graph, kind):
    result = call("commitments", node_id=getattr(graph, kind), limit=1)
    (claim,) = result["commitments"]
    assert claim["id"] == graph.claim and claim["what"] == "the draft"
    assert claim["derived"] is True and claim["origin"] == "model"
    assert claim["evidence"]["source_item_id"] == graph.sid
    assert claim["evidence"]["span"] == {"start": 0, "end": len("I will send the draft on Friday.")}
    assert "an unrelated task" not in json.dumps(result)


def test_commitments_status_empty_scope_and_unscoped_http_behavior(graph):
    resolved = call("commitments", node_id=graph.alias, status="resolved")["commitments"]
    assert [c["id"] for c in resolved] == [graph.resolved]
    all_claims = call("commitments", node_id=graph.project, status="all")["commitments"]
    assert [c["id"] for c in all_claims] == [graph.resolved, graph.claim]
    assert call("commitments", node_id=graph.empty)["commitments"] == []
    assert commitments(graph.s, limit=1)[0]["what"] == "an unrelated task"


def test_commitment_scope_uses_edge_evidence_and_ignores_rejected_links(graph):
    link = graph.s.execute(text("UPDATE edges SET source_item_id=NULL WHERE type='WORKED_AT' RETURNING id")).scalar_one()
    graph.s.execute(text("""INSERT INTO edge_evidence (edge_id,source_item_id,kind,detail)
                            VALUES (:e,:i,'fixture','{}')"""), {"e": link, "i": graph.sid})
    assert call("commitments", node_id=graph.org)["commitments"][0]["id"] == graph.claim
    graph.s.execute(text("UPDATE edges SET props=jsonb_build_object('verdict','drop') WHERE id=:i"), {"i": link})
    assert call("commitments", node_id=graph.org)["commitments"] == []


def test_unknown_nodes_are_distinct_from_empty_results(graph):
    from mcp.server.mcpserver.exceptions import ToolError

    for name, args in (("timeline", {"node_id": 9999}), ("timeline", {"node_id": graph.sam, "other_node_id": 9999}),
                       ("commitments", {"node_id": 9999})):
        with pytest.raises(ToolError, match="no node 9999"):
            asyncio.run(mcp_server.server.call_tool(name, args))
    assert call("timeline", node_id=graph.empty)["edges"] == []


@pytest.mark.parametrize(("name", "args"), [
    ("timeline", {"node_id": 0}),
    ("timeline", {"node_id": 1, "other_node_id": 0}),
    ("timeline", {"node_id": 1, "limit": 0}),
    ("timeline", {"node_id": 1, "limit": 201}),
    ("commitments", {"node_id": 0}),
    ("commitments", {"node_id": 1, "limit": 0}),
    ("commitments", {"node_id": 1, "limit": 201}),
    ("commitments", {"node_id": 1, "status": "unknown"}),
])
def test_mcp_rejects_invalid_history_arguments_before_opening_a_session(monkeypatch, name, args):
    from mcp.server.mcpserver.exceptions import ToolError

    def forbidden():
        raise AssertionError("invalid arguments must not reach the database")

    monkeypatch.setattr(mcp_server, "read_only_session", forbidden)
    with pytest.raises(ToolError):
        asyncio.run(mcp_server.server.call_tool(name, args))
