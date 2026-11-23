"""GitHub repositories stay repositories; unconfirmed handles do not displace people."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from igneous.graph.find import find
from igneous.graph.spine import Builder
from igneous.main import app


@pytest.fixture
def graph(db):
    return db


def node(s, name, key=None, props=None, kind="Person"):
    return s.execute(text("""
        INSERT INTO nodes (type, key, name, props) VALUES (:t, :k, :n, CAST(:p AS jsonb)) RETURNING id
    """), {"t": kind, "k": key or name, "n": name, "p": json.dumps(props or {})}).scalar_one()


def edge(s, a, b, kind):
    s.execute(text("INSERT INTO edges (src,dst,type,extractor) VALUES (:a,:b,:t,'spine')"),
              {"a": a, "b": b, "t": kind})


def exchange(s, sender, recipient, label, hidden=False):
    mid = node(s, label, kind="Message", props={"hidden": hidden})
    edge(s, sender, mid, "SENT")
    edge(s, mid, recipient, "RECEIVED")


def test_github_fixture_never_builds_repo_logins_as_people(graph):
    s = graph
    sid = s.execute(text("""INSERT INTO source_items (source,external_id,content_hash)
                            VALUES ('github','sam/project/issue/1','fixture') RETURNING id""")).scalar_one()
    item = SimpleNamespace(id=sid, source="github", external_id="sam/project/issue/1", title="Fix the build",
                           occurred_at=None,
                           raw_meta={"repo": "sam/project", "kind": "issue", "number": 1,
                                     "author": {"login": "sam/project"},
                                     "comments": [{"login": "sam/project"}, {"login": "samlee"}],
                                     "commits": [{"login": "sam/other"}]})
    builder = Builder(s)
    builder.build(item)
    builder.flush()
    assert list(s.execute(text("SELECT key FROM nodes WHERE type='Person'")).scalars()) == ["github:samlee"]
    assert s.execute(text("SELECT type FROM nodes WHERE key='repo:sam/project'")).scalar_one() == "Folder"


def test_existing_repository_people_are_filtered_before_the_candidate_limit(graph, monkeypatch):
    s = graph
    from igneous.graph import find as finder

    monkeypatch.setattr(finder, "CANDIDATES", 3)
    for i in range(5):
        node(s, f"sam/repo{i}", f"github:sam/repo{i}")
    # Even a human-looking label must not turn a repository key into a person.
    node(s, "Sam", "github:owner/project")
    repo = node(s, "sam/project", "repo:sam/project", kind="Folder")
    human = node(s, "Sam Lee", "sam@example.test")
    hits = find(s, "sam")
    assert human in {h["id"] for h in hits}
    assert repo in {h["id"] for h in hits}
    assert all("/" not in h["name"] for h in hits if h["type"] == "Person")


def test_named_person_beats_exact_bare_handle_even_with_one_way_activity(graph):
    s = graph
    named = node(s, "Sam Lee", "sam.lee@example.test")
    handle = node(s, "sam", "github:sam", {"github_login": "sam"})
    other = node(s, "Alex Example", "alex@example.test")
    exchange(s, handle, other, "one-way")
    hits = find(s, "sam")
    assert [h["id"] for h in hits] == [named, handle]
    assert hits[0]["score"] > hits[1]["score"]


def test_reciprocal_aliases_and_contact_names_keep_normal_match_ranking(graph):
    s = graph
    node(s, "Sam Lee", "sam.lee@example.test")
    handle = node(s, "sam", "github:sam")
    alias = node(s, "sam-alias", "sam@example.test")
    other = node(s, "Alex Example", "alex@example.test")
    s.execute(text("INSERT INTO canonical VALUES (:a,:h)"), {"a": alias, "h": handle})
    exchange(s, handle, other, "outbound")
    exchange(s, other, alias, "inbound")
    assert find(s, "sam")[0]["id"] == handle
    contact = node(s, "Sam", "+15555550123", {"named_by": "contacts"})
    assert find(s, "sam")[0]["id"] in (contact, handle)
    assert next(h for h in find(s, "sam") if h["id"] == contact)["score"] >= 10


def test_self_mail_or_hidden_traffic_does_not_confirm_a_handle(graph):
    s = graph
    human = node(s, "Sam Lee", "sam.lee@example.test")
    handle = node(s, "sam", "github:sam")
    other = node(s, "Alex Example", "alex@example.test")
    exchange(s, handle, handle, "self")
    exchange(s, handle, other, "outbound")
    exchange(s, other, handle, "hidden reply", hidden=True)
    assert find(s, "sam")[0]["id"] == human


def test_http_and_mcp_share_person_search_policy(graph):
    from igneous import mcp_server

    s = graph
    node(s, "sam/repo", "github:sam/repo")
    human = node(s, "Sam Lee", "sam@example.test")
    node(s, "sam", "github:sam")
    response = TestClient(app).get("/graph/find", params={"q": "sam"})
    assert response.status_code == 200
    assert response.json()["results"][0]["id"] == human
    assert mcp_server.find("sam")[0]["node_id"] == human
    assert all(h["name"] != "sam/repo" for h in response.json()["results"])
