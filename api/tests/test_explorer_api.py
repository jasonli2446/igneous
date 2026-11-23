"""The explorer's read API (#209): one identity per person, nothing hidden and no service anywhere, bounds and 404s,
the empty state, where every edge came from, and no dead routes.

A small graph has the traps a real mailbox has: you and Sam each have two addresses merged into one person, a one-on-one
thread and a project are hidden, a bank mails you as a service, a link you rejected joins you and Bo, and your
organization is known from your resume and from your own mail at once. Node ids start at 1,000,000, so any integer in
that range anywhere in an answer is a node id, and every route is walked for the ids it returns.
"""
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from igneous.main import app

T0 = datetime.now(UTC) - timedelta(days=120)  # everything here is older than a month
NODE_IDS = range(1_000_000, 1_100_000)


class World(dict):
    __getattr__ = dict.__getitem__


def _node(s, type_, key, name, **props) -> int:
    return s.execute(text("""INSERT INTO nodes (type, key, name, props) VALUES (:t, :k, :n, CAST(:p AS jsonb))
                             RETURNING id"""), {"t": type_, "k": key, "n": name, "p": json.dumps(props)}).scalar_one()


def _edge(s, a, b, type_, extractor="spine", item=None, confidence=None, when=None, **props) -> int:
    return s.execute(text("""
        INSERT INTO edges (src, dst, type, props, extractor, extractor_version, source_item_id, confidence,
                           valid_from, valid_to)
        VALUES (:a, :b, :t, CAST(:p AS jsonb), :x, '1', :i, :c, :w, :w) RETURNING id"""),
        {"a": a, "b": b, "t": type_, "x": extractor, "i": item, "c": confidence, "w": when,
         "p": json.dumps(props)}).scalar_one()


def _message(s, w, ref, sender, to, title, days, thread=None, hidden=False, project=None, source="gmail",
             meta=None) -> int:
    """A message `sender` sent to `to` (node ids) `days` after T0, in `thread` and part of `project`."""
    when = T0 + timedelta(days=days)
    item = s.execute(text("""
        INSERT INTO source_items (source, external_id, content_hash, title, body_text, occurred_at, raw_meta)
        VALUES (:s, :e, :e, :t, :b, :w, CAST(:m AS jsonb)) RETURNING id"""),
        {"s": source, "e": ref, "t": title, "b": f"{title}: notes about the robotics demo.", "w": when,
         "m": json.dumps(meta or {"from": [{"email": "x@x.test"}]})}).scalar_one()
    m = s.execute(text("""INSERT INTO nodes (type, key, name, props, source_item_id)
                          VALUES ('Message', :k, :n, CAST(:p AS jsonb), :i) RETURNING id"""),
                  {"k": f"{source}:{ref}", "n": title, "p": json.dumps({"hidden": hidden}), "i": item}).scalar_one()
    s.execute(text("INSERT INTO chunks (source_item_id, node_id, ord, text) VALUES (:i, :n, 0, :t)"),
              {"i": item, "n": m, "t": f"{title}: notes about the robotics demo."})
    _edge(s, sender, m, "SENT", item=item)
    for r in to:
        _edge(s, m, r, "RECEIVED", item=item)
    if thread:
        _edge(s, m, thread, "IN_THREAD", item=item)
    if project:
        _edge(s, m, project, "PART_OF", "threads", item=item)
    w.setdefault("items", []).append(item)
    return m


@pytest.fixture
def world(db):
    s = db
    s.execute(text("SELECT setval('nodes_id_seq', 1000000, false)"))
    w = World()
    w["me"] = _node(s, "Person", "me", "Me Myself", self=True)
    w["me_work"] = _node(s, "Person", "me@acme.test", "Me Myself")
    w["sam"] = _node(s, "Person", "sam@a.test", "Sam Lee")
    w["sam2"] = _node(s, "Person", "sam.lee@b.test", "Sam Lee")
    w["ana"] = _node(s, "Person", "ana@acme.test", "Ana Ames")
    w["bo"] = _node(s, "Person", "bo@acme.test", "Bo Brown")
    w["bank"] = _node(s, "Person", "alerts@bank.test", "Bank Alerts", kind="service")
    for alias, head in (("me_work", "me"), ("me", "me"), ("sam2", "sam"), ("sam", "sam")):
        s.execute(text("INSERT INTO canonical VALUES (:a, :h)"), {"a": w[alias], "h": w[head]})
    w["crew"] = _node(s, "Thread", "thread:crew", "Robotics crew", conversational=True, hidden=False)
    w["lunch"] = _node(s, "Thread", "thread:lunch", "Lunch", conversational=True, hidden=True)
    w["atlas"] = _node(s, "Project", "thread:atlas", "Atlas", derived="threads", kind="project")
    w["secret"] = _node(s, "Project", "thread:secret", "Secret plan", derived="threads", kind="project", hidden=True)
    w["acme"] = _node(s, "Organization", "acme.test", "Acme Robotics")
    w["standup"] = _node(s, "Event", "gcal:standup", "Standup")
    # You and Sam, one-on-one (a hidden thread: the relationship itself), from and to both of each one's addresses.
    for i in range(6):
        a, b = (w["me"], w["sam2"]) if i % 2 else (w["sam"], w["me_work"])
        _message(s, w, f"lunch{i}", a, [b], f"Lunch {i}", i * 7, thread=w["lunch"], project=w["atlas"])
    # The crew: four of you, everyone writes.
    people = [w["me"], w["sam2"], w["ana"], w["bo"]]
    for i, sender in enumerate(people * 2):
        _message(s, w, f"crew{i}", sender, [p for p in people if p != sender], f"Crew update {i}", 30 + i,
                 thread=w["crew"], project=w["secret"] if i == 0 else w["atlas"])
    # Ana, one-on-one, both ways; the bank, many times, never answered (hidden mail from a service).
    _message(s, w, "ana1", w["ana"], [w["me"]], "Acme plans", 40)
    _message(s, w, "ana2", w["me_work"], [w["ana"]], "Re: Acme plans", 41)
    for i in range(5):
        _message(s, w, f"bank{i}", w["bank"], [w["me"]], f"Your statement {i}", 10 + i, hidden=True)
    _message(s, w, "hidden-sam", w["sam2"], [w["me"]], "Newsletter forward", 3, hidden=True)
    # Projects: Atlas with Sam (by his other address) and Ana; the hidden one with Sam.
    _edge(s, w["sam2"], w["atlas"], "INVOLVED_IN", "threads", verdict="keep", items=6)
    _edge(s, w["sam"], w["atlas"], "INVOLVED_IN", "threads", verdict="keep", items=2)
    _edge(s, w["ana"], w["atlas"], "INVOLVED_IN", "threads", verdict="keep", items=3)
    _edge(s, w["bank"], w["atlas"], "INVOLVED_IN", "threads", verdict="keep", items=1)
    _edge(s, w["sam"], w["secret"], "INVOLVED_IN", "threads", verdict="keep", items=1)
    # Where people work: Acme, for you from your resume and from your own mail; Ana and Bo by their addresses.
    _edge(s, w["me"], w["acme"], "WORKED_AT", "profile", title="Engineer")
    _edge(s, w["me_work"], w["acme"], "WORKED_AT", "organizations", why=["your address me@acme.test"])
    _edge(s, w["ana"], w["acme"], "WORKED_AT", "organizations", why=["wrote with you from ana@acme.test"])
    _edge(s, w["bo"], w["acme"], "WORKED_AT", "organizations", why=["wrote with you from bo@acme.test"])
    _edge(s, w["bank"], w["acme"], "WORKED_AT", "organizations")
    _edge(s, w["sam2"], w["standup"], "ATTENDED")
    _edge(s, w["me"], w["standup"], "ATTENDED")
    _edge(s, w["bank"], w["standup"], "ATTENDED")
    # Links between two people that a rule inferred: one you rejected (you and Bo), one still standing (Ana and Sam).
    w["intro"] = _edge(s, w["ana"], w["sam2"], "INTRODUCED", "relationship-history", confidence=0.8,
                       when=T0 + timedelta(days=45))
    s.execute(text("""INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, reason, source)
                      VALUES ('Person:me', 'Person:bo@acme.test', 'INTRODUCED', 'relationship-history',
                              'override:1', 'drop', 'not how we met', 'user')"""))
    s.execute(text("UPDATE graph_version SET version = version + 1"))
    w["hidden"] = {w["lunch"], w["secret"], w["bank"]} | set(s.execute(text(
        "SELECT id FROM nodes WHERE props->>'hidden' = 'true'")).scalars())
    w["aliases"] = {w["me_work"], w["sam2"]}
    return w


@pytest.fixture
def client(world):
    return TestClient(app)


def node_ids(body) -> list[int]:
    """Every node id anywhere in a JSON answer (ints in NODE_IDS), in order."""
    out = []

    def walk(v):
        if isinstance(v, bool):
            return
        if isinstance(v, int) and v in NODE_IDS:
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(body)
    return out


def read_routes(w):
    """(path, params) for every read route, over the people, groups, projects and pairs the graph has."""
    on_screen = [w.me, w.sam, w.ana, w.crew, w.atlas, w.acme]
    routes = [("/graph/view", {}), ("/graph/view", {"seed": w.me_work}), ("/graph/view", {"seed": w.sam2}),
              ("/graph/view", {"seed": w.ana, "per": 20}), ("/graph/view", {"seed": w.atlas})]
    for node in (w.sam, w.sam2, w.ana, w.bo, w.crew, w.atlas, w.acme, w.standup, w.me_work, w.secret, w.lunch):
        routes.append(("/graph/expand", {"seed": w.me, "node": node, "visible": on_screen}))
        routes.append(("/graph/expand", {"seed": w.sam2, "node": node, "visible": [w.sam]}))
    for a, b in ((w.me_work, w.sam2), (w.me, w.ana), (w.ana, w.sam2), (w.sam, w.atlas), (w.me, w.bo),
                 (w.sam2, w.acme), (w.bo, w.standup)):
        routes.append(("/graph/explain", {"a": a, "b": b}))
    for q in ("sam", "lee", "me", "ana", "acme", "robotics", "lunch", "secret", "bank", "alerts", "standup", "atlas"):
        routes.append(("/graph/find", {"q": q}))
        routes.append(("/graph/find", {"q": q, "seed": w.sam2}))
    for node in (w.me, w.me_work, w.sam2, w.ana, w.crew, w.atlas, w.acme, w.standup):
        routes += [(f"/graph/node/{node}", {}), (f"/graph/node/{node}/summary", {}),
                   (f"/graph/node/{node}/sources", {}), ("/timeline", {"a": node})]
    routes += [("/timeline", {"a": w.me_work, "b": w.sam2}), ("/graph/entity/" + str(w.atlas), {}),
               ("/commitments", {"status": "all"}), ("/ask/suggestions", {}),
               ("/experts", {"q": "robotics demo", "mode": "lexical"}), ("/graph/version", {})]
    return routes


def test_every_read_route_answers_heads_only_and_never_hidden_nodes_or_services(world, client):
    w = world
    for path, params in read_routes(w):
        r = client.get(path, params=params)
        assert r.status_code == 200, (path, params, r.text)
        body = r.json()
        asked = {params.get("seed"), params.get("node"), params.get("a"), params.get("b")}
        if path.startswith("/graph/node/") and path.count("/") == 3:
            asked.add(int(path.rsplit("/", 1)[1]))
        ids = node_ids(body)
        assert not w.aliases & set(ids), (path, params, "another address of a person, not the person")
        # A hidden node or a service may come back only as the very node the request named.
        leaked = (w.hidden - asked) & set(ids)
        assert not leaked, (path, params, "hidden or service", leaked)
        for listing in (body.get("nodes"), body.get("results"), body.get("highlights"), body.get("people")) \
                if isinstance(body, dict) else ():
            if listing:
                listed = [x["id"] for x in listing]
                assert len(listed) == len(set(listed)), (path, params, "someone twice")


def test_a_person_is_opened_and_shown_as_their_identity_head(world, client):
    w = world
    assert client.get(f"/graph/node/{w.sam2}").json()["id"] == w.sam
    assert client.get("/graph/view", params={"seed": w.me_work}).json()["seed"] == w.me
    why = client.get("/graph/explain", params={"a": w.me_work, "b": w.sam2}).json()
    assert (why["a"]["id"], why["b"]["id"]) == (w.me, w.sam)
    # The crew's eight messages and six one-on-one, from and to both addresses of each: one relationship.
    assert why["messages"] == 14
    # The one-on-one thread is the relationship, not a node to open: named, but no id.
    assert [(c["thread"], c["thread_id"], c["messages"]) for c in why["conversations"]] == [
        ("Robotics crew", w.crew, 8), ("Lunch", None, 6)]
    entity = client.get(f"/graph/entity/{w.atlas}").json()
    assert [(p["id"], p["items"]) for p in entity["people"] if p["name"] == "Sam Lee"] == [(w.sam, 8)]


def test_the_opening_view_shows_your_org_once_and_never_a_pair_you_rejected(world, client):
    w = world
    g = client.get("/graph/view").json()
    assert g["seed"] == w.me and g["empty"] is False
    ids = [n["id"] for n in g["nodes"]]
    # Acme is yours twice over (your resume, your own mail): one node, first in the ring.
    assert ids.count(w.acme) == 1 and ids[1] == w.acme
    assert {w.sam, w.ana, w.crew} <= set(ids)
    # You rejected the link between you and Bo: no spoke and no tie joins the two of you.
    assert all({e["src"], e["dst"]} != {w.me, w.bo} for e in g["edges"])
    expanded = client.get("/graph/expand", params={"seed": w.me, "node": w.crew, "visible": ids}).json()
    assert all({e["src"], e["dst"]} != {w.me, w.bo} for e in expanded["edges"])


def test_a_pair_you_rejected_stays_out_under_another_address_and_after_a_merge(world, client, db):
    """You said Sam, by his other address, isn't in Atlas, and that Ana isn't in a thread since found to be Atlas: the
    explorer joins neither to Atlas, in the opening view or when Atlas is opened (the reading the derived stages
    write by, graph/derived.py)."""
    w = world
    db.execute(text("""INSERT INTO thread_registry (key, name, terms, status, merged_into) VALUES
                       ('thread:atlas', 'Atlas', '{Atlas}', 'active', NULL),
                       ('thread:atlas-v1', 'Atlas v1', '{Atlas}', 'merged', 'thread:atlas')"""))
    db.execute(text("""INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, source) VALUES
                       ('Person:sam.lee@b.test', 'Project:thread:atlas', 'INVOLVED_IN', 'threads', 'user', 'drop', 'user'),
                       ('Person:ana@acme.test', 'Project:thread:atlas-v1', 'INVOLVED_IN', 'threads', 'user', 'drop',
                        'user')"""))
    db.execute(text("UPDATE graph_version SET version = version + 1"))
    out = {frozenset((w.sam, w.atlas)), frozenset((w.ana, w.atlas))}
    g = client.get("/graph/view").json()
    assert not out & {frozenset((e["src"], e["dst"])) for e in g["edges"]}
    opened = client.get("/graph/expand", params={"seed": w.me, "node": w.atlas, "visible": [w.me]}).json()
    assert not {w.sam, w.ana} & {n["id"] for n in opened["nodes"]}
    assert not out & {frozenset((e["src"], e["dst"])) for e in opened["edges"]}


def test_a_tie_goes_through_the_one_context_two_people_share(world, client, db):
    """Ana and Sam only ever write in the crew: the crew is how they know each other, so their line goes through it.
    Once they write to each other outside it, they are linked directly."""
    w = world

    def ties():
        g = client.get("/graph/view").json()
        return {(e["type"], frozenset((e["src"], e["dst"]))) for e in g["edges"]}
    before = ties()
    assert ("CORRESPONDS", frozenset((w.ana, w.sam))) not in before
    assert {("PART_OF", frozenset((w.ana, w.crew))), ("PART_OF", frozenset((w.sam, w.crew)))} <= before
    for i in range(4):
        a, b = (w.ana, w.sam2) if i % 2 else (w.sam, w.ana)
        _message(db, w, f"direct{i}", a, [b], f"Coffee {i}", 60 + i)
    db.execute(text("UPDATE graph_version SET version = version + 1"))
    assert ("CORRESPONDS", frozenset((w.ana, w.sam))) in ties()


def test_opening_a_node_shows_at_least_a_few_connections(world, client, db):
    """A topic has no members of its own: it opens to what its records link to, a message standing for the group
    chat it is in."""
    w = world
    topic = _node(db, "Topic", "topic:robots", "Robots")
    for m in db.execute(text("""SELECT n.id FROM nodes n WHERE n.key IN ('gmail:crew1', 'gmail:crew2')""")).scalars():
        _edge(db, m, topic, "ABOUT", "llm-extract")
    got = client.get("/graph/expand", params={"seed": w.me, "node": topic, "visible": [w.me]}).json()
    ids = [n["id"] for n in got["nodes"]]
    assert len(ids) >= 3 and w.crew in ids and not set(ids) & (w.hidden | w.aliases | {w.me})


def test_every_edge_says_where_it_came_from(world, client, db):
    w = world
    _edge(db, w.me, w.atlas, "RELATED_TO", "user", why="my project")
    _edge(db, w.ana, w.atlas, "RELATED_TO", "agent:helper")
    db.execute(text("UPDATE graph_version SET version = version + 1"))
    g = client.get("/graph/view").json()
    expanded = client.get("/graph/expand", params={"seed": w.me, "node": w.atlas, "visible": [w.me]}).json()
    for e in g["edges"] + expanded["edges"]:
        assert e["origin"] in ("stated", "derived", "user") and e["stated"] is (e["origin"] != "derived"), e
    spokes = [e for e in g["edges"] if e["src"] == w.me]
    assert spokes and all(e["origin"] == "derived" and not e["stated"] for e in spokes)
    # Who is in the crew is read straight from who wrote there.
    ties = [e for e in g["edges"] if e["type"] == "PART_OF" and e["dst"] == w.crew]
    assert ties and all(e["origin"] == "stated" and e["stated"] for e in ties)
    links = client.get("/graph/explain", params={"a": w.me, "b": w.atlas}).json()["direct"]
    assert [(e["origin"], e["stated"], e["how"]) for e in links] == [("user", True, "You added this.")]
    inferred = client.get("/graph/explain", params={"a": w.ana, "b": w.atlas}).json()["direct"]
    by = {e["extractor"]: e for e in inferred}
    assert (by["agent:helper"]["origin"], by["agent:helper"]["stated"]) == ("agent:helper", False)
    assert by["agent:helper"]["how"] == "Added by helper. Not checked yet."
    assert (by["threads"]["origin"], by["threads"]["stated"]) == ("derived", False)
    assert by["threads"]["how"] == "Suggested automatically from shared words and timing."  # its verdict: keep
    intro = client.get("/graph/explain", params={"a": w.ana, "b": w.sam}).json()["direct"]
    assert [(e["origin"], e["stated"]) for e in intro] == [("derived", False)]
    assert intro[0]["how"] == "Inferred from who wrote to whom, and when. Not checked yet."


def test_find_says_how_much_each_person_talks_with_you_whoever_the_centre_is(world, client):
    w = world
    mine = client.get("/graph/find", params={"q": "ana"}).json()["results"]
    from_sam = client.get("/graph/find", params={"q": "ana", "seed": w.sam2}).json()["results"]
    # The crew's eight messages and the two between you and Ana.
    assert [r["context"] for r in mine if r["id"] == w.ana] == ["10 messages with you"]
    # Centred on Sam, Ana still reads as what she shares with you: Sam's count (8) is never claimed as yours.
    assert [r["context"] for r in from_sam if r["id"] == w.ana] == ["10 messages with you"]
    you = client.get("/graph/find", params={"q": "me myself", "seed": w.sam2}).json()["results"]
    assert [r["context"] for r in you if r["id"] == w.me] == ["You"]
    assert [r["id"] for r in client.get("/graph/find", params={"q": "atl", "types": ["Project"]}).json()["results"]] \
        == [w.atlas]
    people = client.get("/graph/find", params={"q": "robotics", "types": ["Person", "Thread"]}).json()["results"]
    assert [(r["id"], r["type"]) for r in people] == [(w.crew, "Thread")]


@pytest.mark.parametrize(("path", "params"), [
    ("/graph/view", {"per": -20}), ("/graph/view", {"per": 0}), ("/graph/view", {"per": 21}),
    ("/graph/view", {"seed": 0}), ("/graph/expand", {"node": 0}), ("/graph/expand", {"node": 1, "k": 0}),
    ("/graph/expand", {"node": 1, "k": 21}), ("/graph/explain", {"a": 0, "b": 1}), ("/graph/find", {"q": ""}),
    ("/graph/find", {"q": "x" * 201}), ("/graph/find", {"q": "ab", "limit": 0}), ("/graph/find", {"q": "ab", "limit": 21}),
    ("/graph/find", {"q": "ab", "types": ["Message"]}), ("/graph/node/0", {}), ("/graph/node/0/summary", {}),
    ("/graph/node/1/sources", {"limit": 0}), ("/graph/node/1/sources", {"offset": -1}), ("/timeline", {"a": 0}),
    ("/graph/entity/0", {}), ("/items/0", {}),
])
def test_out_of_range_parameters_are_refused(client, path, params):
    assert client.get(path, params=params).status_code == 422


def test_unknown_ids_answer_404(world, client):
    w = world
    missing = 1_099_999
    for path, params in [("/graph/view", {"seed": missing}), ("/graph/expand", {"node": missing}),
                         ("/graph/expand", {"node": w.sam, "seed": missing}),
                         ("/graph/explain", {"a": w.me, "b": missing}), (f"/graph/node/{missing}", {}),
                         (f"/graph/node/{missing}/summary", {}), (f"/graph/node/{missing}/sources", {}),
                         ("/timeline", {"a": missing}), ("/graph/find", {"q": "sam", "seed": missing}),
                         (f"/graph/entity/{missing}", {})]:
        r = client.get(path, params=params)
        assert r.status_code == 404, (path, params, r.status_code)
    assert client.get("/graph/explain", params={"a": w.sam, "b": w.sam2}).status_code == 422  # one person


def test_an_empty_graph_answers_with_why(db):
    client = TestClient(app)
    assert client.get("/graph/view").json() == {"seed": None, "nodes": [], "edges": [], "empty": True,
                                                "reason": "nothing_built"}
    # The owner alone, before anything links to them: nobody to open on yet.
    me = _node(db, "Person", "me", "Me", self=True)
    assert client.get("/graph/view").json()["reason"] == "nothing_built"
    other = _node(db, "Person", "pat@x.test", "Pat")
    assert client.get("/graph/view").json() == {"seed": None, "nodes": [], "edges": [], "empty": True,
                                                "reason": "no_owner"}
    # A centre you ask for always opens, even with nothing around it.
    g = client.get("/graph/view", params={"seed": other}).json()
    assert g["seed"] == other and [n["id"] for n in g["nodes"]] == [other] and g["empty"] is False
    db.execute(text("DELETE FROM nodes WHERE id = :i"), {"i": me})
    assert client.get("/graph/view").json()["reason"] == "no_owner"


def test_a_node_carries_only_the_metadata_a_client_may_see(world, client, db):
    w = world
    secret = {"from": [{"email": "a@x.test"}], "path": "home/secret.txt", "access_token": "TOKEN", "headers": "H",
              "account": "me@x.test", "fingerprint": "fp", "labels": ["INBOX"]}
    m = _message(db, w, "meta1", w.ana, [w.me], "With metadata", 50, meta=secret)
    body = client.get(f"/graph/node/{m}").json()
    assert body["source_item"]["meta"] == {"from": [{"email": "a@x.test"}], "labels": ["INBOX"]}
    for leak in ("TOKEN", "secret.txt", "fingerprint", "raw_meta", '"H"', "account"):
        assert leak not in json.dumps(body), leak
    assert body["source_item"]["text"].startswith("With metadata")
    # The account only picks which Gmail account the original opens in.
    assert body["source_item"]["open_url"] == "https://mail.google.com/mail/?authuser=me@x.test#all/meta1"


def test_dead_routes_are_gone():
    paths = TestClient(app).app.openapi()["paths"]
    for dead in ("/stats", "/decisions", "/relationships/history", "/graph/search", "/graph/subgraph",
                 "/graph/people", "/graph/network", "/graph/node/{node_id}/evidence"):
        assert dead not in paths, dead
    for live in ("/graph/view", "/graph/expand", "/graph/explain", "/graph/find", "/graph/node/{node_id}",
                 "/graph/node/{node_id}/summary", "/graph/node/{node_id}/sources", "/timeline"):
        assert live in paths, live


def test_names_are_labels_not_rewrites_of_ids_and_links(world, client, db):
    """A phone number reads as its contact in a label; ids, keys and links keep their digits."""
    w = world
    pat = _node(db, "Person", "+16145551234", "+16145551234")
    card = _node(db, "Person", "pat@x.test", "Pat Contact", named_by="contacts")
    db.execute(text("INSERT INTO canonical VALUES (:a, :h), (:h, :h)"), {"a": pat, "h": card})
    stranger = _node(db, "Person", "+19995550000", "+19995550000")
    ref = "18c0ffee12345678901234"
    m = _message(db, w, ref, pat, [w.me], "Call +16145551234 or +19995550000", 52, meta={"account": "me@x.test"})
    _edge(db, m, stranger, "RECEIVED")
    db.execute(text("UPDATE graph_version SET version = version + 1"))
    assert client.get(f"/graph/node/{pat}").json()["name"] == "Pat Contact"
    assert client.get(f"/graph/node/{stranger}").json()["name"] == "···0000"
    assert client.get(f"/graph/node/{stranger}").json()["key"] == "+19995550000"
    card_ = next(c for c in client.get(f"/graph/node/{m}/sources").json()["items"] if c["id"] == w["items"][-1])
    assert card_["open_url"].endswith(f"#all/{ref}")  # a Gmail id with a digit run is a link, not a number
    assert card_["snippet"].startswith("Call Pat Contact or +19995550000")  # text names known numbers only


def test_the_timeline_names_counterparts_as_the_explorer_does(world, client, monkeypatch):
    """The local API names every counterpart (a message by its subject); what an outside model may see of it is the
    MCP tool's to decide (test_mcp_policy.py)."""
    from igneous import llm

    w = world
    monkeypatch.setattr(llm.settings, "llm_sources", "")
    edges = client.get("/timeline", params={"a": w.ana}).json()["edges"]
    assert {e["counterpart"]["name"] for e in edges if e["counterpart"]["type"] == "Message"} >= {"Acme plans"}
    people = [e["counterpart"] for e in edges if e["counterpart"]["type"] in ("Person", "Organization")]
    assert people and all(c["name"] for c in people)
