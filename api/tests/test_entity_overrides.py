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


def test_hide_is_idempotent_and_links_keep_their_type_props_and_who_stated_them():
    p = plan([row(1, "hide", [A]), row(2, "hide", [A]),
              row(3, "link", ["Person:x", A], "INVOLVED_IN", {"props": {"why": "you added this"}}),
              row(4, "unlink", [A, "Person:x"], data={"pair_correction": True}),
              row(5, "link", ["Person:y", "Project:added:board games"], "INVOLVED_IN",
                  {"props": {"why": "she hosts it"}, "by": "agent:claude-code",
                   "nodes": {"Project:added:board games": {"type": "Project", "name": "Board Games"}}}),
              row(6, "link", ["Person:x", "Project:added:board games"], "INVOLVED_IN",
                  {"props": {}, "nodes": {"Project:added:board games": {"type": "Project", "name": "board games"}}})])
    assert p.hidden == {A}
    assert p.links == [("Person:x", A, "INVOLVED_IN", {"why": "you added this"}, "user"),
                       ("Person:y", "Project:added:board games", "INVOLVED_IN", {"why": "she hosts it"},
                        "agent:claude-code"),
                       ("Person:x", "Project:added:board games", "INVOLVED_IN", {}, "user")]
    # A new thing is what the oldest fact that names it says.
    assert p.nodes == {"Project:added:board games": {"type": "Project", "name": "Board Games",
                                                     "by": "agent:claude-code"}}


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
    # One write path for what you or an agent add: a fact. A link is taken out by its id.
    assert "post" in paths["/facts"] and "/graph/link" not in paths and "delete" in paths["/graph/link/{edge_id}"]


def test_adding_a_fact_accepts_only_pairs_the_menu_offers(db):
    """POST /facts accepts only the pairs its menu offers for the two kinds of thing (#181)."""
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from igneous.main import app

    ids = [db.execute(text("INSERT INTO nodes (type, key, name) VALUES (:t, :k, :k) RETURNING id"),
                      {"t": t, "k": k}).scalar_one() for t, k in (("Person", "a"), ("Person", "b"), ("Project", "p"))]
    client = TestClient(app)

    def fact(subject, relation, object_, **more):
        return client.post("/facts", json={"subject": subject, "relation": relation, "object": object_, **more})

    assert fact(ids[0], "PART_OF", ids[1]).status_code == 400
    assert fact(ids[2], "SAME_AS", ids[0]).status_code == 400
    assert fact(ids[0], "RELATED_TO", 10**9).status_code == 404
    assert fact(ids[0], "RELATED_TO", ids[0]).status_code == 400                       # nothing is linked to itself
    assert fact(ids[0], "SAME_AS", ids[1], agent="helper").status_code == 400          # only you say who is who
    assert fact(ids[0], "INVOLVED_IN", ids[2], agent="helper").status_code == 400      # an agent says why
    assert fact(ids[0], "INVOLVED_IN", {"name": "  ", "type": "Project"}).status_code in (400, 422)
    assert fact(ids[0], "INVOLVED_IN", {"name": "Chess", "type": "Person"}).status_code == 422
    assert db.execute(text("SELECT count(*) FROM overrides")).scalar() == 0
