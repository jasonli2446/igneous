"""One stage runner for `igneous pipeline` and Rebuild: the stage list, its dependency order, progress, `--stage`."""
import json

import click
import pytest
from pipeline_data import card, day, event, linkedin_profile, local_models, mail, rows  # noqa: F401
from sqlalchemy import text
from typer.testing import CliRunner

from igneous import cli, llm, pipeline
from igneous.graph import overrides

pytestmark = pytest.mark.usefixtures("local_models")  # embeddings without a model download, no hosted calls
ME = "me@x.test"


def owner_ids(s) -> set[int]:
    return set(s.execute(text("""
        SELECT n.id FROM nodes n WHERE n.type = 'Person' AND (n.key = 'me' OR n.id IN (
            SELECT node_id FROM canonical WHERE canonical_id = (SELECT id FROM nodes WHERE type = 'Person' AND key = 'me')))
    """)).scalars())


def test_the_stage_list_is_the_dependency_order():
    assert pipeline.NAMES == ("redact", "spine", "chunk", "embed", "extract", "self", "resolve", "thread_flags",
                              "introductions", "topics", "entities", "profile", "threads", "backbone", "organizations",
                              "verify", "corrections")
    sources = [st.name for st in pipeline.STAGES if not st.derived]
    assert sources == ["redact", "spine", "chunk", "embed", "extract"]  # everything after them publishes together
    assert pipeline.GATED == {"resolve"}
    assert all(st.label and st.label[0].isupper() for st in pipeline.STAGES)


def test_a_plan_runs_named_stages_in_pipeline_order_and_always_ends_with_corrections():
    names = lambda stages: [st.name for st in stages]  # noqa: E731
    assert names(pipeline.plan()) == [n for n in pipeline.NAMES if n != "extract"]
    assert names(pipeline.plan(extract=5)) == list(pipeline.NAMES)
    assert names(pipeline.plan(["resolve", "self"])) == ["self", "resolve", "corrections"]
    assert names(pipeline.plan(["corrections"])) == ["corrections"]
    # A stage that reads source records never reads one stored under older credential rules.
    assert names(pipeline.plan(["extract"], extract=5)) == ["redact", "extract", "corrections"]
    assert names(pipeline.plan(["chunk", "self"])) == ["redact", "chunk", "self", "corrections"]
    with pytest.raises(ValueError, match="unknown stage 'spines'; the stages are redact, spine, chunk"):
        pipeline.plan(["spines"])
    with pytest.raises(ValueError, match="--extract N"):
        pipeline.plan(["extract"])


def test_the_cli_validates_stages_against_the_list_and_generates_its_help(db):
    import typer

    command = typer.main.get_command(cli.app).commands["pipeline"]
    stage = next(p for p in command.params if p.name == "stage")
    assert tuple(stage.type.choices) == pipeline.NAMES
    runner = CliRunner()
    # Rich styles help and errors whenever it sees a terminal, and Typer forces one under CI: compare the text.
    assert "--stage" in click.unstyle(runner.invoke(cli.app, ["pipeline", "--help"]).output)
    unknown = runner.invoke(cli.app, ["pipeline", "--stage", "spines"])
    assert unknown.exit_code == 2 and "'spines' is not one of" in click.unstyle(unknown.output)
    no_count = runner.invoke(cli.app, ["pipeline", "--stage", "extract"])
    assert no_count.exit_code == 2 and "--extract N" in click.unstyle(no_count.output)


def test_progress_names_each_stage_with_its_label_step_and_counts(db):
    mail(db, "a", "kai@y.test", [ME], "Plans", day(0), body="Let's meet for lunch on Friday at noon, as usual.")
    db.commit()
    states = []
    results = pipeline.run(db, progress=lambda state, force=False: states.append((dict(state), force)))
    started = [st for st, force in states if force and st["stage"]]
    expected = [st for st in pipeline.STAGES if st.name != "extract"]
    assert [st["stage"] for st in started] == [st.name for st in expected]
    assert [st["label"] for st in started] == [st.label for st in expected]
    assert [st["step"] for st in started] == list(range(1, len(expected) + 1))
    assert {st["steps"] for st in started} == {len(expected)}
    counted = [st for st, force in states if not force and st["stage"] == "embed"]
    assert counted and counted[-1]["count"] == counted[-1]["total"] == 1  # the one chunk, embedded
    assert set(results) == {st.name for st in expected}
    assert all(set(r) == {"result", "seconds"} for r in results.values())
    assert states[-1][0]["stage"] is None and states[-1][1]


def test_the_cli_prints_a_line_per_stage(db):
    mail(db, "a", "kai@y.test", [ME], "Plans", day(0))
    db.commit()
    out = CliRunner().invoke(cli.app, ["pipeline", "--stage", "spine", "--stage", "self"])
    assert out.exit_code == 0, out.output
    assert [line.split()[0] for line in out.output.splitlines()] == ["redact", "spine", "self", "corrections"]


def test_a_first_build_has_the_backbone_and_never_makes_you_a_member_of_your_projects(db):
    """Self and resolve run before everything that reads identities, profile after self, backbone after both."""
    names = {ME: "Mo Self", "kai@y.test": "Kai Yu", "lu@z.test": "Lu Zed"}
    linkedin_profile(db, "Mo Self", [{"org": "Acme Robotics", "title": "Engineer", "start": "2024-01"}])
    for i in range(3):
        event(db, f"acct/cal/gn_{i}", "Gloomhaven Night", day(7 * i), ["kai@y.test", "lu@z.test"], ME, "gn",
              names)
    for i in range(2):
        mail(db, f"gn{i}", ME, ["kai@y.test"], f"Gloomhaven Night {i}", day(3 + i), sent=True, names=names,
             body="Are we still on for Gloomhaven Night this week? I'll bring the expansion.")
    db.commit()
    pipeline.run(db)
    project = db.execute(text("SELECT id FROM nodes WHERE type = 'Project' AND name = 'Gloomhaven Night'")).scalar_one()
    members = rows(db, "SELECT n.key FROM edges e JOIN nodes n ON n.id = e.src "
                       "WHERE e.dst = :p AND e.type = 'INVOLVED_IN'", p=project)
    assert members == [("kai@y.test",)]  # on the series and in your mail about it
    involved = db.execute(text("SELECT count(*) FROM edges WHERE type = 'INVOLVED_IN' AND src = ANY(:me)"),
                          {"me": sorted(owner_ids(db))}).scalar()
    assert involved == 0
    org = db.execute(text("""SELECT o.id FROM edges e JOIN nodes o ON o.id = e.dst JOIN nodes me ON me.id = e.src
                             WHERE e.type = 'WORKED_AT' AND me.key = 'me' AND o.name = 'Acme Robotics'""")).scalar_one()
    backbone = db.execute(text("SELECT key FROM nodes WHERE type = 'Project' AND props->>'org' = :o"),
                          {"o": str(org)}).scalar_one()
    assert backbone == "backbone:org:acme-robotics"


def test_thread_flags_count_people_merged_in_the_same_build(db):
    """Alice's two addresses are one person (her contact card), so the thread is a two-person exchange."""
    card(db, "acct/people/1", "Alice Ames", emails=["alice@a.test", "alice@b.test"])
    mail(db, "p1", "bob@c.test", ["alice@a.test"], "Plans", day(0), thread="plans")
    mail(db, "p2", "alice@b.test", ["bob@c.test"], "Re: Plans", day(1), thread="plans")
    db.commit()
    pipeline.run(db)
    flags = db.execute(text("SELECT props FROM nodes WHERE type = 'Thread' AND key = 'gmail:plans'")).scalar_one()
    assert flags["conversational"] is True and flags["hidden"] is True


def test_introductions_use_identities_merged_in_the_same_build(db):
    """Cara writes back from her other address (one person, by her contact card)."""
    card(db, "acct/people/1", "Cara Cole", emails=["cara@z.test", "cara@w.test"])
    mail(db, "i1", "ann@x.test", ["ben@y.test", "cara@z.test"], "Meet Cara", day(0))
    mail(db, "i2", "ben@y.test", ["cara@w.test"], "Coffee?", day(10))
    db.commit()
    pipeline.run(db)
    (a, b, introducer), = rows(db, """
        SELECT a.key, b.key, i.key FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst
        JOIN nodes i ON i.id = (e.props->>'introducer')::bigint WHERE e.type = 'INTRODUCED'""")
    cara = db.execute(text("SELECT key FROM nodes WHERE id = (SELECT canonical_id FROM canonical c JOIN nodes n "
                           "ON n.id = c.node_id WHERE n.key = 'cara@z.test')")).scalar_one()
    assert {a, b} == {"ben@y.test", cara} and introducer == "ann@x.test"


def test_introductions_never_involve_a_service_the_self_stage_found(db):
    mail(db, "i1", "ann@x.test", ["ben@y.test", "noreply@alerts.test"], "Welcome", day(0))
    mail(db, "i2", "ben@y.test", ["noreply@alerts.test"], "Re: Welcome", day(10))
    db.commit()
    pipeline.run(db)
    assert db.execute(text("SELECT count(*) FROM edges WHERE type = 'INTRODUCED'")).scalar() == 0
    assert db.execute(text("SELECT props->>'kind' FROM nodes WHERE key = 'noreply@alerts.test'")).scalar() == "service"


def test_where_people_work_attaches_to_identities_merged_in_the_same_build(db):
    """Jordan's home address (on his card, with his company) merges with his busier school address by score."""
    jordan = {"jordan.lee@school.test": "Jordan Lee", "jlee@home.test": "Jordan Lee"}
    for i, address in enumerate(jordan):
        for k in range(3 - 2 * i):
            mail(db, f"j{i}{k}", address, ["ana@q.test", "bo@r.test"], f"Weekend {k}", day(3 * i + k), names=jordan)
    card(db, "acct/people/1", "Jordan Lee", emails=["jlee@home.test"], orgs=["Initech"])
    db.commit()
    pipeline.run(db)
    (person,) = db.execute(text("""SELECT p.key FROM edges e JOIN nodes p ON p.id = e.src
                                   WHERE e.type = 'WORKED_AT' AND e.extractor = 'organizations'""")).scalars()
    assert person == "jordan.lee@school.test"  # the head of his identities, the one with most mail


def test_the_verifier_judges_the_links_written_in_the_same_build(db, monkeypatch):
    """A model that rejects every link: on the first build, the people found for a project are already checked."""
    for i in range(3):
        event(db, f"acct/cal/gn_{i}", "Gloomhaven Night", day(7 * i), ["kai@y.test", "lu@z.test"], ME, "gn")
    for i in range(2):
        mail(db, f"gn{i}", ME, ["kai@y.test"], f"Gloomhaven Night {i}", day(3 + i), sent=True,
             body="Are we still on for Gloomhaven Night this week? I'll bring the expansion.")
    db.commit()

    def model(system, user, schema, **_options):
        props = schema["properties"]
        if "edges" in props:
            return {"edges": [{"edge": i, "verdict": "drop", "confidence": 0.9, "reason": "not shown"}
                              for i in range(user.count("### Edge "))]}, {}
        if "keep" in props:
            return {"keep": [line[2:] for line in user.splitlines() if line.startswith("- ")]}, {}
        return {k: {"array": [], "boolean": False, "number": 0}.get(v.get("type"), "") for k, v in props.items()}, {}

    monkeypatch.setattr(llm, "structured", model)
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail,gcal")
    pipeline.run(db)
    assert db.execute(text("SELECT count(*) FROM edges WHERE type = 'INVOLVED_IN'")).scalar() == 0
    assert db.execute(text("SELECT count(*) FROM edges_dropped WHERE type = 'INVOLVED_IN'")).scalar() == 1


def test_corrections_apply_after_the_last_stage_that_writes_projects(db):
    """Backbone names your org's thread on every run; your rename of it still holds after a rebuild."""
    linkedin_profile(db, "Mo Self", [{"org": "Acme Robotics", "title": "Engineer", "start": "2024-01"}])
    mail(db, "m1", ME, ["kai@y.test"], "Hi", day(0), sent=True)
    db.commit()
    pipeline.run(db)
    overrides.add(db, "rename", ["Project:backbone:org:acme-robotics"], "The Robot Shop")
    db.commit()
    pipeline.run(db)
    assert db.execute(text("SELECT name FROM nodes WHERE key = 'backbone:org:acme-robotics'")).scalar() \
        == "The Robot Shop"


def test_pipeline_with_extract_completes_with_a_stubbed_model(db, monkeypatch):
    """`make demo EXTRACT=N`: read messages with the model, then every stage, and resolve the promise it found."""
    promise = "I will send the budget report by Friday"
    first = (f"Hi Kai, thanks for the update on the Atlas launch. {promise} so we can close the quarter. Let me "
             "know if the vendor list changes before then, and whether the launch date still holds. Best, Mo")
    reply = ("Thanks Mo, the budget report arrived this morning and it looks complete. The Atlas launch is on "
             "track, the vendor list did not change, and the launch date still holds. Talk soon, Kai")
    mail(db, "b1", ME, ["kai@y.test"], "Atlas budget", day(0), sent=True, body=first, thread="atlas")
    mail(db, "b2", "kai@y.test", [ME], "Re: Atlas budget", day(2), body=reply, thread="atlas")
    db.commit()

    def model(system, user, schema, **_options):
        props = schema["properties"]
        if "commitments" in props:  # extraction
            claims = [{"who": "sender", "what": "send the budget report", "by_when": "Friday", "quote": promise}]
            return ({"people": [], "organizations": [], "topics": ["Atlas launch"], "decisions": [],
                     "commitments": claims if promise in user else []}, {"input_tokens": 10, "output_tokens": 5})
        if "resolved" in props:
            return {"resolved": True}, {}
        return {k: {"array": [], "boolean": False, "number": 0, "integer": 0}.get(v.get("type"), "")
                for k, v in props.items()}, {}

    monkeypatch.setattr(llm, "structured", model)
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    out = CliRunner().invoke(cli.app, ["pipeline", "--extract", "5"])
    assert out.exit_code == 0, out.output
    printed = [line.split()[0] for line in out.output.splitlines() if not line.startswith(" ")]
    assert printed == list(pipeline.NAMES)
    assert db.execute(text("SELECT props->>'status' FROM nodes WHERE type = 'Commitment'")).scalar() == "resolved"
    assert db.execute(text("SELECT name FROM nodes WHERE type = 'Topic'")).scalars().all() == ["Atlas launch"]


def test_rebuild_runs_the_same_stages_and_reports_them(db):
    from igneous import sources

    mail(db, "a", "kai@y.test", [ME], "Plans", day(0))
    db.commit()
    started = []
    result = sources.run_pipeline(db, lambda state, force=False: started.append(state["stage"]) if force else None)
    rebuild = [n for n in pipeline.NAMES if n != "extract"]
    assert list(result) == rebuild and started == [*rebuild, None]
    assert json.loads(json.dumps(result, default=str))  # what the job stores as its result
