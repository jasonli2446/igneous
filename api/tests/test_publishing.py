"""The derived stages publish in one transaction: a reader keeps the previous graph until the new one is committed,
a failed rebuild publishes nothing but keeps what the model was paid for, and a correction made while it runs is
recorded at once and lands in what it publishes, without ever waiting long or being refused."""
import threading

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


class Rebuild:
    """A rebuild in a thread of its own, on its own connection, paused at `where` (a stage function or the final step)
    until released."""

    def __init__(self, monkeypatch, module, name: str):
        self.reached, self.release, self.error = threading.Event(), threading.Event(), None
        real = getattr(module, name)
        rebuild = self

        def paused(*args, **kwargs):
            if threading.current_thread() is rebuild.thread:
                rebuild.reached.set()
                assert rebuild.release.wait(30)
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, paused)
        self.thread = threading.Thread(target=self._run)

    def _run(self):
        from igneous.db import SessionLocal

        try:
            with SessionLocal() as s:
                pipeline.run(s)
        except Exception as e:  # reported by finish()
            self.error = e

    def __enter__(self):
        self.thread.start()
        assert self.reached.wait(30)
        return self

    def finish(self):
        self.release.set()
        self.thread.join(30)
        assert self.error is None and not self.thread.is_alive()

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(30)


def published(name_of: int) -> str:
    with engine.connect() as reader:
        return reader.execute(text("SELECT name FROM nodes WHERE id = :p"), {"p": name_of}).scalar()


def test_a_correction_made_while_a_rebuild_derives_is_saved_at_once_and_lands_in_what_it_publishes(
        committed_db, monkeypatch):
    """Two connections: a rebuild deriving (paused in a stage), and a rename made meanwhile. The rename's intent is
    committed at once, it changes nothing the rebuild is deriving, the answer says it applies when the rebuild
    publishes, and it does."""
    from igneous.main import app

    s = committed_db
    first_build(s)
    project = s.execute(text("SELECT id FROM nodes WHERE type = 'Project' AND name = 'Gloomhaven Night'")).scalar()
    client = TestClient(app)
    with Rebuild(monkeypatch, organizations, "run") as rebuild:
        answer = client.post(f"/graph/entity/{project}/rename", json={"name": "Game Night"})
        assert answer.status_code == 200 and answer.json()["applied"] == "on_publish"
        with engine.connect() as reader:
            assert reader.execute(text("SELECT op, value FROM overrides")).one() == ("rename", "Game Night")
        assert published(project) == "Gloomhaven Night"
        rebuild.finish()
    assert published(project) == "Game Night"


def test_a_correction_made_during_the_final_step_waits_for_it_then_applies_now(committed_db, monkeypatch):
    """The final step holds the publish lock until its commit: a correction recorded meanwhile waits (it would be
    missed otherwise), then applies in place to the graph the rebuild published."""
    from igneous.graph import corrections
    from igneous.main import app

    s = committed_db
    first_build(s)
    project = s.execute(text("SELECT id FROM nodes WHERE type = 'Project' AND name = 'Gloomhaven Night'")).scalar()
    client, answers = TestClient(app), []
    with Rebuild(monkeypatch, corrections, "apply_all") as rebuild:
        correction = threading.Thread(target=lambda: answers.append(
            client.post(f"/graph/entity/{project}/rename", json={"name": "Game Night"})))
        correction.start()
        correction.join(1.0)
        assert correction.is_alive()  # waiting for the final step
        with engine.connect() as reader:
            assert reader.execute(text("SELECT count(*) FROM overrides")).scalar() == 0
        rebuild.finish()
        correction.join(30)
    assert answers[0].status_code == 200 and answers[0].json()["applied"] == "now"
    assert published(project) == "Game Night"


def test_undoing_a_merge_during_a_rebuild_brings_the_folded_project_back_when_it_publishes(committed_db, monkeypatch):
    """Undo is an intent too: recorded at once, completed by the final step (the folded project comes back with
    what moved from it). Merging and undoing with no rebuild running apply now."""
    from igneous.main import app

    s = committed_db
    first_build(s)
    new_records(s)
    pipeline.run(s)
    project = {n: i for i, n in s.execute(text("SELECT id, name FROM nodes WHERE type = 'Project'"))}
    client = TestClient(app)
    merge = client.post(f"/graph/entity/{project['Gloomhaven Night']}/merge", json={"other": project["Book Club"]})
    assert merge.json()["applied"] == "now"
    names = lambda: sorted(s.execute(text("SELECT name FROM nodes WHERE type = 'Project'")).scalars())  # noqa: E731
    assert names() == ["Gloomhaven Night"]
    with Rebuild(monkeypatch, organizations, "run") as rebuild:
        undo = client.delete(f"/overrides/{merge.json()['override_id']}")
        assert undo.status_code == 200 and undo.json()["applied"] == "on_publish"
        assert names() == ["Gloomhaven Night"]
        rebuild.finish()
    assert names() == ["Book Club", "Gloomhaven Night"]
    assert s.execute(text("SELECT count(*) FROM overrides")).scalar() == 0
    assert client.delete(f"/overrides/{merge.json()['override_id']}").status_code == 404


def test_a_correction_gives_up_a_lock_wait_rather_than_wait_out_another_writer(committed_db):
    """A correction applying in place never waits as long as the deadlock detector would: when another transaction
    holds a row it must change, it gives up (its intent stays recorded) and says when it applies."""
    from igneous.main import app

    s = committed_db
    first_build(s)
    project = s.execute(text("SELECT id FROM nodes WHERE type = 'Project' AND name = 'Gloomhaven Night'")).scalar()
    with engine.connect() as other:
        other.execute(text("SELECT 1 FROM nodes WHERE id = :p FOR UPDATE"), {"p": project})
        answer = TestClient(app).post(f"/graph/entity/{project}/rename", json={"name": "Game Night"}).json()
        assert answer["applied"] == "on_publish"
        other.rollback()
    assert published(project) == "Gloomhaven Night"
    pipeline.run(s)
    assert published(project) == "Game Night"
