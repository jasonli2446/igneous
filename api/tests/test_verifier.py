"""The verifier judges each claim as it is made, on its evidence (#141, #172): an introduction names its introducer, a
group chat or a class is judged with its own records, only the verdicts the model gave are stored, a changed prompt
judges again, and a link its record settles (a chat member, an attendee) needs no model. An unsure link is asked once
more of the strong model, and becomes a suggestion whose answer is a label."""
import json
import re
from datetime import UTC, datetime, timedelta

import life_threads
import pytest
from fastapi.testclient import TestClient
from life_threads import at, trips  # noqa: F401
from pipeline_data import local_models  # noqa: F401
from sqlalchemy import text

from igneous import llm, pipeline
from igneous.graph import backbone, verify
from igneous.main import app

T0 = datetime(2025, 9, 1, tzinfo=UTC)


class Judge:
    """A model for the verifier: every edge it is asked about gets `answer(index, model)` ((verdict, confidence)),
    or the answers `numbers(n)` picks; every call is recorded as (system, prompt, model)."""

    def __init__(self, answer=lambda i, model: ("keep", 0.9), numbers=None):
        self.answer, self.numbers, self.calls = answer, numbers or (lambda n: range(n)), []

    def __call__(self, system, user, schema, model=None, **_options):
        self.calls.append((system, user, model))
        n = len(re.findall(r"^### Edge \d+$", user, re.M))
        return {"edges": [{"edge": i, "verdict": self.answer(i, model)[0], "confidence": self.answer(i, model)[1],
                           "reason": "as the records show"} for i in self.numbers(n)]}, {}


def item(s, source: str, ref: str, title: str, body: str, when: datetime) -> int:
    return s.execute(text("""INSERT INTO source_items (source, external_id, content_hash, title, body_text, occurred_at)
                             VALUES (:s, :e, :e, :t, :b, :w) RETURNING id"""),
                     {"s": source, "e": ref, "t": title, "b": body, "w": when}).scalar_one()


def node(s, type_: str, key: str, name: str, props: dict | None = None, source_item: int | None = None) -> int:
    return s.execute(text("""INSERT INTO nodes (type, key, name, props, source_item_id)
                             VALUES (:t, :k, :n, CAST(:p AS jsonb), :i) RETURNING id"""),
                     {"t": type_, "k": key, "n": name, "p": json.dumps(props or {}), "i": source_item}).scalar_one()


def edge(s, src: int, dst: int, type_: str, extractor: str = "spine", props: dict | None = None,
         source_item: int | None = None, when: datetime | None = None) -> int:
    return s.execute(text("""INSERT INTO edges (src, dst, type, props, extractor, extractor_version, source_item_id,
                                                valid_from)
                             VALUES (:a, :b, :t, CAST(:p AS jsonb), :x, '1', :i, :w) RETURNING id"""),
                     {"a": src, "b": dst, "t": type_, "p": json.dumps(props or {}), "x": extractor, "i": source_item,
                      "w": when}).scalar_one()


def mail(s, ref: str, sender: int, to: list[int], subject: str, body: str, when: datetime) -> tuple[int, int]:
    """A gmail record and its Message node, sent by `sender` to `to`, whose headers name each of them as the spine
    records it (spine_node_support)."""
    sid = item(s, "gmail", ref, subject, body, when)
    m = node(s, "Message", f"gmail:{ref}", subject, source_item=sid)
    edge(s, sender, m, "SENT", source_item=sid, when=when)
    for r in to:
        edge(s, m, r, "RECEIVED", source_item=sid, when=when)
    s.execute(text("""INSERT INTO spine_node_support (node_id, source_item_id, name, rank)
                      SELECT id, :sid, name, 3 FROM nodes WHERE id = ANY(:ids) ON CONFLICT DO NOTHING"""),
              {"sid": sid, "ids": [sender, *to]})
    return sid, m


def verdicts(s) -> dict[tuple[str, str], tuple[str, str, str]]:
    return {(a, b): (v, src, ev.split(":")[0]) for a, b, v, src, ev in s.execute(text(
        "SELECT src_key, dst_key, verdict, source, evidence FROM edge_verdicts ORDER BY 1, 2"))}


@pytest.fixture
def intros(db, monkeypatch):
    """Ida writes to Bo and Cy together (then to Bo and Di): two introductions she made, in opted-in mail."""
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    me = node(db, "Person", "me", "Mo Self", {"self": True})
    ida, bo, cy, di = (node(db, "Person", f"{n.lower()}@x.test", f"{n} {last}")
                       for n, last in (("Ida", "Kim"), ("Bo", "Berg"), ("Cy", "Cole"), ("Di", "Diaz")))
    for ref, other in (("intro-cy", cy), ("intro-di", di)):
        sid, _m = mail(db, ref, ida, [bo, other, me], "You two should meet", "Bo, meet my friend. You both climb.",
                       T0)
        edge(db, bo, other, "INTRODUCED", "relationship-history", {"introducer": ida, "derived": True},
             source_item=sid, when=T0)
    return {"ida": ida, "bo": bo, "cy": cy, "di": di}


def test_an_introduction_is_judged_as_one_naming_its_introducer(db, intros, monkeypatch):
    judge = Judge()
    monkeypatch.setattr(llm, "structured", judge)
    verify.run(db, log=None)
    [(system, prompt, _model)] = judge.calls
    assert "A and B were introduced to each other by the introducer" in system
    assert "A = Person 'Bo Berg'; B = Person 'Cy Cole'; introducer = 'Ida Kim'" in prompt


def test_a_verdict_is_judged_again_when_the_prompt_changes_and_never_otherwise(db, intros, monkeypatch):
    judge = Judge()
    monkeypatch.setattr(llm, "structured", judge)
    verify.run(db, log=None)
    verify.run(db, log=None)
    assert len(judge.calls) == 1
    claim = ("INTRODUCED", "relationship-history")
    monkeypatch.setitem(verify.CLAIMS, claim, verify.CLAIMS[claim] + " (they had never written to each other)")
    verify.run(db, log=None)
    assert len(judge.calls) == 2


def test_only_the_verdicts_the_model_gave_are_stored_and_an_answer_that_misnumbers_its_edges_is_discarded(
        db, intros, monkeypatch):
    def asked(call) -> list[str]:
        return re.findall(r"B = Person '(\w+)", call[1])

    judge = Judge(numbers=lambda n: [0])  # answers the first edge of the batch only
    monkeypatch.setattr(llm, "structured", judge)
    verify.run(db, log=None)
    assert asked(judge.calls[0]) == ["Cy", "Di"] and list(verdicts(db)) == [("Person:bo@x.test", "Person:cy@x.test")]
    judge.numbers = lambda n: [5]  # an edge the batch doesn't have
    verify.run(db, log=None)
    assert asked(judge.calls[1]) == ["Di"] and len(verdicts(db)) == 1
    judge.numbers = lambda n: [0]
    verify.run(db, log=None)
    assert asked(judge.calls[2]) == ["Di"] and len(verdicts(db)) == 2


def test_an_unsure_link_is_asked_once_of_the_strong_model_and_your_answer_to_it_is_a_label(db, intros, monkeypatch):
    judge = Judge(answer=lambda i, model: ("unsure", 0.5))
    monkeypatch.setattr(llm, "structured", judge)
    verify.run(db, log=None)
    assert [m is None for _s, _p, m in judge.calls] == [False, True]  # the cheap model, then the strong one
    verify.run(db, log=None)
    assert len(judge.calls) == 2  # once per evidence change
    client = TestClient(app)
    suggested = client.get("/graph/suggestions").json()["suggestions"]
    assert {(s["src_name"], s["dst_name"]) for s in suggested} == {("Bo Berg", "Cy Cole"), ("Bo Berg", "Di Diaz")}
    answer = client.post("/graph/edge/confirm", json={"edge": suggested[0]["edge"]}).json()
    assert answer["applied"] == "now"
    assert len(client.get("/graph/suggestions").json()["suggestions"]) == 1
    assert ("keep", "user", "user") in verdicts(db).values()
    # New evidence for the other link: asked again, cheap model first.
    other = suggested[1]["edge"]
    sid = item(db, "gmail", "intro-di-2", "Climbing Saturday?", "Bo and Di, still on for Saturday?", T0)
    db.execute(text("INSERT INTO edge_evidence (edge_id, source_item_id, kind) VALUES (:e, :s, 'follow-up')"),
               {"e": other, "s": sid})
    verify.run(db, log=None)
    assert [m is None for _s, _p, m in judge.calls[2:]] == [False, True]


def test_a_class_is_judged_against_its_school_with_its_own_records(db, monkeypatch):
    """A class you took while at a school, run without the school's email (the professor writes from a personal
    address): the link to the school carries the class's records from your dates there, and the verifier reads them."""
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    me = node(db, "Person", "me", "Mo Self", {"self": True})
    school = node(db, "Organization", "state.edu", "State University", {"named_by": "profile"})
    db.execute(text("""INSERT INTO edges (src, dst, type, props, extractor, valid_from, valid_to)
                       VALUES (:m, :o, 'WORKED_AT', '{"school": true}', 'profile', :a, :b)"""),
               {"m": me, "o": school, "a": datetime(2023, 9, 1, tzinfo=UTC), "b": datetime(2027, 5, 1, tzinfo=UTC)})
    prof = node(db, "Person", "kim.prof@mail.test", "Ida Kim")
    cls = node(db, "Project", "thread:cs401", "CS 401", {"derived": "threads", "kind": "class", "records": 3,
                                                         "start": T0.isoformat(),
                                                         "end": (T0 + timedelta(days=90)).isoformat()})
    for i in range(3):
        sid, m = mail(db, f"hw{i}", prof, [me], f"CS 401 homework {i + 1}",
                      f"Homework {i + 1} for CS 401 at State University is posted.", T0 + timedelta(days=30 * i))
        edge(db, m, cls, "PART_OF", "threads", source_item=sid)
    backbone.run(db, log=None)
    link = db.execute(text("""SELECT e.id, count(ev.id) FROM edges e JOIN nodes o ON o.id = e.dst
                              LEFT JOIN edge_evidence ev ON ev.edge_id = e.id
                              WHERE e.src = :c AND e.extractor = 'backbone' AND o.name = 'State University'
                              GROUP BY e.id"""), {"c": cls}).one()
    assert link[1] == 3
    judge = Judge()
    monkeypatch.setattr(llm, "structured", judge)
    verify.run(db, log=None)
    [prompt] = [p for system, p, _m in judge.calls if "was done at or for this organization or school" in system]
    assert "A = Project 'CS 401'; B = Project 'State University'" in prompt and "CS 401 homework 3" in prompt
    assert verdicts(db)[("Project:thread:cs401", "Project:backbone:state.edu")][:2] == ("keep", "model")


@pytest.mark.usefixtures("trips")
def test_a_group_chat_about_a_thread_is_judged_as_one_with_its_own_records(db, monkeypatch):
    from igneous.graph import threads

    monkeypatch.setattr(threads, "MAX_DF", 0.05)  # three more records name Portland in this small world
    for d in range(3):
        life_threads._chat_day(db, "chat-plan", "Portland planning", ["+15550000001", "+15550000002"], at(50 + d),
                               f"Portland: who books the car? ({d})")
    db.commit()
    seen = []

    def model(system, user, schema, **options):
        seen.append((system, user))
        return life_threads.model(system, user, schema, **options)

    monkeypatch.setattr(llm, "structured", model)
    pipeline.run(db)
    asked = [(system, block) for system, user in seen for block in user.split("### Edge ")
             if "A = Thread 'Portland planning'" in block]
    assert len(asked) == 1
    system, block = asked[0]
    assert "this group chat or calendar series (A) is about this thread" in system
    assert block.count("[imessage, ") == 3 and "who books the car" in block


@pytest.mark.usefixtures("trips")
def test_a_record_that_only_shows_a_project_going_on_is_judged_as_its_evidence_after_every_other_link(db, monkeypatch):
    """A search during the trip is the trip's evidence, and one naming the friends' group chat while it is active is
    the chat's Project's: the verifier judges each under its own claim, after the links people see (who went, what is
    part of what), so a run that runs out of budget has spent it on those first."""
    from pipeline_data import put

    put(db, "takeout_activity", "activity/search/portland", "Search activity", {"product": "Search"}, at(64),
        "[21:02] Searched for portland food carts")
    put(db, "takeout_activity", "activity/search/crew", "Search activity", {"product": "Search"}, at(70),
        "[22:40] Searched for road crew playlist")
    db.commit()
    monkeypatch.setattr(llm.settings, "llm_sources", life_threads.SOURCES + ",takeout")
    systems = []

    def model(system, user, schema, **options):
        if "edges" in schema["properties"]:
            systems.append(system)
        return life_threads.model(system, user, schema, **options)

    monkeypatch.setattr(llm, "structured", model)
    pipeline.run(db)
    claims = ["(A) shows that this thread of the person's life (B) happened",
              "this record (A) shows this project (B) going on"]
    asked = [i for i, system in enumerate(systems) if any(c in system for c in claims)]
    assert [any(c in systems[i] for i in asked) for c in claims] == [True, True]
    assert asked == list(range(len(systems) - len(asked), len(systems)))  # the last calls of the run
    assert verify.claim_of({"type": "RELATED_TO", "extractor": "threads", "src_type": "Person"}) == \
        ("RELATED_TO", "threads")  # a person who talked about it is no evidence


@pytest.mark.usefixtures("trips")
def test_a_link_its_record_settles_needs_no_model(db, monkeypatch):
    """Nothing opted in: the people in a trip's group chat took part in it, as the chat itself lists them."""
    monkeypatch.setattr(llm.settings, "llm_sources", "")
    pipeline.run(db)
    rows = db.execute(text("""
        SELECT a.key, e.props->>'verdict', e.props->>'verified_by' FROM edges e JOIN nodes a ON a.id = e.src
        JOIN nodes p ON p.id = e.dst WHERE e.type = 'INVOLVED_IN' AND p.name LIKE 'Portland%' ORDER BY 1""")).all()
    assert ("+15550000001", "keep", "evidence") in rows and ("+15550000002", "keep", "evidence") in rows


def test_identities_merged_by_score_are_split_only_by_a_confident_drop(db):
    a, b = (node(db, "Person", k, "Sam Lee") for k in ("sam@a.test", "sam@b.test"))
    edge(db, a, b, "SAME_AS", "er")
    from igneous.graph.resolve import refresh_canonical
    refresh_canonical(db)

    def drop(confidence: float) -> None:
        db.execute(text("""INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, confidence,
                                                      source)
                           VALUES ('Person:sam@a.test', 'Person:sam@b.test', 'SAME_AS', 'er', 'd', 'drop', :c, 'model')
                           ON CONFLICT (src_key, dst_key, type, extractor) DO UPDATE SET confidence = :c"""),
                   {"c": confidence})
        verify.apply_verdicts(db)

    drop(0.7)
    assert db.execute(text("SELECT count(*) FROM canonical")).scalar() == 2
    drop(0.9)
    assert db.execute(text("SELECT count(*) FROM canonical")).scalar() == 0
    assert db.execute(text("SELECT count(*) FROM edges_dropped WHERE type = 'SAME_AS'")).scalar() == 1
