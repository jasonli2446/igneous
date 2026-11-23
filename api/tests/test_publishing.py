"""The derived stages publish in one transaction: a reader keeps the previous graph until the new one is committed,
a failed rebuild publishes nothing but keeps what the model was paid for, and corrections never interleave with it."""
import pytest
from fastapi.testclient import TestClient
from pipeline_data import card, day, event, local_models, mail  # noqa: F401
from sqlalchemy import text

from igneous import llm, pipeline
from igneous.db import engine
from igneous.graph import dirty, organizations, overrides, verify

pytestmark = pytest.mark.usefixtures("local_models")
ME = "me@x.test"


def derived(c) -> dict:
    """What any connection sees of the derived graph."""
    return {"version": c.execute(text("SELECT version FROM graph_version")).scalar(),
            "projects": sorted(c.execute(text("SELECT name FROM nodes WHERE type = 'Project'")).scalars()),
            "identities": c.execute(text("SELECT count(*) FROM canonical")).scalar(),
            "involved": c.execute(text("SELECT count(*) FROM edges WHERE type = 'INVOLVED_IN'")).scalar()}


def first_build(s) -> None:
    for i in range(3):
        event(s, f"acct/cal/gn_{i}", "Gloomhaven Night", day(7 * i), ["kai@y.test", "lu@z.test"], ME, "gn")
    for i in range(2):
        mail(s, f"gn{i}", ME, ["kai@y.test"], f"Gloomhaven Night {i}", day(3 + i), sent=True,
             body="Are we still on for Gloomhaven Night this week? I'll bring the expansion.")
    s.commit()
    pipeline.run(s)


def new_records(s) -> None:
    """A second series that becomes a project, and a contact card that makes Kai's two addresses one person."""
    for i in range(3):
        event(s, f"acct/cal/bc_{i}", "Book Club", day(30 + 7 * i), ["kai@y.test", "ann@q.test"], ME, "bc")
    card(s, "acct/people/1", "Kai Yu", emails=["kai@y.test", "kai@home.test"])
    mail(s, "k1", "kai@home.test", [ME], "Re: books", day(40), body="See you at the next one.")
    s.commit()


def involvement(s, person: str) -> tuple[str, str]:
    """The natural keys of `person`'s INVOLVED_IN edge to the Gloomhaven Night project."""
    return s.execute(text("""
        SELECT 'Person:' || a.key, 'Project:' || b.key FROM edges e JOIN nodes a ON a.id = e.src
        JOIN nodes b ON b.id = e.dst WHERE e.type = 'INVOLVED_IN' AND a.key = :p AND b.name = 'Gloomhaven Night'
    """), {"p": person}).one()


def test_a_reader_sees_the_previous_graph_until_the_rebuild_publishes(committed_db, monkeypatch):
    s = committed_db
    first_build(s)
    with engine.connect() as reader:
        before = derived(reader)
    new_records(s)
    seen = {}
    real = organizations.run

    def mid_rebuild(session):  # a stage late in the run, after resolve and entities rewrote their rows
        with engine.connect() as reader:
            seen.update(derived(reader))
        return real(session)

    monkeypatch.setattr(organizations, "run", mid_rebuild)
    pipeline.run(s)
    assert seen == before
    with engine.connect() as reader:
        after = derived(reader)
    assert after["version"] == before["version"] + 1
    assert after["projects"] == ["Book Club", "Gloomhaven Night"] and after["identities"] > before["identities"]


def _model(system: str, user: str, schema: dict, **_options):
    """Keeps every link the verifier asks about and every name entities screens; the smallest answer otherwise."""
    props = schema.get("properties", {})
    if "edges" in props:
        return {"edges": [{"edge": i, "verdict": "keep", "confidence": 0.9, "reason": "seen in the records"}
                          for i in range(user.count("### Edge "))]}, {}
    if "keep" in props and props["keep"].get("type") == "array":
        return {"keep": [line[2:] for line in user.splitlines() if line.startswith("- ")]}, {}
    return {k: {"array": [], "boolean": False, "number": 0, "integer": 0}.get(v.get("type"), "")
            for k, v in props.items()}, {}


def test_a_failed_rebuild_publishes_nothing_and_keeps_what_the_model_was_paid_for(committed_db, monkeypatch):
    s = committed_db
    first_build(s)
    with engine.connect() as reader:
        before = derived(reader)
    new_records(s)
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail,gcal")
    monkeypatch.setattr(llm, "structured", _model)

    def fail(_s):
        raise RuntimeError("the last step failed")

    monkeypatch.setattr(overrides, "apply", fail)
    with pytest.raises(RuntimeError, match="the last step failed"):
        pipeline.run(s)
    with engine.connect() as reader:
        assert derived(reader) == before
        verdicts = reader.execute(text("SELECT count(*) FROM edge_verdicts WHERE source = 'model'")).scalar()
        screened = reader.execute(text("SELECT count(*) FROM llm_cache WHERE cache_key LIKE 'entities-screen:%'"))
        assert verdicts > 0 and screened.scalar() > 0


def test_each_publish_bumps_the_graph_version_once(db):
    first_build(db)
    version = dirty.version(db)
    pipeline.run(db)
    assert dirty.version(db) == version + 1
    pipeline.run(db, ["resolve"])
    assert dirty.version(db) == version + 2


def test_the_last_step_applies_every_correction_committed_before_it(committed_db, monkeypatch):
    s = committed_db
    first_build(s)
    kai, project = involvement(s, "kai@y.test")
    real = verify.run

    def correct_after_verify(session, **options):
        result = real(session, **options)
        with engine.begin() as other:  # corrections committed after the verifier applied the verdicts it saw
            verify.record_correction(other, kai, project, "INVOLVED_IN", "entities", "drop", "not a player")
            other.execute(text("INSERT INTO overrides (op, node_keys, value) VALUES ('rename', :k, 'Game Night')"),
                          {"k": [project]})
        return result

    monkeypatch.setattr(verify, "run", correct_after_verify)
    pipeline.run(s)
    assert s.execute(text("SELECT name FROM nodes WHERE key = :k"), {"k": project.split(":", 1)[1]}).scalar() \
        == "Game Night"
    live = s.execute(text("SELECT count(*) FROM edges e JOIN nodes a ON a.id = e.src "
                          "WHERE e.type = 'INVOLVED_IN' AND a.key = 'kai@y.test'")).scalar()
    assert live == 0


def test_a_correction_waits_for_a_publishing_rebuild(committed_db, monkeypatch):
    from igneous.main import app

    s = committed_db
    first_build(s)
    project = s.execute(text("SELECT id FROM nodes WHERE type = 'Project' AND name = 'Gloomhaven Night'")).scalar()
    monkeypatch.setattr(pipeline, "CORRECTION_WAIT_MS", 200)
    client = TestClient(app)
    with engine.connect() as rebuild:
        rebuild.execute(text("SELECT pg_advisory_xact_lock(hashtext('igneous:publish'))"))  # publishing now
        busy = client.post(f"/graph/entity/{project}/rename", json={"name": "Game Night"})
        assert busy.status_code == 503 and "being rebuilt" in busy.json()["detail"]
        rebuild.rollback()  # published
    assert client.post(f"/graph/entity/{project}/rename", json={"name": "Game Night"}).status_code == 200
    assert s.execute(text("SELECT name FROM nodes WHERE id = :p"), {"p": project}).scalar() == "Game Night"


def test_undoing_a_merge_publishes_with_the_undo_or_changes_nothing(committed_db, monkeypatch):
    """The folded project comes back through an entities run of its own, which waits for a publishing rebuild only
    as long as a correction may; meanwhile the merge stays as it was."""
    from igneous.main import app

    s = committed_db
    first_build(s)
    new_records(s)
    pipeline.run(s)
    project = {n: i for i, n in s.execute(text("SELECT id, name FROM nodes WHERE type = 'Project'"))}
    client = TestClient(app)
    merge = client.post(f"/graph/entity/{project['Gloomhaven Night']}/merge", json={"other": project["Book Club"]})
    undo = f"/overrides/{merge.json()['override_id']}"
    names = lambda: sorted(s.execute(text("SELECT name FROM nodes WHERE type = 'Project'")).scalars())  # noqa: E731
    assert names() == ["Gloomhaven Night"]
    monkeypatch.setattr(pipeline, "CORRECTION_WAIT_MS", 200)
    with engine.connect() as rebuild:
        rebuild.execute(text("SELECT pg_advisory_xact_lock(hashtext('igneous:publish'))"))  # publishing now
        busy = client.delete(undo)
        assert busy.status_code == 503 and "being rebuilt" in busy.json()["detail"]
        rebuild.rollback()
    assert names() == ["Gloomhaven Night"]
    assert s.execute(text("SELECT count(*) FROM overrides WHERE op = 'merge'")).scalar() == 1
    assert client.delete(undo).status_code == 200
    assert names() == ["Book Club", "Gloomhaven Night"]
