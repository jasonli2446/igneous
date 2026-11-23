"""A rejected pair stays rejected across rules and rebuilds; undo keeps its exact provenance."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import llm, mcp_server, providers
from igneous.graph import verify
from igneous.graph.resolve import refresh_canonical
from igneous.main import app


@pytest.fixture
def graph(db, monkeypatch):
    def forbidden(*_a, **_kw):
        raise AssertionError("corrections never need a model")

    monkeypatch.setattr(llm, "structured", forbidden)
    monkeypatch.setattr(providers, "chat", forbidden)
    monkeypatch.setattr(verify.settings, "llm_enabled", False)
    yield db, TestClient(app)


def node(s, kind, key, name=None, props=None):
    return s.execute(text("INSERT INTO nodes (type,key,name,props) VALUES (:t,:k,:n,CAST(:p AS jsonb)) RETURNING id"),
                     {"t": kind, "k": key, "n": name or key, "p": json.dumps(props or {})}).scalar_one()


def edge(s, a, b, kind="RELATED_TO", extractor="threads", props=None, item=None):
    return s.execute(text("""
        INSERT INTO edges (src,dst,type,extractor,extractor_version,props,source_item_id,confidence,model,
                           valid_from,valid_to,span_start,span_end)
        VALUES (:a,:b,:t,:x,'2',CAST(:p AS jsonb),:i,.73,'fixture-model',
                '2025-01-01','2025-01-02',0,5) RETURNING id
    """), {"a": a, "b": b, "t": kind, "x": extractor, "p": json.dumps(props or {}), "i": item}).scalar_one()


def item(s, key="record"):
    return s.execute(text("""INSERT INTO source_items (source,external_id,content_hash,title,body_text)
                            VALUES ('email',:k,:k,'Synthetic evidence','Hello from a synthetic record') RETURNING id"""),
                     {"k": key}).scalar_one()


def rows(s, table):
    assert table in ("edges", "edge_evidence", "edge_verdicts")
    return list(s.execute(text(f"SELECT to_jsonb(r) FROM {table} r ORDER BY id" if table != "edge_verdicts"
                              else "SELECT to_jsonb(r) FROM edge_verdicts r ORDER BY src_key,dst_key,type,extractor")).scalars())


@pytest.mark.parametrize(("left", "right", "kind"), [
    ("Person", "Project", "INVOLVED_IN"), ("Person", "Organization", "WORKED_AT"),
    ("Thread", "Project", "PART_OF"), ("Project", "Organization", "PART_OF"),
    ("Person", "Person", "INTRODUCED"), ("Project", "Project", "PART_OF"),
    ("Event", "Project", "PART_OF"), ("File", "Project", "PART_OF"), ("Folder", "Project", "PART_OF"),
])
def test_rejection_blocks_all_derived_types_and_directions_after_a_rebuild(graph, left, right, kind):
    s, client = graph
    a, b = node(s, left, "A"), node(s, right, "B")
    first = edge(s, a, b, kind)
    other = edge(s, b, a, extractor="different-rule")
    stated = [edge(s, a, b, extractor=x) for x in verify.STATED]
    response = client.post("/graph/edge/wrong", json={"edge": first})
    assert response.status_code == 200
    assert set(response.json()["edges"]) == {first, other}
    assert response.json()["also_removed"] == 1
    assert {r["id"] for r in rows(s, "edges")} == set(stated)
    # A future rule/version chooses a different kind and reverses the direction.
    rebuilt = edge(s, b, a, kind="PART_OF", extractor="new-rule")
    verify.run(s, log=None)
    assert s.execute(text("SELECT 1 FROM edges WHERE id=:i"), {"i": rebuilt}).first() is None
    assert {r["id"] for r in rows(s, "edges")} == set(stated)


@pytest.mark.parametrize("extractor", verify.STATED)
def test_stated_facts_cannot_be_rejected(graph, extractor):
    s, client = graph
    a, b = node(s, "Person", "A"), node(s, "Project", "B")
    eid = edge(s, a, b, extractor=extractor)
    assert client.post("/graph/edge/wrong", json={"edge": eid}).status_code == 400
    assert s.execute(text("SELECT 1 FROM overrides")).first() is None
    assert s.execute(text("SELECT 1 FROM edges WHERE id=:i"), {"i": eid}).first()


def test_undo_restores_exact_edges_evidence_and_previous_verdict(graph):
    s, client = graph
    a, b = node(s, "Person", "A"), node(s, "Project", "B")
    sid = item(s)
    eid = edge(s, a, b, "INVOLVED_IN", props={"verdict": "unsure", "reason": "original reason", "custom": [1, 2]},
               item=sid)
    edge(s, b, a, extractor="other-rule", props={"why": "reverse link"})
    s.execute(text("""INSERT INTO edge_evidence (edge_id,source_item_id,kind,detail)
                     VALUES (:e,:i,'quoted',jsonb_build_object('line',7))"""), {"e": eid, "i": sid})
    s.execute(text("""INSERT INTO edge_verdicts (src_key,dst_key,type,extractor,evidence,verdict,confidence,reason)
                     VALUES ('Person:A','Project:B','INVOLVED_IN','threads','old-hash','unsure',.4,'original reason')"""))
    before = {table: rows(s, table) for table in ("edges", "edge_evidence", "edge_verdicts")}
    result = client.post("/graph/edge/wrong", json={"edge": eid}).json()
    assert rows(s, "edge_evidence") == []
    # An unrelated fact added after the correction must not be erased by Undo.
    c = node(s, "Project", "Unrelated")
    later = edge(s, b, c, extractor="user", props={"note": "added later"})
    restored = client.post("/graph/edge/restore", json={"edges": [eid]})
    assert restored.status_code == 200 and set(restored.json()["restored"]) == set(result["edges"])
    assert [r for r in rows(s, "edges") if r["id"] != later] == before["edges"]
    assert rows(s, "edge_evidence") == before["edge_evidence"]
    assert rows(s, "edge_verdicts") == before["edge_verdicts"]
    assert s.execute(text("SELECT 1 FROM overrides WHERE id=:i"), {"i": result["override_id"]}).first() is None


def test_why_panel_and_mcp_report_the_earlier_correction(graph):
    s, client = graph
    a, b = node(s, "Person", "Sam", "Sam Lee"), node(s, "Organization", "Example", "Example University")
    eid = edge(s, a, b, "WORKED_AT", extractor="organizations")
    result = client.post("/graph/edge/wrong", json={"edge": eid}).json()
    explanation = client.get("/graph/explain", params={"a": a, "b": b}).json()
    assert explanation["direct"] == []
    (correction,) = explanation["corrections"]
    assert correction["override_id"] == result["override_id"]
    assert correction["src_name"] == "Sam Lee" and correction["dst_name"] == "Example University"
    assert "wrong" in correction["reason"] and correction["updated_at"]
    assert mcp_server.why_connected(a, b)["corrections"][0]["override_id"] == result["override_id"]
    assert client.delete(f"/overrides/{result['override_id']}").status_code == 200
    assert client.get("/graph/explain", params={"a": a, "b": b}).json()["corrections"] == []


def test_conversation_removal_and_entity_unlink_restore_every_original_extractor(graph):
    s, client = graph
    project = node(s, "Project", "Widget")
    thread = node(s, "Thread", "Mail thread")
    messages = [node(s, "Message", f"M{i}", props={"source": "gmail"}) for i in range(2)]
    links = []
    for msg in messages:
        edge(s, msg, thread, "IN_THREAD", "spine")
        links.append(edge(s, msg, project, "PART_OF", "threads"))
        links.append(edge(s, project, msg, extractor="another-rule"))
    stated = edge(s, messages[1], project, "PART_OF", "user")
    response = client.post(f"/graph/entity/{project}/unlink", json={"node": messages[0]})
    assert response.status_code == 200
    result = response.json()
    assert set(result["edges"]) == set(links)
    assert s.execute(text("SELECT 1 FROM edges WHERE id=:i"), {"i": stated}).first()
    assert client.delete(f"/overrides/{result['override_id']}").status_code == 200
    assert set(links) <= {r["id"] for r in rows(s, "edges")}
    assert client.delete(f"/overrides/{result['override_id']}").status_code == 404


def test_removing_one_day_of_a_chat_leaves_its_other_days(graph):
    """A chat's days are separate conversations (an iMessage chat is years of them): only the one goes."""
    s, client = graph
    project = node(s, "Project", "Trip")
    chat = node(s, "Thread", "Trip chat")
    days = [node(s, "Message", f"D{i}", props={"source": "imessage"}) for i in range(2)]
    links = []
    for day in days:
        edge(s, day, chat, "IN_THREAD", "spine")
        links.append(edge(s, day, project, "PART_OF", "threads"))
    result = client.post(f"/graph/entity/{project}/unlink", json={"node": days[0]}).json()
    assert result["edges"] == [links[0]] and result["applied"] == "now"
    assert {r["id"] for r in rows(s, "edges") if r["type"] == "PART_OF"} == {links[1]}


def test_undo_does_not_remove_a_later_independent_correction(graph):
    s, client = graph
    a, b = node(s, "Person", "A"), node(s, "Project", "B")
    eid = edge(s, a, b, "INVOLVED_IN")
    result = client.post("/graph/edge/wrong", json={"edge": eid}).json()
    verify.record_correction(s, "Person:A", "Project:B", "INVOLVED_IN", "threads", "drop", "a later decision")
    response = client.post("/graph/edge/restore", json={"edges": result["edges"]})
    assert response.json()["restored"] == []
    assert rows(s, "edge_verdicts")[0]["reason"] == "a later decision"
    assert rows(s, "edges") == []


def test_rebuilt_node_ids_do_not_break_rejection_or_undo(graph):
    s, client = graph
    a, b = node(s, "Person", "A"), node(s, "Project", "B")
    eid = edge(s, a, b, "INVOLVED_IN")
    result = client.post("/graph/edge/wrong", json={"edge": eid}).json()
    s.execute(text("DELETE FROM nodes WHERE id=:i"), {"i": b})
    rebuilt = node(s, "Project", "B")
    edge(s, a, rebuilt, "PART_OF", "new-rule")
    verify.run(s, log=None)
    assert rows(s, "edges") == []
    response = client.post("/graph/edge/restore", json={"edges": result["edges"]})
    assert response.json()["restored"] == [eid]
    (restored,) = rows(s, "edges")
    assert restored["dst"] == rebuilt


def test_rejecting_and_restoring_identity_link_refreshes_canonical_state(graph, monkeypatch):
    s, client = graph
    a, b = node(s, "Person", "sam@example.test", "Sam Lee"), node(s, "Person", "sam@other.test", "Sam Lee")
    eid = edge(s, a, b, "SAME_AS", "er")
    refresh_canonical(s)
    assert s.execute(text("SELECT count(*) FROM canonical")).scalar() == 2
    original_commit, commits = Session.commit, []

    def commit(session):
        commits.append(session)
        original_commit(session)

    monkeypatch.setattr(Session, "commit", commit)
    result = client.post("/graph/edge/wrong", json={"edge": eid}).json()
    assert len(commits) == 2 and result["applied"] == "now"  # the intent (verdict and snapshot), then the change
    assert s.execute(text("SELECT count(*) FROM canonical")).scalar() == 0
    assert client.post("/graph/edge/restore", json={"edges": result["edges"]}).json()["restored"] == [eid]
    assert len(commits) == 4
    assert s.execute(text("SELECT count(*) FROM canonical")).scalar() == 2


def test_restoring_an_old_archive_cannot_bypass_a_newer_pair_correction(graph):
    s, client = graph
    a, b = node(s, "Person", "A"), node(s, "Project", "B")
    old = edge(s, a, b, "INVOLVED_IN", "old-rule")
    s.execute(text("""INSERT INTO edge_verdicts (src_key,dst_key,type,extractor,evidence,verdict,confidence)
                     VALUES ('Person:A','Project:B','INVOLVED_IN','old-rule','hash','drop',.9)"""))
    verify.apply_verdicts(s)
    new = edge(s, b, a, extractor="new-rule")
    result = client.post("/graph/edge/wrong", json={"edge": new}).json()
    response = client.post("/graph/edge/restore", json={"edges": [old]})
    assert response.json()["restored"] == []
    assert s.execute(text("SELECT 1 FROM overrides WHERE id=:i"), {"i": result["override_id"]}).first()
    assert s.execute(text("SELECT 1 FROM edge_verdicts WHERE source='user' AND evidence=:e"),
                     {"e": f"override:{result['override_id']}"}).first()
    assert rows(s, "edges") == []


def test_undo_skips_deleted_source_records_without_touching_other_edges(graph):
    s, client = graph
    a, b, c = node(s, "Person", "A"), node(s, "Project", "B"), node(s, "Project", "C")
    sid = item(s)
    eid = edge(s, a, b, "INVOLVED_IN", item=sid)
    other = edge(s, b, c, extractor="user")
    result = client.post("/graph/edge/wrong", json={"edge": eid}).json()
    s.execute(text("DELETE FROM source_items WHERE id=:i"), {"i": sid})
    response = client.post("/graph/edge/restore", json={"edges": result["edges"]})
    assert response.status_code == 200 and response.json()["restored"] == []
    assert [r["id"] for r in rows(s, "edges")] == [other]
