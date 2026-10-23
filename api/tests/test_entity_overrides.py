from igneous.graph.overrides import plan

A, B, C = "Project:project:a", "Project:project:b", "Project:project:c"


def row(id, op, keys, value=None):
    return {"id": id, "op": op, "node_keys": keys, "value": value}


def test_later_rename_wins():
    p = plan([row(1, "rename", [A], "First"), row(2, "rename", [A], "Second")])
    assert p.renames == {A: "Second"}


def test_merge_chain_collapses_to_final_survivor():
    p = plan([row(1, "merge", [B, C]), row(2, "merge", [A, B])])
    assert p.merges == {B: A, C: A}


def test_overrides_on_a_folded_project_follow_it_to_the_survivor():
    p = plan([row(1, "rename", [B], "Renamed"), row(2, "hide", [B]), row(3, "unlink", [B, "Person:x"]),
              row(4, "merge", [A, B])])
    assert p.renames == {A: "Renamed"} and p.hidden == {A} and p.unlinks == {(A, "Person:x")}


def test_self_merges_and_cycles_never_fold_a_project_into_itself():
    p = plan([row(1, "merge", [A, A]), row(2, "merge", [A, B]), row(3, "merge", [B, A])])
    assert all(k != v for k, v in p.merges.items())


def test_unlink_and_hide_are_idempotent():
    p = plan([row(1, "hide", [A]), row(2, "hide", [A]), row(3, "unlink", [A, "m"]), row(4, "unlink", [A, "m"])])
    assert p.hidden == {A} and p.unlinks == {(A, "m")}


def test_rename_without_value_is_ignored():
    assert plan([row(1, "rename", [A], None)]).renames == {}


def test_entity_routes_are_registered():
    from igneous.main import app

    paths = app.openapi()["paths"]
    for op in ("rename", "hide", "merge", "unlink"):
        assert "post" in paths[f"/graph/entity/{{node_id}}/{op}"]
    assert "get" in paths["/graph/entity/{node_id}"]
    assert "delete" in paths["/overrides/{override_id}"]


def test_adding_a_connection_accepts_only_pairs_the_menu_offers():
    """POST /graph/link used to check only the edge type's name, so it could write illegal edges (#181)."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from igneous.db import get_session
    from igneous.main import app

    kinds = {1: "Person", 2: "Person", 3: "Project"}

    class Session:
        def execute(self, statement, params=None):
            return SimpleNamespace(scalar=lambda: kinds.get((params or {}).get("i")))

    app.dependency_overrides[get_session] = lambda: Session()
    try:
        client = TestClient(app)
        assert client.post("/graph/link", json={"src": 1, "dst": 2, "type": "PART_OF"}).status_code == 400
        assert client.post("/graph/link", json={"src": 3, "dst": 1, "type": "SAME_AS"}).status_code == 400
        assert client.post("/graph/link", json={"src": 1, "dst": 9, "type": "RELATED_TO"}).status_code == 404
    finally:
        app.dependency_overrides.clear()
