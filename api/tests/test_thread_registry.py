"""The thread registry keeps a found thread found (#208): a failed or missing judgment never rejects a candidate, a
thread named without the model is judged once its records may be read, the model budget goes only to what is new,
merged threads keep every record, and a thread whose row or org is gone leaves the graph. Seeds and resume terms
match whole names, never part of a word or a generic word."""
import life_threads
import pytest
from life_threads import ME, at, life, trips  # noqa: F401
from pipeline_data import local_models, put  # noqa: F401
from sqlalchemy import text

from igneous import llm, pipeline
from igneous.graph import threads


def project(s, name: str) -> int | None:
    return s.execute(text("SELECT id FROM nodes WHERE type = 'Project' AND name = :n"), {"n": name}).scalar()


def thread_names(s) -> list[str]:
    return sorted(s.execute(text("""SELECT name FROM nodes WHERE type = 'Project'
                                    AND props->>'derived' = 'threads'""")).scalars())


def records(s, nid: int) -> set[int]:
    return set(s.execute(text("""SELECT n.source_item_id FROM edges e JOIN nodes n ON n.id = e.src
                                 WHERE e.dst = :p AND e.type = 'PART_OF' AND n.source_item_id IS NOT NULL"""),
                         {"p": nid}).scalars())


@pytest.mark.usefixtures("trips")
def test_a_failed_judgment_writes_no_row_and_the_candidate_is_judged_on_a_later_run(db, monkeypatch):
    def down(system, user, schema, **options):
        if "defining_terms" in schema["properties"]:
            raise RuntimeError("the model is down")
        return life_threads.model(system, user, schema, **options)

    monkeypatch.setattr(llm, "structured", down)
    pipeline.run(db)
    assert db.execute(text("SELECT count(*) FROM thread_registry")).scalar() == 0
    assert thread_names(db) == []
    monkeypatch.setattr(llm, "structured", life_threads.model)
    pipeline.run(db)
    assert thread_names(db) == ["Lisbon trip", "Portland trip"]


@pytest.mark.usefixtures("trips")
def test_a_thread_named_without_the_model_is_judged_once_its_records_may_be_read_unless_you_named_it(db, monkeypatch):
    from fastapi.testclient import TestClient

    from igneous.main import app

    monkeypatch.setattr(llm.settings, "llm_sources", "")  # nothing opted in: named by its term and month
    pipeline.run(db)
    named = {n.split(" · ")[0]: project(db, n) for n in thread_names(db)}
    assert sorted(named) == ["Lisbon", "Portland"] and all(" · " in n for n in thread_names(db))
    TestClient(app).post(f"/graph/entity/{named['Lisbon']}/rename", json={"name": "Winter getaway"})
    monkeypatch.setattr(llm.settings, "llm_sources", life_threads.SOURCES)
    pipeline.run(db)
    assert thread_names(db) == ["Portland trip", "Winter getaway"]
    assert (project(db, "Portland trip"), project(db, "Winter getaway")) == (named["Portland"], named["Lisbon"])
    assert db.execute(text("SELECT name, judged FROM thread_registry ORDER BY name")).all() == [
        ("Lisbon · Dec 2025", False), ("Portland trip", True)]


@pytest.mark.usefixtures("trips")
def test_the_judgment_budget_goes_only_to_new_candidates(db, monkeypatch):
    """With room for one judgment per run, the first run judges one trip and leaves the other waiting (no row); the
    next run spends nothing on the known trip, and judges the other."""
    monkeypatch.setattr(threads, "JUDGE_BUDGET", 1)
    pipeline.run(db)
    assert thread_names(db) == ["Portland trip"]
    assert db.execute(text("SELECT name FROM thread_registry")).scalars().all() == ["Portland trip"]
    pipeline.run(db)
    assert thread_names(db) == ["Lisbon trip", "Portland trip"]


@pytest.mark.usefixtures("trips")
def test_two_threads_found_to_be_one_keep_every_record_on_later_runs(db):
    """A thread found twice (the trip, and its group chat with the photos shared weeks later, under two keys) is
    merged: the chat's row becomes a facet of the trip and finds its own records by its own terms and window, so the
    trip holds both on every later run, with the other name as one alias."""
    for i in range(3):
        life_threads._mail(db, f"photos{i}", "cy@pals.test", [ME, "fay@pals.test"], f"Wanderers photo album {i}",
                           at(258 + i), "more pictures from our wanderings")
    db.execute(text("""
        INSERT INTO thread_registry (key, name, kind, terms, span_start, span_end, status, origin, judged) VALUES
            ('thread:lisbon', 'Lisbon trip', 'trip', '{Lisbon}', :a, :b, 'active', 'burst', true),
            ('thread:wanderers', 'lisbon-trip', 'trip', '{Wanderers}', :c, :d, 'active', 'burst', true)
    """), {"a": at(185), "b": at(207), "c": at(193), "d": at(260)})
    db.commit()
    lisbon, wanderers = (set(db.execute(text(f"""SELECT id FROM source_items
                                                   WHERE title ILIKE '%{t}%' OR body_text ILIKE '%{t}%'""")).scalars())
                         for t in ("lisbon", "wanderers"))
    pipeline.run(db)
    trip = project(db, "Lisbon trip")
    assert project(db, "lisbon-trip") is None and records(db, trip) == lisbon | wanderers
    for _ in range(2):
        pipeline.run(db)
        assert project(db, "Lisbon trip") == trip and records(db, trip) == lisbon | wanderers
        assert db.execute(text("SELECT props->'aliases' FROM nodes WHERE id = :t"), {"t": trip}).scalar() \
            == ["lisbon-trip"]
    assert db.execute(text("SELECT status, merged_into FROM thread_registry WHERE key = 'thread:wanderers'")
                         ).one() == ("merged", "thread:lisbon")


def rejecting(term: str):
    """The synthetic life's model, except that it judges the candidate sharing `term` not a thread."""
    def model(system, user, schema, **options):
        out, usage = life_threads.model(system, user, schema, **options)
        if "defining_terms" in schema["properties"] and user.startswith(f"Shared terms: {term}"):
            out = {**out, "keep": False}
        return out, usage
    return model


@pytest.mark.usefixtures("trips")
def test_a_thread_judged_not_one_gives_back_what_was_found_to_be_the_same_thing(db, monkeypatch):
    """Two threads named without the model, under one name, are one; once the model may read them it says the bigger
    is no thread, and the other stands on its own again with its own records."""
    for i in range(3):
        life_threads._mail(db, f"photos{i}", "cy@pals.test", [ME, "fay@pals.test"], f"Wanderers photo album {i}",
                           at(258 + i), "more pictures from our wanderings")
    db.execute(text("""
        INSERT INTO thread_registry (key, name, kind, terms, span_start, span_end, status, origin, judged) VALUES
            ('thread:lisbon', 'Lisbon · Dec 2025', 'thread', '{Lisbon}', :a, :b, 'active', 'burst', false),
            ('thread:wanderers', 'Lisbon · Dec 2025', 'thread', '{Wanderers}', :c, :d, 'active', 'burst', false)
    """), {"a": at(185), "b": at(207), "c": at(193), "d": at(260)})
    db.commit()
    monkeypatch.setattr(llm.settings, "llm_sources", "")
    pipeline.run(db)
    assert db.execute(text("SELECT merged_into FROM thread_registry WHERE key = 'thread:wanderers'")).scalar() \
        == "thread:lisbon"
    monkeypatch.setattr(llm.settings, "llm_sources", life_threads.SOURCES)
    monkeypatch.setattr(llm, "structured", rejecting("Lisbon"))
    pipeline.run(db)
    assert db.execute(text("""SELECT key, status, merged_into FROM thread_registry WHERE key IN
                              ('thread:lisbon', 'thread:wanderers') ORDER BY key""")).all() == [
        ("thread:lisbon", "rejected", None), ("thread:wanderers", "active", None)]
    wanderers = db.execute(text("SELECT id FROM nodes WHERE key = 'thread:wanderers'")).scalar_one()
    assert len(records(db, wanderers)) == 9  # its chat's six days and the three photo mails


@pytest.mark.usefixtures("trips")
def test_a_thread_you_merged_another_into_is_kept_whatever_the_judge_says(db, monkeypatch):
    from fastapi.testclient import TestClient

    from igneous.main import app

    monkeypatch.setattr(llm.settings, "llm_sources", "")
    pipeline.run(db)
    named = {n.split(" · ")[0]: project(db, n) for n in thread_names(db)}
    lisbon = records(db, named["Lisbon"])
    TestClient(app).post(f"/graph/entity/{named['Portland']}/merge", json={"other": named["Lisbon"]})
    monkeypatch.setattr(llm.settings, "llm_sources", life_threads.SOURCES)
    monkeypatch.setattr(llm, "structured", rejecting("Portland"))
    pipeline.run(db)
    assert thread_names(db) == ["Portland trip"] and project(db, "Portland trip") == named["Portland"]
    assert lisbon <= records(db, named["Portland"])


@pytest.mark.usefixtures("life")
def test_a_thread_whose_resume_entry_or_org_is_gone_leaves_the_graph(db):
    """Your resume stops listing a project and a school: the project's pinned thread and the school's own thread go
    (the classes stay, no longer under it)."""
    pipeline.run(db)
    atlas = db.execute(text("SELECT key FROM nodes WHERE type = 'Project' AND key LIKE 'profile:%'")).scalars().all()
    school = db.execute(text("SELECT key FROM nodes WHERE type = 'Project' AND key LIKE 'backbone:%' "
                             "AND name = 'State University'")).scalars().all()
    assert len(atlas) == 1 and len(school) == 1
    profile = {"is_about_owner": True, "person_name": "Mo Self", "projects": [], "education": [],
               "experience": [{"org": "Acme Robotics", "title": "Software Intern", "start": "2025-06",
                               "end": "2025-08", "domain_hint": "", "parent": "", "project": ""}]}
    put(db, "linkedin", "profile", "LinkedIn profile: Mo Self", {"kind": "profile", "person_name": "Mo Self",
                                                                 "profile": profile}, None, "LinkedIn profile of Mo Self")
    life_threads.arrived_later(db)
    pipeline.run(db)
    gone = db.execute(text("SELECT key FROM nodes WHERE key = ANY(:k)"), {"k": atlas + school}).scalars().all()
    assert gone == [] and db.execute(text("SELECT count(*) FROM thread_registry WHERE origin = 'profile'")).scalar() == 0
    assert project(db, "CS 401") and project(db, "EE 221")


def test_course_seeds_are_whole_course_codes_and_a_series_needs_its_key(db):
    def event(ref, title, day, key=None):
        put(db, "gcal", ref, title, {"attendees": [], "organizer": {"email": ME},
                                     **({"event_key": key} if key else {})}, at(day), title)

    for i in range(5):
        event(f"sync{i}", "Team sync June 2025", i)
        event(f"cs{i}", "CS 401 lecture", 7 * i, "gcal:cs401")
        put(db, "gmail", f"inv{i}", "Invoice 2025 for your plan", {"from": [{"email": "billing@shop.test"}],
                                                                  "to": [{"email": ME}]}, at(i), "invoice")
    for i, title in enumerate(("Dentist", "Haircut", "Oil change", "Library return")):  # one-off events, no series
        event(f"once{i}", title, 30 + i)
    db.commit()
    assert [(sd["how"], sd["name"]) for sd in threads.name_seeds(db)] == [("course", "CS 401"),
                                                                         ("series", "CS 401 lecture")]
    assert threads.course_codes("Team sync JUNE 2025, cs401 and MATH 221") == ["CS 401", "MATH 221"]


@pytest.mark.usefixtures("local_models")
def test_a_resume_project_is_found_by_its_name_never_by_a_word_used_for_other_things(db):
    """"Kestrel Mobile Suite" on your LinkedIn: its records name it, or "Kestrel" alone. Phone bills ("mobile") and
    hotel bookings ("suite") are not part of it."""
    profile = {"is_about_owner": True, "person_name": "Mo Self", "experience": [], "education": [],
               "projects": [{"name": "Kestrel Mobile Suite", "description": "an app", "org": ""}]}
    put(db, "linkedin", "profile", "LinkedIn profile: Mo Self", {"kind": "profile", "person_name": "Mo Self",
                                                                 "profile": profile}, None, "LinkedIn profile of Mo Self")
    mine = []
    for i in range(6):
        life_threads._mail(db, f"k{i}", ME, ["jo@acmerobotics.com"], f"Kestrel Mobile Suite release {i}", at(10 * i),
                           "the new Kestrel Mobile Suite build is out." if i < 4 else "Kestrel crashes on start.")
        mine.append(f"k{i}")
        life_threads._mail(db, f"bill{i}", "billing@phone.test", [ME], f"Your mobile phone bill {i}", at(10 * i + 1),
                           "your mobile plan statement")
    for i in range(9):
        life_threads._mail(db, f"hotel{i}", "stay@hotel.test", [ME], f"Suite booking {i}", at(7 * i + 2),
                           "your suite is confirmed")
    db.commit()
    pipeline.run(db)
    kestrel = project(db, "Kestrel Mobile Suite")
    assert db.execute(text("SELECT terms FROM thread_registry WHERE origin = 'profile'")).scalar() == [
        "Kestrel Mobile Suite", "Kestrel"]
    assert records(db, kestrel) == set(db.execute(text("SELECT id FROM source_items WHERE external_id = ANY(:e)"),
                                                  {"e": mine}).scalars())
