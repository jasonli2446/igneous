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


@pytest.mark.usefixtures("trips")
def test_a_name_you_gave_a_thread_names_the_thread_it_is_found_to_be(db):
    """A thread you renamed while it stood on its own is found to be the same as another: your name names the one it
    went into, which lists that rename so it can be undone there, and a name you give that one itself wins."""
    from fastapi.testclient import TestClient

    from igneous.main import app

    db.execute(text("""
        INSERT INTO thread_registry (key, name, kind, terms, span_start, span_end, status, origin, judged) VALUES
            ('thread:lisbon', 'Lisbon trip', 'trip', '{Lisbon}', :a, :b, 'active', 'burst', true),
            ('thread:wanderers', 'lisbon-trip', 'trip', '{Wanderers}', :c, :d, 'active', 'burst', true)
    """), {"a": at(185), "b": at(207), "c": at(193), "d": at(260)})
    db.execute(text("INSERT INTO overrides (op, node_keys, value) VALUES ('rename', '{Project:thread:wanderers}', "
                    "'Winter getaway') RETURNING id"))
    db.commit()
    pipeline.run(db)
    trip = project(db, "Winter getaway")
    assert trip is not None and project(db, "lisbon-trip") is None
    client = TestClient(app)
    [shown] = client.get(f"/graph/entity/{trip}").json()["corrections"]
    assert (shown["op"], shown["value"]) == ("rename", "Winter getaway")
    client.post(f"/graph/entity/{trip}/rename", json={"name": "Lisbon with Cy"})
    assert project(db, "Lisbon with Cy") == trip
    assert [c["value"] for c in client.get(f"/graph/entity/{trip}").json()["corrections"]] == ["Lisbon with Cy"]


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
def test_a_thread_judged_not_one_leaves_the_folders_that_joined_it_to_the_entities_stage(db, monkeypatch):
    """The threads stage alone judges a thread no thread: a folder that had joined it (the entities stage's registry row,
    which has no terms) is never made a thread of its own, which nothing could build; the entities stage places the
    folder again when it runs."""
    from igneous.graph import derived_invariants

    db.execute(text("""
        INSERT INTO thread_registry (key, name, kind, terms, span_start, span_end, status, origin, judged) VALUES
            ('thread:lisbon', 'Lisbon · Dec 2025', 'thread', '{Lisbon}', :a, :b, 'active', 'burst', false),
            ('project:lisbon:1', 'Lisbon', NULL, '{}', NULL, NULL, 'merged', 'entities', false)
    """), {"a": at(185), "b": at(207)})
    db.execute(text("UPDATE thread_registry SET merged_into = 'thread:lisbon' WHERE key = 'project:lisbon:1'"))
    db.commit()
    monkeypatch.setattr(llm, "structured", rejecting("Lisbon"))
    pipeline.run(db, ["threads"])
    assert db.execute(text("SELECT status FROM thread_registry WHERE key = 'thread:lisbon'")).scalar() == "rejected"
    assert db.execute(text("SELECT status FROM thread_registry WHERE key = 'project:lisbon:1'")).scalar() == "merged"
    assert "joined_row_unmerged" not in {v.code for v in derived_invariants.check(db)}
    pipeline.run(db)  # no folder is about the trip any more: its row goes
    assert db.execute(text("SELECT count(*) FROM thread_registry WHERE origin = 'entities'")).scalar() == 0


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
    db.commit()
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
    for i in range(5):  # a room, a suite and an error code are numbered like a course, and are none
        event(f"room{i}", f"Standup in Room 101 ({i})", 40 + i)
        put(db, "gmail", f"lease{i}", "Suite 200 lease renewal", {"from": [{"email": "office@lease.test"}],
                                                                   "to": [{"email": ME}]}, at(40 + i), "lease")
        put(db, "gmail", f"err{i}", "HTTP 404 on the login page", {"from": [{"email": "ci@ops.test"}],
                                                                    "to": [{"email": ME}]}, at(40 + i), "error")
    db.execute(text("""INSERT INTO nodes (type, key, name, props) VALUES ('Thread', 'chat:games', 'Board games',
                                                                          '{"group": true}')"""))
    db.commit()
    # Courses first: a class's project or group chat is part of it.
    assert [(sd["how"], sd["name"]) for sd in threads.name_seeds(db)] == [
        ("course", "CS 401"), ("chat", "Board games"), ("series", "CS 401 lecture")]
    assert threads.course_codes("Team sync JUNE 2025, cs401 and MATH 221") == ["CS 401", "MATH 221"]
    assert threads.course_codes("Room 101, Gate 210, suite 200, HTTP 404") == []
    assert threads.course_code("ROOM 101") is None and threads.course_code("ENVS 410") == "ENVS 410"


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


def capstone(s) -> None:
    """The persona's CS 481 over a spring term, among a year of everyday mail: the professor's weekly mail names the
    course; its project is named "Harborline" (mail and a design doc that name the course too, and the team chat that
    doesn't) and its last event "Demo Day" (the same). A planning note names the course in the autumn."""
    life_threads._everyday(s)
    team = ["+15550000001", "+15550000002"]
    life_threads._mail(s, "plan", ME, ["prof.kim@state.edu"], "Spring courses", at(20),
                       "For spring I'm planning CS 481 and two electives.")
    for i in range(8):
        life_threads._mail(s, f"cs481-{i}", "prof.kim@state.edu", [ME], f"CS 481 week {i + 1}", at(100 + 10 * i),
                           "Readings for this week are posted.")
    for i in range(3):
        life_threads._mail(s, f"team{i}", "ana@pals.test", [ME, "bo@pals.test"], "CS 481 capstone team: Harborline",
                           at(104 + 5 * i), "Harborline, our CS 481 capstone: a live map of the harbor shuttle.")
    life_threads._doc(s, "fs", "school/cs481/harborline-design.md", "harborline-design.md", at(112),
                      "CS 481 capstone: Harborline design.")
    for d in range(4):
        life_threads._chat_day(s, "chat-harbor", "Harborline team", team, at(106 + 6 * d),
                               f"Harborline standup {d}: map matching done")
    for i in range(2):
        life_threads._mail(s, f"demo{i}", "prof.kim@state.edu", [ME], "CS 481 Demo Day", at(168 + 4 * i),
                           "CS 481 Demo Day is Friday, 1-5pm in the ballroom.")
    life_threads._doc(s, "fs", "school/cs481/demo-day-slides.md", "demo-day-slides.md", at(170),
                      "Demo Day slides for CS 481.")
    for d in range(3):
        life_threads._chat_day(s, "chat-harbor", "Harborline team", team, at(166 + 3 * d), f"Demo Day poster {d}")
    s.commit()


def course_thread(s) -> tuple[int, str]:
    (only,) = s.execute(text("""SELECT id, name FROM nodes WHERE type = 'Project' AND props->>'derived' = 'threads'
                                AND (name ILIKE '%481%' OR name ILIKE '%harborline%' OR name ILIKE '%demo day%')""")).all()
    return only


@pytest.mark.usefixtures("local_models")
def test_a_class_found_under_its_project_and_its_demo_day_is_one_thread(db):
    """With the model off: CS 481's code in the professor's mail seeds the class over its term, and the bursts of
    "Harborline" and "Demo Day", most of whose records name the course, are its parts: one thread holds them all."""
    capstone(db)
    pipeline.run(db)
    tid, name = course_thread(db)
    assert name.startswith("CS 481")
    held = records(db, tid)
    chats = set(db.execute(text("SELECT id FROM source_items WHERE source = 'imessage'")).scalars())
    assert chats <= held  # the team chat's days name only the project or the event
    plan = db.execute(text("SELECT id FROM source_items WHERE external_id = 'plan'")).scalar_one()
    assert plan not in held  # the autumn's planning note is outside the term
    parts = db.execute(text("""SELECT name, merged_by FROM thread_registry WHERE status = 'merged'
                               AND origin <> 'entities' ORDER BY name""")).all()
    assert [(n.split(" · ")[0], by) for n, by in parts] == [("Demo Day", "part"), ("Harborline", "part")]
    # Its folder and its team chat are about it: they join it, never a Project beside it.
    joined = db.execute(text("SELECT name FROM thread_registry WHERE origin = 'entities' ORDER BY name")).scalars()
    assert list(joined) == ["Harborline team", "cs481"]
    assert db.execute(text("""SELECT count(*) FROM nodes WHERE type = 'Project'
                              AND props->>'derived' IN ('true', 'threads')""")).scalar() == 1
    before = (name, held)
    pipeline.run(db)
    assert (course_thread(db)[1], records(db, tid)) == before  # a second run changes nothing


@pytest.mark.usefixtures("local_models")
def test_a_class_found_as_two_threads_before_comes_together(db):
    """A registry from before the course was seeded holds the project and the event as threads of their own, and you
    named the project: they name one course in one term, so they are one thread, with no model. They are its parts,
    not the class: the name you gave the project doesn't name the class."""
    capstone(db)
    for key, term, a, b in (("thread:harborline", "Harborline", at(104), at(130)),
                            ("thread:demo-day", "Demo Day", at(166), at(172))):
        db.execute(text("""INSERT INTO thread_registry (key, name, kind, terms, span_start, span_end, status, origin,
                                                        judged)
                           VALUES (:k, :n, 'thread', ARRAY[CAST(:t AS text)], :a, :b, 'active', 'burst', false)"""),
                   {"k": key, "n": f"{term} · then", "t": term, "a": a, "b": b})
    db.execute(text("""INSERT INTO overrides (op, node_keys, value)
                       VALUES ('rename', '{Project:thread:harborline}', 'Harbor shuttle app')"""))
    db.commit()
    pipeline.run(db)
    tid, name = course_thread(db)
    assert name.startswith("CS 481")  # the class keeps the thread and its name; its project and its event fold in
    aliases = set(db.execute(text("SELECT jsonb_array_elements_text(props->'aliases') FROM nodes WHERE id = :t"),
                             {"t": tid}).scalars())
    assert {"Harborline · then", "Demo Day · then"} <= aliases
    assert db.execute(text("""SELECT key, merged_by FROM thread_registry WHERE key IN ('thread:harborline',
                              'thread:demo-day') ORDER BY key""")).all() == [
        ("thread:demo-day", "part"), ("thread:harborline", "part")]


def test_a_shared_course_code_within_one_term_is_one_thread():
    from datetime import timedelta

    from igneous.graph.resolve_threads import same_course

    def thread(i, start, days):
        return {"id": i, "a": at(start), "b": at(start) + timedelta(days=days)}

    codes = {1: {"CS 481"}, 2: {"CS 481"}, 3: {"CS 481"}, 4: {"CS 452"}}
    assert same_course(thread(1, 0, 40), thread(2, 50, 30), codes)["relation"] == "same"  # within NEAR_DAYS
    assert same_course(thread(1, 0, 40), thread(3, 300, 30), codes) is None  # the same code, another term
    assert same_course(thread(1, 0, 40), thread(4, 10, 30), codes) is None  # another course
