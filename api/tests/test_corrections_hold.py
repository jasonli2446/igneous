"""Corrections hold through every rebuild (#208): on every kind of derived Project, across thread merges and a
person's other addresses, and while a rebuild writes; and every correction answers with what it changed and the id
that undoes it."""
import json

import life_threads
import pytest
from fastapi.testclient import TestClient
from life_threads import life  # noqa: F401
from pipeline_data import day, event, local_models, mail  # noqa: F401
from sqlalchemy import text

from igneous import llm, pipeline
from igneous.graph import backbone, corrections, threads, verify
from igneous.graph.derived import Writer
from igneous.main import app

client = TestClient(app)


def project(s, name: str) -> int | None:
    return s.execute(text("SELECT id FROM nodes WHERE type = 'Project' AND name = :n"), {"n": name}).scalar()


def node(s, key: str) -> int:
    return s.execute(text("SELECT id FROM nodes WHERE key = :k"), {"k": key}).scalar_one()


def name_of(s, nid: int) -> str:
    return s.execute(text("SELECT name FROM nodes WHERE id = :i"), {"i": nid}).scalar()


def hidden(s, nid: int) -> bool:
    return bool(s.execute(text("SELECT COALESCE((props->>'hidden')::bool, false) FROM nodes WHERE id = :i"),
                          {"i": nid}).scalar())


def records(s, nid: int) -> set[int]:
    """The records a Project holds (its PART_OF members' source items)."""
    return set(s.execute(text("""SELECT n.source_item_id FROM edges e JOIN nodes n ON n.id = e.src
                                 WHERE e.dst = :p AND e.type = 'PART_OF' AND n.source_item_id IS NOT NULL"""),
                         {"p": nid}).scalars())


def linked(s, people: list[int], nid: int) -> int:
    return s.execute(text("""SELECT count(*) FROM edges WHERE dst = :p AND src = ANY(:a)
                             AND extractor NOT IN ('user')"""), {"p": nid, "a": people}).scalar()


# --- One Project contract: your renames, hides and merges hold on every kind of derived Project ----------------

@pytest.mark.usefixtures("life")
def test_renames_and_hides_hold_on_threads_and_org_threads_and_undo_brings_back_what_was_derived(db):
    pipeline.run(db)
    portland, lisbon, acme = (project(db, n) for n in ("Portland trip", "Lisbon trip", "Acme Robotics"))
    renamed = client.post(f"/graph/entity/{portland}/rename", json={"name": "Our Portland weekend"}).json()
    hide = client.post(f"/graph/entity/{lisbon}/hide").json()
    org = client.post(f"/graph/entity/{acme}/rename", json={"name": "Acme internship"}).json()
    assert renamed["applied"] == hide["applied"] == org["applied"] == "now"
    pipeline.run(db)  # threads, then the backbone that names the org's thread, write them again
    assert (name_of(db, portland), hidden(db, lisbon), name_of(db, acme)) == ("Our Portland weekend", True,
                                                                             "Acme internship")
    shaped = client.get(f"/graph/entity/{portland}").json()["corrections"]
    assert [(c["override_id"], c["op"], c["value"]) for c in shaped] == [(renamed["override_id"], "rename",
                                                                          "Our Portland weekend")]
    assert [c["op"] for c in client.get(f"/graph/entity/{lisbon}").json()["corrections"]] == ["hide"]
    for correction in (renamed, hide, org):
        assert client.delete(f"/overrides/{correction['override_id']}").json()["applied"] == "now"
    assert (name_of(db, portland), hidden(db, lisbon), name_of(db, acme)) == ("Portland trip", False, "Acme Robotics")
    pipeline.run(db)
    assert (name_of(db, portland), hidden(db, lisbon), name_of(db, acme)) == ("Portland trip", False, "Acme Robotics")


def test_a_project_holds_what_its_stage_derives_now_and_what_others_set_on_it(db):
    """A stage writes a Project's props afresh (one it no longer derives goes); your name and flags, and the
    backbone's marks, stay."""
    w = Writer(db, "threads")
    nid = w.project("thread:x", "Trip", {"derived": "threads", "kind": "trip", "suggested": True})
    db.execute(text("""UPDATE nodes SET name = 'My trip', props = props || '{"renamed": true, "org": 7, "x": 1}'
                       WHERE id = :i"""), {"i": nid})
    assert w.project("thread:x", "Summer trip", {"derived": "threads", "kind": "trip"}) == nid
    assert db.execute(text("SELECT name, props FROM nodes WHERE id = :i"), {"i": nid}).one() == (
        "My trip", {"derived": "threads", "kind": "trip", "original_name": "Summer trip", "renamed": True, "org": 7})


def test_your_hide_of_a_thread_the_backbone_also_hid_outlives_the_backbone(db):
    """The backbone marks a thread named like an org as the org's duplicate (its own flag); your hide is yours."""
    me = db.execute(text("INSERT INTO nodes (type, key, name, props) VALUES ('Person', 'me', 'Mo', '{\"self\": true}') "
                         "RETURNING id")).scalar_one()
    org = db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Organization', 'org:robotics-lab', "
                          "'Robotics Lab') RETURNING id")).scalar_one()
    db.execute(text("INSERT INTO edges (src, dst, type, extractor) VALUES (:m, :o, 'WORKED_AT', 'profile')"),
               {"m": me, "o": org})
    big, small = (db.execute(text("""INSERT INTO nodes (type, key, name, props) VALUES ('Project', :k, 'Robotics Lab',
                                     CAST(:p AS jsonb)) RETURNING id"""),
                             {"k": f"thread:{k}", "p": json.dumps({"derived": "threads", "records": n})}).scalar_one()
                  for k, n in (("big", 9), ("small", 3)))
    backbone.run(db, log=None)
    corrections.apply_all(db)
    assert not hidden(db, big) and hidden(db, small)  # the duplicate leaves the view
    client.post(f"/graph/entity/{small}/hide")
    db.execute(text("UPDATE nodes SET name = 'Lab offsite', props = props || '{\"original_name\": \"Lab offsite\"}' "
                    "WHERE id = :i"), {"i": small})  # its records now say it's something else
    backbone.run(db, log=None)
    corrections.apply_all(db)
    assert hidden(db, small)


@pytest.mark.usefixtures("life")
def test_merging_threads_keeps_every_member_through_rebuilds_and_undo_gives_both_back(db):
    pipeline.run(db)
    portland, lisbon = project(db, "Portland trip"), project(db, "Lisbon trip")
    mine, theirs = records(db, portland), records(db, lisbon)
    merge = client.post(f"/graph/entity/{portland}/merge", json={"other": lisbon}).json()
    assert merge["applied"] == "now" and merge["override_id"]
    assert project(db, "Lisbon trip") is None and records(db, portland) == mine | theirs
    pipeline.run(db)  # the folded thread is a facet now: its records keep coming to the one you kept
    assert project(db, "Lisbon trip") is None and records(db, portland) == mine | theirs
    assert db.execute(text("SELECT status, merged_by FROM thread_registry WHERE name = 'Lisbon trip'")).one() \
        == ("merged", "user")
    assert "Lisbon trip" in db.execute(text("SELECT props->'aliases' FROM nodes WHERE id = :p"),
                                       {"p": portland}).scalar()
    # The panel offers to undo it, naming what was folded in.
    shaped = client.get(f"/graph/entity/{portland}").json()["corrections"]
    assert [(c["override_id"], c["op"], c["value"]) for c in shaped] == [(merge["override_id"], "merge", "Lisbon trip")]
    assert client.delete(f"/overrides/{merge['override_id']}").json()["applied"] == "now"
    lisbon = project(db, "Lisbon trip")
    assert lisbon and records(db, lisbon) == theirs and records(db, portland) == mine
    pipeline.run(db)
    assert records(db, project(db, "Lisbon trip")) == theirs and records(db, portland) == mine


# --- A rejection holds: while a rebuild writes, through a person's other address, after a merge -----------------

@pytest.mark.usefixtures("life")
def test_a_rejected_link_never_comes_back_mid_rebuild_through_another_address_or_after_a_merge(db, monkeypatch):
    from igneous.graph import derived_invariants

    pipeline.run(db)
    portland, lisbon = project(db, "Portland trip"), project(db, "Lisbon trip")
    ana = [node(db, "+15550000001"), node(db, "ana@pals.test")]  # one person: her contact card joins them
    edge = db.execute(text("SELECT id FROM edges WHERE dst = :p AND src = ANY(:a) AND type = 'INVOLVED_IN'"),
                      {"p": portland, "a": ana}).scalar_one()
    wrong = client.post("/graph/edge/wrong", json={"edge": edge}).json()
    assert wrong["applied"] == "now" and linked(db, ana, portland) == 0
    # Her mail address becomes the busier one, so the head of her identity (whom threads link) changes.
    for i in range(16):
        life_threads._mail(db, f"ana-more{i}", "ana@pals.test", [life_threads.ME], f"lunch {i}",
                           life_threads.at(300 + i), "lunch at noon?")
    db.commit()
    seen = []
    real = backbone.run

    def after_threads(session, **options):  # the next stage reads exactly what threads wrote
        seen.append(linked(session, ana, portland))
        return real(session, **options)

    monkeypatch.setattr(backbone, "run", after_threads)
    pipeline.run(db)
    assert db.execute(text("SELECT canonical_id FROM canonical WHERE node_id = :n"), {"n": ana[0]}).scalar() == ana[1]
    assert seen == [0] and linked(db, ana, portland) == 0
    # A rejection on a thread holds for the thread it is merged into.
    cy = [node(db, "+15550000003"), node(db, "cy@pals.test")]
    cy_edge = db.execute(text("SELECT id FROM edges WHERE dst = :p AND src = ANY(:a) AND type = 'INVOLVED_IN'"),
                         {"p": lisbon, "a": cy}).scalar_one()
    client.post("/graph/edge/wrong", json={"edge": cy_edge})
    client.post(f"/graph/entity/{portland}/merge", json={"other": lisbon})
    assert linked(db, cy, portland) == 0
    pipeline.run(db)
    assert linked(db, cy, portland) == 0 and linked(db, ana, portland) == 0
    assert [v for v in derived_invariants.check(db) if v.code == "rejected_pair_linked"] == []


def test_the_writer_skips_a_rejected_pair_through_identity_heads_and_merges(db):
    """graph/derived.py on its own: a rejection by natural key blocks the pair under any address of the person and
    for the Project the rejected one was folded into, in either direction."""
    def add(type_, key, name=None):
        return db.execute(text("INSERT INTO nodes (type, key, name) VALUES (:t, :k, :n) RETURNING id"),
                          {"t": type_, "k": key, "n": name or key}).scalar_one()

    head, other, thread, survivor = (add("Person", "a@x.test"), add("Person", "+1555"), add("Project", "thread:fold"),
                                     add("Project", "thread:keep"))
    db.execute(text("INSERT INTO canonical (node_id, canonical_id) VALUES (:o, :h), (:h, :h)"), {"o": other, "h": head})
    db.execute(text("""INSERT INTO thread_registry (key, name, terms, status, merged_into) VALUES
                       ('fold', 'Fold', '{fold}', 'merged', 'keep'), ('keep', 'Keep', '{keep}', 'active', NULL)"""))
    db.execute(text("UPDATE nodes SET key = 'fold' WHERE id = :i"), {"i": thread})
    db.execute(text("UPDATE nodes SET key = 'keep' WHERE id = :i"), {"i": survivor})
    db.execute(text("""INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, source)
                       VALUES ('Person:+1555', 'Project:fold', 'INVOLVED_IN', 'threads', 'user', 'drop', 'user')"""))
    w = Writer(db, "threads")
    assert w.edge(head, survivor, "INVOLVED_IN") is None and w.edge(survivor, other, "RELATED_TO") is None
    assert w.edge(head, add("Project", "thread:other"), "INVOLVED_IN") is not None and w.skipped == 2
    assert w.rejected.heads() == {frozenset((head, survivor))}  # as the explorer reads it


@pytest.mark.usefixtures("local_models")
def test_a_message_you_linked_leaves_with_its_record_and_the_link_comes_back_with_it(db):
    """A link you added is an intent: it never keeps a message whose record is gone (that would be a message built
    from no record), and it is made again when the record comes back."""
    from igneous.graph.invariants import check_graph

    mail(db, "plans", "ann@x.test", ["mo@x.test"], "Trip plans", day(1), body="Flights are booked.")
    pipeline.run(db)
    message = db.execute(text("SELECT id FROM nodes WHERE type = 'Message'")).scalar_one()
    trip = db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Project', 'project:trip', 'Trip') "
                           "RETURNING id")).scalar_one()
    assert client.post("/graph/link", json={"src": message, "dst": trip, "type": "PART_OF"}).json()["id"]
    db.execute(text("DELETE FROM source_items WHERE external_id = 'plans'"))  # a sync saw it deleted
    pipeline.run(db)
    assert db.execute(text("SELECT count(*) FROM nodes WHERE type = 'Message'")).scalar() == 0
    assert "phantom_message" not in {v.code for v in check_graph(db)}
    mail(db, "plans", "ann@x.test", ["mo@x.test"], "Trip plans", day(1), body="Flights are booked.")
    pipeline.run(db)
    assert db.execute(text("""SELECT count(*) FROM edges e JOIN nodes m ON m.id = e.src
                              WHERE m.type = 'Message' AND e.dst = :t AND e.extractor = 'user'"""),
                      {"t": trip}).scalar() == 1


def test_a_dropped_link_whose_record_is_gone_is_not_brought_back(db):
    """Your 'keep' on a link the verifier dropped ('This is right' landing while a rebuild drops it), after the record
    behind it was deleted: your verdict stands, and the link stays out (a rebuild makes it again only from records
    that remain)."""
    person = db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Person', 'pat@x.test', 'Pat') "
                             "RETURNING id")).scalar_one()
    trip = db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Project', 'thread:trip', 'Trip') "
                           "RETURNING id")).scalar_one()
    record = db.execute(text("""INSERT INTO source_items (source, external_id, content_hash, title)
                                VALUES ('gmail', 'r1', 'r1', 'Trip?') RETURNING id""")).scalar_one()
    db.execute(text("""INSERT INTO edges (src, dst, type, extractor, source_item_id)
                       VALUES (:p, :t, 'INVOLVED_IN', 'threads', :r)"""), {"p": person, "t": trip, "r": record})
    db.execute(text("""INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, confidence, source)
                       VALUES ('Person:pat@x.test', 'Project:thread:trip', 'INVOLVED_IN', 'threads', 'd', 'drop', 0.9,
                               'model')"""))
    corrections.apply_all(db)
    dropped = db.execute(text("SELECT src_key, dst_key, type, extractor FROM edges_dropped")).one()
    db.execute(text("DELETE FROM source_items WHERE id = :r"), {"r": record})
    verify.record_correction(db, *dropped, "keep", "you said this connection is right")
    corrections.apply_all(db)
    assert db.execute(text("SELECT count(*) FROM edges_dropped")).scalar() == 0
    assert db.execute(text("SELECT count(*) FROM edges")).scalar() == 0
    assert db.execute(text("SELECT verdict, source FROM edge_verdicts")).one() == ("keep", "user")


# --- What the verifier dropped is archived once, by natural key, and leaves with its verdict --------------------

@pytest.mark.usefixtures("local_models")
def test_a_dropped_link_is_archived_once_by_natural_key_and_goes_when_you_say_it_is_right(db, monkeypatch):
    me = "me@x.test"
    for i in range(3):
        event(db, f"acct/cal/gn_{i}", "Gloomhaven Night", day(7 * i), ["kai@y.test", "lu@z.test"], me, "gn")
    for i in range(2):
        mail(db, f"gn{i}", me, ["kai@y.test"], f"Gloomhaven Night {i}", day(3 + i), sent=True,
             body="Are we still on for Gloomhaven Night this week? I'll bring the expansion.")
        mail(db, f"mo{i}", "mo@q.test", [me], f"Gloomhaven Night recap {i}", day(5 + i),
             body="Gloomhaven Night was fun, wish I could have come.")
    db.commit()

    def model(system, user, schema, **_options):  # drops every link it is asked about
        if "edges" in schema["properties"]:
            return {"edges": [{"edge": i, "verdict": "drop", "confidence": 0.9, "reason": "not shown"}
                              for i in range(user.count("### Edge "))]}, {}
        return life_threads.model(system, user, schema)

    monkeypatch.setattr(llm, "structured", model)
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail,gcal")
    for _ in range(3):
        pipeline.run(db)
    archived = db.execute(text("""SELECT src_key, dst_key, type, extractor, count(*) FROM edges_dropped
                                  GROUP BY 1, 2, 3, 4""")).all()
    assert archived and all(n == 1 for *_k, n in archived)
    mo, *key = db.execute(text("SELECT id, src_key, dst_key, type, extractor FROM edges_dropped "
                               "WHERE src_key = 'Person:mo@q.test'")).one()
    # 'This is right' landing while a rebuild drops the link: the final step brings it back with your 'keep'.
    verify.record_correction(db, *key, "keep", "you said this connection is right")
    corrections.apply_all(db)
    assert db.execute(text("SELECT count(*) FROM edges WHERE id = :i"), {"i": mo}).scalar() == 1
    assert db.execute(text("SELECT count(*) FROM edges_dropped WHERE src_key = 'Person:mo@q.test'")).scalar() == 0
    pipeline.run(db)  # your 'keep' is a label: the verifier never drops it again
    assert db.execute(text("""SELECT e.props->>'verdict', e.props->>'verified_by' FROM edges e JOIN nodes p
                              ON p.id = e.src WHERE p.key = 'mo@q.test' AND e.type = 'INVOLVED_IN'""")).one() \
        == ("keep", "user")


# --- Umbrellas keep their identity, and the hierarchy you draw is stored as yours ---------------------------------

def test_an_umbrella_is_upserted_and_a_link_you_drew_to_it_survives_rebuilds(db, monkeypatch):
    from igneous.graph.derived import Writer

    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    monkeypatch.setattr(threads, "_is_umbrella", lambda term, names, months, shown: term == "Robotics Lab")
    for key, term in (("kiosk", "Kiosk"), ("atlas", "Atlas")):
        db.execute(text("""INSERT INTO thread_registry (key, name, kind, terms, status, origin, judged)
                           VALUES (:k, :n, 'project', ARRAY[:n], 'active', 'gold', true)"""), {"k": f"thread:{key}", "n": term})
        for i in range(8):
            sid = db.execute(text("""INSERT INTO source_items (source, external_id, content_hash, title, body_text,
                                     occurred_at) VALUES ('gmail', :e, :e, :t, :b, :w) RETURNING id"""),
                             {"e": f"{key}{i}", "t": f"{term} update {i}", "b": f"Robotics Lab work on {term}",
                              "w": day(30 * i)}).scalar_one()
            db.execute(text("INSERT INTO nodes (type, key, name, source_item_id) VALUES ('Message', :k, :t, :s)"),
                       {"k": f"gmail:{key}{i}", "t": f"{term} update {i}", "s": sid})
    threads.run(db, log=None)
    lab = project(db, "Robotics Lab")
    kiosk = project(db, "Kiosk")
    assert lab and db.execute(text("SELECT count(*) FROM edges WHERE dst = :l AND extractor = 'hierarchy'"),
                              {"l": lab}).scalar() == 2
    side = Writer(db, "threads").project("thread:side", "Side project", {"derived": "threads"})
    drawn = client.post("/graph/link", json={"src": side, "dst": lab, "type": "PART_OF"}).json()
    assert drawn["applied"] == "now" and drawn["id"]
    assert db.execute(text("SELECT child_key, parent_key, origin FROM thread_parents")).one() == \
        ("thread:side", "umbrella:robotics lab", "user")
    threads.run(db, log=None)
    assert project(db, "Robotics Lab") == lab  # the same node: upserted, not recreated
    assert db.execute(text("SELECT count(*) FROM edges WHERE dst = :l AND extractor = 'user'"), {"l": lab}).scalar() == 0
    corrections.apply_all(db)  # Side project has no records, so only the stated hierarchy keeps it (last step)
    assert kiosk == project(db, "Kiosk")


# --- Correction answers: what changed on screen, and the id that undoes it -------------------------------------

def _pair_graph(db):
    """A person, a project and a group chat, with derived links between person and project and between chat and
    project, and a stated link (a group chat member) between chat and person."""
    def add(type_, key):
        return db.execute(text("INSERT INTO nodes (type, key, name) VALUES (:t, :k, :k) RETURNING id"),
                          {"t": type_, "k": key}).scalar_one()

    person, thread, chat = add("Person", "pat@x.test"), add("Project", "thread:trip"), add("Thread", "chat:trip")
    derived = db.execute(text("""INSERT INTO edges (src, dst, type, extractor) VALUES (:a, :t, 'INVOLVED_IN', 'threads')
                                 RETURNING id"""), {"a": person, "t": thread}).scalar_one()
    corrections.link(db, "Project:thread:trip", "Person:pat@x.test", "RELATED_TO", {"why": "you added this"})
    db.execute(text("""INSERT INTO edges (src, dst, type, props, extractor)
                       VALUES (:t, :a, 'RELATED_TO', '{"why": "you added this"}', 'user')"""), {"a": person, "t": thread})
    only = db.execute(text("""INSERT INTO edges (src, dst, type, extractor) VALUES (:c, :t, 'PART_OF', 'threads')
                              RETURNING id"""), {"c": chat, "t": thread}).scalar_one()
    return person, thread, chat, derived, only


def test_wrong_says_per_pair_whether_a_link_still_joins_it(db):
    person, thread, chat, derived, only = _pair_graph(db)
    wrong = client.post("/graph/edge/wrong", json={"edge": derived}).json()
    # The link you added still joins them: the explorer draws a link you added even between a pair you rejected.
    assert wrong["pairs"] == [{"a": min(person, thread), "b": max(person, thread), "still_linked": True}]
    # A link read from your data still joins chat and project, but nothing else is drawn between a pair you rejected.
    db.execute(text("INSERT INTO edges (src, dst, type, extractor) VALUES (:c, :t, 'MENTIONS', 'spine')"),
               {"c": chat, "t": thread})
    alone = client.post("/graph/edge/wrong", json={"edge": only}).json()
    assert alone["pairs"] == [{"a": min(chat, thread), "b": max(chat, thread), "still_linked": False}]


def test_undo_says_per_pair_whether_a_link_joins_it_again(db):
    """Undoing a link taken out or a link you added answers with its pairs too, so a view puts back or takes out
    exactly those; undoing a rename touches no pair."""
    _person, thread, chat, _derived, only = _pair_graph(db)
    pair = {"a": min(chat, thread), "b": max(chat, thread)}
    alone = client.post("/graph/edge/wrong", json={"edge": only}).json()
    back = client.delete(f"/overrides/{alone['override_id']}").json()
    assert back["applied"] == "now" and back["op"] == "unlink" and back["pairs"] == [{**pair, "still_linked": True}]
    wrong = client.post("/graph/edge/wrong", json={"edge": only}).json()
    added = client.post("/graph/link", json={"src": chat, "dst": thread, "type": "PART_OF"}).json()
    assert added["pairs"] == [{**pair, "still_linked": True}]  # your link joins a pair whose inferred links you rejected
    assert client.delete(f"/overrides/{added['override_id']}").json()["pairs"] == [{**pair, "still_linked": False}]
    assert client.delete(f"/overrides/{wrong['override_id']}").json()["pairs"] == [{**pair, "still_linked": True}]
    renamed = client.post(f"/graph/entity/{thread}/rename", json={"name": "The trip"}).json()
    assert client.delete(f"/overrides/{renamed['override_id']}").json()["pairs"] == []


def test_a_pair_is_answered_as_the_people_the_explorer_shows(db):
    """A link from another address of a person is answered as that person, their identity head."""
    person, thread, _chat, _derived, _only = _pair_graph(db)
    alias = db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Person', 'pat@y.test', 'Pat') RETURNING id")
                       ).scalar_one()
    db.execute(text("INSERT INTO canonical (node_id, canonical_id) VALUES (:h, :h), (:a, :h)"), {"a": alias, "h": person})
    edge = db.execute(text("""INSERT INTO edges (src, dst, type, extractor) VALUES (:a, :t, 'RELATED_TO', 'threads')
                              RETURNING id"""), {"a": alias, "t": thread}).scalar_one()
    wrong = client.post("/graph/edge/wrong", json={"edge": edge}).json()
    # Still linked: the link you added joins the person (under their other address) and the project.
    assert wrong["pairs"] == [{"a": min(person, thread), "b": max(person, thread), "still_linked": True}]


@pytest.mark.usefixtures("local_models")
def test_a_link_you_added_can_be_taken_out_kept_out_through_rebuilds_and_brought_back(db):
    person, thread, _chat, derived, _only = _pair_graph(db)
    assert client.delete(f"/graph/link/{derived}").status_code == 400  # inferred: say it is wrong instead
    assert client.delete("/graph/link/999999").status_code == 404
    added = client.post("/graph/link", json={"src": person, "dst": thread, "type": "INVOLVED_IN"}).json()
    assert added["applied"] == "now" and added["id"] and added["override_id"]
    out = client.delete(f"/graph/link/{added['id']}").json()
    assert out["applied"] == "now" and out["override_id"]
    # The inferred link and the one you added earlier still join them.
    assert out["pairs"] == [{"a": min(person, thread), "b": max(person, thread), "still_linked": True}]
    assert db.execute(text("SELECT count(*) FROM edges WHERE id = :e"), {"e": added["id"]}).scalar() == 0

    def yours() -> int:
        return db.execute(text("""SELECT count(*) FROM edges WHERE src = :a AND dst = :t AND extractor = 'user'
                                  AND type = 'INVOLVED_IN'"""), {"a": person, "t": thread}).scalar()

    pipeline.run(db)
    assert yours() == 0
    assert client.delete(f"/overrides/{out['override_id']}").json()["applied"] == "now"
    assert yours() == 1
    pipeline.run(db)
    assert yours() == 1


@pytest.mark.usefixtures("local_models")
def test_not_the_same_person_splits_them_on_every_rebuild_whatever_joins_them_and_undo_joins_them(db):
    from igneous.graph import resolve

    a = db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Person', 'sam@a.test', 'Sam Lee') RETURNING id")
                   ).scalar_one()
    b = db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Person', 'sam@b.test', 'Sam Lee') RETURNING id")
                   ).scalar_one()
    db.execute(text("INSERT INTO edges (src, dst, type, extractor) VALUES (:a, :b, 'SAME_AS', 'contacts')"),
               {"a": a, "b": b})  # a shared contact card
    merged = client.post(f"/graph/person/{a}/merge", json={"other": b}).json()  # and you said so too
    assert merged["override_id"] and merged["person"] in (a, b)
    # Split off the identity the person isn't shown as, by its key, as their panel lists it.
    other = "sam@b.test" if merged["person"] == a else "sam@a.test"
    assert [i["key"] for i in client.get(f"/graph/node/{a}").json()["identities"]] == [other]
    split = client.post(f"/graph/person/{a}/split", json={"key": other}).json()
    # The card still joins them, but nothing is drawn between two people you said are not one.
    assert split["applied"] == "now" and split["override_id"]
    assert split["pairs"] == [{"a": min(a, b), "b": max(a, b), "still_linked": False}]
    assert db.execute(text("SELECT count(*) FROM edges WHERE extractor = 'contacts'")).scalar() == 1
    assert db.execute(text("SELECT count(*) FROM canonical")).scalar() == 0
    assert db.execute(text("SELECT count(*) FROM overrides WHERE op = 'link'")).scalar() == 0
    assert frozenset((a, b)) in resolve.rejected(db, yours=True)
    resolve.run(db, rescore=True)
    corrections.apply_all(db)
    assert db.execute(text("SELECT count(*) FROM canonical")).scalar() == 0
    assert client.delete(f"/overrides/{split['override_id']}").json()["applied"] == "now"
    assert db.execute(text("SELECT count(DISTINCT canonical_id) FROM canonical")).scalar() == 1
    assert db.execute(text("SELECT count(*) FROM overrides WHERE op = 'link' AND value = 'SAME_AS'")).scalar() == 1
    # Only one of the person's own other identities can be split off: never the one they are shown as, nor anyone
    # else's address.
    head = db.execute(text("SELECT DISTINCT canonical_id FROM canonical")).scalar_one()
    shown_as = db.execute(text("SELECT key FROM nodes WHERE id = :h"), {"h": head}).scalar_one()
    db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Person', 'sam@c.test', 'Sam Lee')"))
    for key in (shown_as, "sam@c.test", "nobody@x.test"):
        assert client.post(f"/graph/person/{head}/split", json={"key": key}).status_code == 404
    assert db.execute(text("SELECT count(*) FROM overrides WHERE op = 'unlink'")).scalar() == 0


def test_undoing_an_older_rejection_never_restores_a_link_a_later_one_covers(db):
    """Undoing an older rejection never brings back a link a later one still rejects (the pair stays covered)."""
    person, thread, _chat, derived, _only = _pair_graph(db)
    first = client.post("/graph/edge/wrong", json={"edge": derived}).json()
    db.execute(text("""INSERT INTO edges (src, dst, type, extractor) VALUES (:a, :t, 'RELATED_TO', 'threads')"""),
               {"a": person, "t": thread})
    db.execute(text("""INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, source)
                       VALUES ('Person:pat@x.test', 'Project:thread:trip', 'RELATED_TO', 'threads', 'user', 'drop',
                               'user')"""))
    client.delete(f"/overrides/{first['override_id']}")
    assert db.execute(text("SELECT count(*) FROM edges WHERE extractor = 'threads' AND src = :a"),
                      {"a": person}).scalar() == 0
