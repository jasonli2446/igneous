"""Derived projects (graph/entities.py) and topic merges (graph/topics.py), built from records through the runner."""
import pytest
from pipeline_data import day, event, local_models, mail, put, rows  # noqa: F401
from sqlalchemy import text

from igneous import pipeline
from igneous.graph import topics

pytestmark = pytest.mark.usefixtures("local_models")
ME = "me@x.test"
ENTITIES = ["spine", "chunk", "self", "resolve", "entities"]


def build(s, stages=ENTITIES) -> None:
    mail(s, "yours", ME, ["someone@else.test"], "Hello", day(-1), sent=True)  # tells the self stage ME is you
    s.commit()
    pipeline.run(s, stages)


def projects(s) -> list[tuple[str, int]]:
    """(name, number of people in it) for every derived project."""
    return rows(s, """SELECT p.name, (SELECT count(*) FROM edges e WHERE e.dst = p.id AND e.type = 'INVOLVED_IN')
                      FROM nodes p WHERE p.type = 'Project' AND p.props->>'derived' = 'true' ORDER BY 1, 2""")


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
