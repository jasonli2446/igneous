from igneous.graph.overrides import plan

A, B, C = "Project:project:a", "Project:project:b", "Project:project:c"


def row(id, op, keys, value=None, data=None):
    return {"id": id, "op": op, "node_keys": keys, "value": value, "data": data}


def test_later_rename_wins():
    p = plan([row(1, "rename", [A], "First"), row(2, "rename", [A], "Second")])
    assert p.renames == {A: "Second"}


def test_merge_chain_collapses_to_final_survivor():
    p = plan([row(1, "merge", [B, C]), row(2, "merge", [A, B])])
    assert p.merges == {B: A, C: A}


def test_overrides_on_a_folded_project_follow_it_to_the_survivor():
    p = plan([row(1, "rename", [B], "Renamed"), row(2, "hide", [B]), row(3, "merge", [A, B])])
    assert p.renames == {A: "Renamed"} and p.hidden == {A}


def test_self_merges_and_cycles_never_fold_a_project_into_itself():
    p = plan([row(1, "merge", [A, A]), row(2, "merge", [A, B]), row(3, "merge", [B, A])])
    assert all(k != v for k, v in p.merges.items())


def test_hide_is_idempotent_and_links_keep_their_type_and_props():
    p = plan([row(1, "hide", [A]), row(2, "hide", [A]),
              row(3, "link", ["Person:x", A], "INVOLVED_IN", {"props": {"why": "you added this"}}),
              row(4, "unlink", [A, "Person:x"], data={"pair_correction": True})])
    assert p.hidden == {A}
    assert p.links == [("Person:x", A, "INVOLVED_IN", {"why": "you added this"})]


def test_rename_without_value_is_ignored():
    assert plan([row(1, "rename", [A], None)]).renames == {}


def test_entity_routes_are_registered():
    from igneous.main import app

    paths = app.openapi()["paths"]
    for op in ("rename", "hide", "merge", "unlink"):
        assert "post" in paths[f"/graph/entity/{{node_id}}/{op}"]
    assert "get" in paths["/graph/entity/{node_id}"]
    assert "delete" in paths["/overrides/{override_id}"]
    assert "get" in paths["/graph/suggestions"] and "post" in paths["/graph/edge/confirm"]


def test_adding_a_connection_accepts_only_pairs_the_menu_offers(db):
    """POST /graph/link accepts only the pairs its menu offers for the two kinds of thing (#181)."""
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from igneous.main import app

    ids = [db.execute(text("INSERT INTO nodes (type, key, name) VALUES (:t, :k, :k) RETURNING id"),
                      {"t": t, "k": k}).scalar_one() for t, k in (("Person", "a"), ("Person", "b"), ("Project", "p"))]
    client = TestClient(app)
    assert client.post("/graph/link", json={"src": ids[0], "dst": ids[1], "type": "PART_OF"}).status_code == 400
    assert client.post("/graph/link", json={"src": ids[2], "dst": ids[0], "type": "SAME_AS"}).status_code == 400
    assert client.post("/graph/link", json={"src": ids[0], "dst": 10**9, "type": "RELATED_TO"}).status_code == 404
    assert client.post("/graph/link", json={"src": ids[0], "dst": ids[0], "type": "RELATED_TO"}).status_code == 422
    assert db.execute(text("SELECT count(*) FROM overrides")).scalar() == 0


def test_two_people_become_one_only_through_same_person_never_an_added_connection(db):
    """'+ Connect to…' never offers 'same person', and the link route refuses it: merging goes through POST
    /graph/person/{id}/merge, which asks you to confirm."""
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from igneous.main import app

    a, b = (db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Person', :k, :k) RETURNING id"),
                       {"k": k}).scalar_one() for k in ("a@x.test", "b@x.test"))
    client = TestClient(app)
    options = client.get("/graph/link/options", params={"a": a, "b": b}).json()["options"]
    assert [o["type"] for o in options] == ["RELATED_TO"]
    assert client.post("/graph/link", json={"src": a, "dst": b, "type": "SAME_AS"}).status_code == 400
    assert client.get("/graph/link/options", params={"a": a, "b": 10**9}).status_code == 404
    assert client.get("/graph/link/options", params={"a": a, "b": a}).status_code == 422
    assert db.execute(text("SELECT count(*) FROM overrides")).scalar() == 0
