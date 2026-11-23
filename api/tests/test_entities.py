"""Derived projects (graph/entities.py) and topic merges (graph/topics.py), built from records through the runner."""
import pytest
from fastapi.testclient import TestClient
from pipeline_data import day, event, local_models, mail, put, rows  # noqa: F401
from sqlalchemy import text

from igneous import pipeline
from igneous.graph import derived_invariants, topics

pytestmark = pytest.mark.usefixtures("local_models")
ME = "me@x.test"
ENTITIES = ["spine", "chunk", "self", "resolve", "entities"]
WITH_THREADS = [*ENTITIES, "threads"]  # the threads stage runs first: what a thread is about joins it


def build(s, stages=ENTITIES) -> None:
    mail(s, "yours", ME, ["someone@else.test"], "Hello", day(-1), sent=True)  # tells the self stage ME is you
    s.commit()
    pipeline.run(s, stages)


def projects(s) -> list[tuple[str, int]]:
    """(name, number of people in it) for every derived project."""
    return rows(s, """SELECT p.name, (SELECT count(*) FROM edges e WHERE e.dst = p.id AND e.type = 'INVOLVED_IN')
                      FROM nodes p WHERE p.type = 'Project' AND p.props->>'derived' = 'true' ORDER BY 1, 2""")


def doc(s, path: str, body: str, when) -> None:
    """A file in your documents folder, as the Local files connector stores it."""
    put(s, "fs", path, path.rsplit("/", 1)[-1], {"path": path}, when, body)


def bulk(s, ref: str, sender: str, to: str, subject: str, when, body: str, unsubscribe: bool = False) -> None:
    """Mail a person didn't write to you: a newsletter (with an unsubscribe header), a notice, or a list's post."""
    put(s, "gmail", ref, subject, {"from": [{"email": sender}], "to": [{"email": to}], "fingerprint": f"fp-{ref}",
                                   "gmail_thread_id": f"t-{ref}", "labels": ["INBOX"],
                                   **({"list_unsubscribe": "<mailto:leave@list.test>"} if unsubscribe else {})},
        when, body)


def thread(s, key: str, term: str, start, end) -> None:
    """A thread the threads stage found from a burst of `term`, named without the model."""
    s.execute(text("""INSERT INTO thread_registry (key, name, kind, terms, span_start, span_end, status, origin, judged)
                      VALUES (:k, :n, 'thread', ARRAY[CAST(:t AS text)], :a, :b, 'active', 'burst', false)"""),
              {"k": key, "n": f"{term} · Mar 2025", "t": term, "a": start, "b": end})


def held(s, key: str) -> dict[str, set[str]]:
    """What the Project keyed `key` holds: its members' names by the stage that linked each."""
    out: dict[str, set[str]] = {}
    for name, extractor in rows(s, """SELECT n.name, e.extractor FROM edges e JOIN nodes p ON p.id = e.dst
                                       JOIN nodes n ON n.id = e.src WHERE p.key = :k AND e.type = 'PART_OF'""", k=key):
        out.setdefault(name, set()).add(extractor)
    return out


def lisbon(s) -> None:
    """The persona's spring break: mail names Lisbon over two weeks, and its folder holds an itinerary that names it and
    an expenses sheet that doesn't. The airline's booking and a search name it too, as do a fare newsletter, and a
    chat with the same friends months after the trip."""
    thread(s, "thread:lisbon", "Lisbon", day(0), day(14))
    mail(s, "l1", "dana@x.test", [ME], "Lisbon hostel", day(1), body="I booked the hostel in Lisbon for us.")
    mail(s, "l2", ME, ["dana@x.test"], "Re: Lisbon hostel", day(2), sent=True, body="Lisbon, here we come.")
    mail(s, "l3", "sofia@x.test", [ME, "dana@x.test"], "Day trips", day(5), body="Sintra from Lisbon on day 3?")
    doc(s, "Travel/Lisbon 2025/itinerary.md", "# Lisbon itinerary: Alfama, Belem, Sintra.", day(6))
    doc(s, "Travel/Lisbon 2025/expenses.xlsx", "Date What Paid EUR. Taxi 32. Dinner 88.", day(13))
    bulk(s, "booking", "no-reply@flyaway.test", ME, "Your flight is confirmed", day(3), "Booking QX7: Lisbon, March 8.")
    put(s, "takeout_activity", "activity/search/day4", "Search activity", {"product": "Search"}, day(4),
        "[23:08] Searched for lisbon alfama guesthouse")
    bulk(s, "fares", "deals@fares.test", ME, "Fares to Lisbon", day(4), "Fly to Lisbon from $399.", unsubscribe=True)
    mail(s, "later", "dana@x.test", [ME, "sofia@x.test"], "remember Lisbon?", day(190), body="Remember Lisbon? Again?")


def evidence(s, key: str) -> list[tuple[str]]:
    """The records only shown as evidence of the Project keyed `key` (never its members)."""
    return rows(s, """SELECT n.name FROM edges e JOIN nodes n ON n.id = e.src JOIN nodes p ON p.id = e.dst
                      WHERE p.key = :k AND e.type = 'RELATED_TO' AND e.props->>'role' = 'evidence' ORDER BY 1""", k=key)


def test_a_folder_a_thread_is_about_joins_it_instead_of_being_a_second_project(db):
    lisbon(db)
    build(db, WITH_THREADS)
    assert rows(db, "SELECT name FROM nodes WHERE type = 'Project'") == [("Lisbon · Mar 2025",)]
    trip = held(db, "thread:lisbon")
    assert trip["Lisbon 2025"] == trip["expenses.xlsx"] == {"entities"}  # the folder, and its file naming nothing
    assert trip["itinerary.md"] == {"threads"}  # held once, by the stage that found it
    assert not {"Fares to Lisbon", "remember Lisbon?"} & set(trip)  # a newsletter, and another time with them
    # The booking and the search show the trip happened: its evidence, never its members, and a newsletter not even that.
    assert evidence(db, "thread:lisbon") == [("Search activity",), ("Your flight is confirmed",)]
    assert not {"Search activity", "Your flight is confirmed"} & set(trip)
    assert rows(db, "SELECT key LIKE 'project:lisbon 2025:%', status, merged_into FROM thread_registry "
                    "WHERE origin = 'entities'") == [(True, "merged", "thread:lisbon")]
    one_thing = ("joined_project_stands", "evidence_is_member")
    assert [v for v in derived_invariants.check(db) if v.code in one_thing] == []
    db.execute(text("""INSERT INTO nodes (type, key, name, props) SELECT 'Project', key, name, '{"derived": true}'
                       FROM thread_registry WHERE origin = 'entities'"""))  # what check-graph would catch
    assert [v.code for v in derived_invariants.check(db) if v.code in one_thing] == ["joined_project_stands"]
    pipeline.run(db, WITH_THREADS)  # a second run changes nothing (and the folder's Project goes again)
    assert rows(db, "SELECT name FROM nodes WHERE type = 'Project'") == [("Lisbon · Mar 2025",)]
    assert held(db, "thread:lisbon") == trip


def test_a_class_folder_joins_its_class_and_a_name_keeps_its_numbers(db):
    thread(db, "thread:cs362", "CS 362", day(0), day(60))
    for i in range(3):
        mail(db, f"c{i}", "prof@school.test", [ME], f"CS 362 homework {i + 1}", day(10 * i), body="Due Friday.")
    for i in range(2):
        doc(db, f"School/Spring 2025/CS 362 Database Systems/week-0{i + 1}.md", "# CS362 lecture notes", day(7 * i))
        doc(db, f"School/Spring 2025/CS 375 Machine Learning/week-0{i + 1}.md", "# lecture notes", day(7 * i))
    build(db, WITH_THREADS)
    # The semester's folder says when, not what; CS 375, with no thread of its own, is a Project of its own.
    assert projects(db) == [("CS 375 Machine Learning", 0)]
    assert {"CS 362 Database Systems", "week-01.md", "week-02.md"} <= set(held(db, "thread:cs362"))
    (own,) = rows(db, "SELECT key FROM nodes WHERE type = 'Project' AND props->>'derived' = 'true'")[0]
    assert set(held(db, own)) == {"CS 375 Machine Learning", "week-01.md", "week-02.md"}  # its folder and its files


def test_a_project_of_its_own_takes_a_record_naming_it_only_when_the_record_shows_it_belongs(db):
    for i in range(3):
        event(db, f"acct/cal/gn_{i}", "Gloomhaven Night", day(7 * i), ["kai@y.test"], ME, "gn")
    mail(db, "kai", "kai@y.test", [ME], "Snacks", day(3), body="I'll bring snacks to Gloomhaven Night.")
    bulk(db, "promo", "deals@games.test", ME, "Sale", day(4), "Gloomhaven Night expansion, 40% off.", unsubscribe=True)
    bulk(db, "notice", "orders@shop.test", ME, "Shipped", day(5), "Your Gloomhaven Night expansion shipped.")
    bulk(db, "club", "pat@club.test", "members@club.test", "Club news", day(6), "Gloomhaven Night is back at the club.")
    doc(db, "Writing/ideas.md", "Gloomhaven Night: try the new scenario.", day(7))
    build(db)
    key = rows(db, "SELECT key FROM nodes WHERE type = 'Project'")[0][0]
    # Kai's mail shares him and you are in it; a newsletter, a notice, a list post nobody on it wrote to you, and a
    # document outside its folder only name it. The notice, sent to you while it went on, is its evidence.
    assert held(db, key) == {"Gloomhaven Night": {"entities"}, "Snacks": {"entities"}}
    assert evidence(db, key) == [("Shipped",)]


def test_what_you_said_about_a_folders_project_holds_when_a_thread_takes_it_in(db):
    from igneous.main import app

    client = TestClient(app)
    doc(db, "Travel/Lisbon 2025/itinerary.md", "# Lisbon itinerary: Alfama, Belem, Sintra.", day(6))
    doc(db, "Travel/Lisbon 2025/expenses.xlsx", "Date What Paid EUR. Taxi 32. Dinner 88.", day(13))
    doc(db, "Travel/Lisbon 2025/dropped-plan.md", "A Lisbon plan we dropped: Porto first.", day(3))
    build(db, WITH_THREADS)  # no thread yet: the folder is a Project of its own
    folder = rows(db, "SELECT id, key FROM nodes WHERE type = 'Project' AND name = 'Lisbon 2025'")[0]
    dropped = rows(db, """SELECT e.id FROM edges e JOIN nodes f ON f.id = e.src
                          WHERE e.dst = :p AND f.name = 'dropped-plan.md'""", p=folder[0])[0][0]
    renamed = client.post(f"/graph/entity/{folder[0]}/rename", json={"name": "Spring break"}).json()
    client.post("/graph/edge/wrong", json={"edge": dropped})
    client.post(f"/graph/entity/{folder[0]}/hide")
    # The trip turns up in mail: the threads stage finds it, and the folder joins it.
    lisbon(db)
    db.commit()
    pipeline.run(db, WITH_THREADS)
    assert rows(db, "SELECT name, COALESCE((props->>'hidden')::bool, false) FROM nodes WHERE type = 'Project'") == [
        ("Spring break", False)]  # your name holds for the thread; your hide of the duplicate doesn't hide the trip
    trip = rows(db, "SELECT id FROM nodes WHERE key = 'thread:lisbon'")[0][0]
    assert "dropped-plan.md" not in held(db, "thread:lisbon")  # named Lisbon, but you said it isn't part of it
    assert "expenses.xlsx" in held(db, "thread:lisbon")
    shown = client.get(f"/graph/entity/{trip}").json()["corrections"]
    assert [(c["override_id"], c["value"]) for c in shown] == [(renamed["override_id"], "Spring break")]
    assert client.delete(f"/overrides/{renamed['override_id']}").json()["applied"] == "now"
    assert rows(db, "SELECT name FROM nodes WHERE type = 'Project'") == [("Lisbon · Mar 2025",)]


def test_a_project_keeps_its_key_when_its_name_is_read_another_way(db):
    """A Project's key holds a correction: a change in how names are read must not orphan it."""
    for i in range(2):
        doc(db, f"Games/Gloomhaven 2/scenario-{i}.md", "Scenario notes.", day(i))
    build(db)
    (folder,) = rows(db, "SELECT id FROM nodes WHERE type = 'Folder' AND name = 'Gloomhaven 2'")[0]
    old = f"project:gloomhaven:{folder}"  # how the name was read before it kept its numbers
    db.execute(text("UPDATE nodes SET key = :k WHERE type = 'Project'"), {"k": old})
    db.execute(text("INSERT INTO overrides (op, node_keys, value) VALUES ('rename', :k, 'Board games')"),
               {"k": [f"Project:{old}"]})
    pipeline.run(db, ENTITIES)
    assert rows(db, "SELECT key, name FROM nodes WHERE type = 'Project'") == [(old, "Board games")]


def test_an_anchor_keeps_every_reason_it_is_part_of_a_project(db):
    for i in range(3):
        event(db, f"acct/cal/gn_{i}", "Gloomhaven Night", day(7 * i), ["kai@y.test"], ME, "gn")
    build(db)
    why, kinds = db.execute(text("""
        SELECT e.props->>'why', (SELECT array_agg(ev.kind ORDER BY ev.kind) FROM edge_evidence ev WHERE ev.edge_id = e.id)
        FROM edges e JOIN nodes a ON a.id = e.src WHERE e.type = 'PART_OF' AND a.type = 'Event'""")).one()
    assert why == "event series (3); mentions" and kinds == ["event series (3)", "mentions"]


def test_namesakes_are_one_project_with_its_original_casing(db):
    """Three single events titled "Book Club" with different guests (and you on all): one project, as you spell it."""
    for i, guests in enumerate((["ana@q.test"], ["ben@q.test"], ["cy@q.test", "ana@q.test"])):
        event(db, f"acct/cal/bc{i}", "Book Club", day(30 * i), guests, ME, f"bc{i}")
    build(db)
    assert projects(db) == [("Book Club", 1)]  # Ana, on two of them; you are on all three, and never counted


def test_namesakes_whose_people_differ_are_two_projects_and_mentions_follow_the_people(db):
    for i in range(3):
        event(db, f"acct/cal/a{i}", "Book Club", day(7 * i), ["ana@q.test", "ben@q.test"], ME, "work-club")
        event(db, f"acct/cal/b{i}", "Book Club", day(7 * i + 1), ["cy@r.test", "dee@r.test"], ME, "street-club")
    mail(db, "m1", "ana@q.test", [ME], "Next book", day(30), body="For the Book Club next week: Middlemarch?")
    build(db)
    assert projects(db) == [("Book Club", 0), ("Book Club", 1)]  # Ana: her club's series and her mail about it
    mentioned_in = rows(db, """SELECT a.key FROM edges e JOIN nodes m ON m.id = e.src AND m.type = 'Message'
                               JOIN edges anchor ON anchor.dst = e.dst AND anchor.type = 'PART_OF'
                               JOIN nodes a ON a.id = anchor.src AND a.type = 'Event' WHERE e.type = 'PART_OF'""")
    assert mentioned_in == [("gcal:work-club",)]


def test_filesystem_roots_and_generic_folders_never_become_projects(db):
    root = "f42ad2cb2633"
    for path in ("a.txt", "b.txt", "files/c.txt", "files/d.txt", "Thesis/e.txt", "Thesis/f.txt"):
        parts = f"{root}/{path}".split("/")
        folders = ["/".join(parts[:i]) for i in range(1, len(parts))]
        put(db, "fs", f"{root}/{path}", parts[-1], {"path": f"{root}/{path}", "folders": folders}, day(0),
            body="notes")
    build(db)
    assert [name for name, _people in projects(db)] == ["Thesis"]


def test_topic_merges_never_collide(db):
    """Two spellings of one topic, both on one message from one extractor: one topic, one ABOUT edge."""
    mail(db, "m1", "ann@x.test", ["bo@x.test"], "Caps", day(0))
    build(db, ["spine"])
    message = db.execute(text("SELECT id FROM nodes WHERE type = 'Message' AND key = 'fp-m1'")).scalar_one()
    for key, name in (("price-caps", "Price caps"), ("price-cap", "price cap"), ("price-cap-rule", "price cap rule")):
        topic = db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Topic', :k, :n) RETURNING id"),
                           {"k": key, "n": name}).scalar_one()
        db.execute(text("INSERT INTO edges (src, dst, type, extractor) VALUES (:m, :t, 'ABOUT', 'llm-extract')"),
                   {"m": message, "t": topic})
    result = topics.run(db, threshold=1.01)  # exact keys only
    assert result["topics_merged"] == 1
    assert rows(db, "SELECT key, name FROM nodes WHERE type = 'Topic' ORDER BY key") == [
        ("price-cap", "price cap"), ("price-cap-rule", "price cap rule")]
    assert db.execute(text("SELECT count(*) FROM edges WHERE type = 'ABOUT'")).scalar() == 2
