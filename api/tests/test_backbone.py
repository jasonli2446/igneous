"""The backbone: your organizations, each once, with the roles you had there, and the threads of your life under the
org (or the role) they belong to."""
import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from pipeline_data import put, rows
from sqlalchemy import text

from igneous import llm
from igneous.graph import backbone, profile, traversal
from igneous.main import app


def me(s, name: str = "Mo Self") -> int:
    return s.execute(text("""INSERT INTO nodes (type, key, name, props) VALUES ('Person', 'me', :n, '{"self": true}')
                             RETURNING id"""), {"n": name}).scalar_one()


def linkedin(s, positions=(), education=()) -> None:
    """Your LinkedIn export's profile item, as connectors/linkedin.py stores it: positions (org, title, start, end,
    and optionally the description you wrote) and schools (school, degree, start, end)."""
    positions = [(*p, "")[:5] for p in positions]
    put(s, "linkedin", "profile", "LinkedIn profile: Mo Self", {
        "kind": "profile", "person_name": "Mo Self",
        "positions": [{"org": o, "title": t, "description": d, "location": "", "start": a or None, "end": b or None}
                      for o, t, a, b, d in positions],
        "profile": {
            "is_about_owner": True, "person_name": "Mo Self", "projects": [],
            "experience": [{"org": o, "title": t, "start": a, "end": b, "domain_hint": ""} for o, t, a, b, _d in positions],
            "education": [{"school": o, "degree": d, "start": a, "end": b} for o, d, a, b in education]}},
        None, "LinkedIn profile of Mo Self")


def resume(s, when: datetime, experience=(), projects=()) -> None:
    """A resume in your documents folder, with the reading the model gave it earlier."""
    body = "Mo Self\nResume\n" + " ".join(e["title"] for e in experience) + "\n" + "experience " * 60
    put(s, "fs", f"docs/resume-{when:%Y}.pdf", f"Mo_Self_Resume_{when:%Y}.pdf", {"path": f"docs/resume-{when:%Y}.pdf"},
        when, body)
    llm.cache_store(profile.cache_key(body), {
        "is_about_owner": True, "person_name": "Mo Self", "education": [],
        "experience": [{"start": "", "end": "", "domain_hint": "", "parent": "", "project": "", **e} for e in experience],
        "projects": [{"description": "", **p} for p in projects]})


def thread(s, name: str, start: str, end: str, kind: str = "thread") -> int:
    """A life thread the threads stage found, as it writes one."""
    return s.execute(text("""INSERT INTO nodes (type, key, name, props) VALUES ('Project', :k, :n, CAST(:p AS jsonb))
                             RETURNING id"""),
                     {"k": f"thread:{name.lower().replace(' ', '-')}", "n": name, "p": json.dumps({
                         "derived": "threads", "kind": kind, "records": 12,
                         "start": f"{start}T12:00:00+00:00", "end": f"{end}T12:00:00+00:00"})}).scalar_one()


def parents(s) -> dict[str, str]:
    """Each Project's parent Project, by name."""
    return dict(rows(s, """SELECT c.name, p.name FROM edges e JOIN nodes c ON c.id = e.src AND c.type = 'Project'
                           JOIN nodes p ON p.id = e.dst AND p.type = 'Project' WHERE e.type = 'PART_OF'"""))


def test_an_internship_then_a_full_time_job_at_one_company(db):
    """LinkedIn lists the internship and the job; a resume written during the internship calls it ongoing. One link to
    the company holds both roles, each with its dates (the internship ends when LinkedIn, written later, says), and
    the company shows once, with a thread for each role. Each project sits under the role it happened in; one from
    before either stays under the company."""
    me_id = me(db)
    linkedin(db, positions=[("Acme Robotics", "Software Engineer", "2025-01", ""),
                            ("Acme Robotics", "Software Engineering Intern", "2024-06", "2024-08")])
    resume(db, datetime(2024, 7, 15, tzinfo=UTC),
           experience=[{"org": "Acme Robotics", "title": "Software Engineering Intern", "start": "2024-06",
                        "end": "present"}],
           projects=[{"name": n, "org": "Acme Robotics"} for n in ("Atlas", "Kiosk", "Hiring Day")])
    db.commit()
    profile.run(db, log=None)
    link = db.execute(text("""SELECT o.id, e.props, e.valid_from, e.valid_to FROM edges e JOIN nodes o ON o.id = e.dst
                              WHERE e.src = :me AND e.type = 'WORKED_AT'"""), {"me": me_id}).one()
    roles = [{k: r[k] for k in ("title", "start", "end", "via")} for r in link.props["roles"]]
    assert roles == [
        {"title": "Software Engineer", "start": "2025-01-01", "end": None, "via": "your LinkedIn"},
        {"title": "Software Engineering Intern", "start": "2024-06-01", "end": "2024-08-01", "via": "your resume"}]
    assert link.props["title"] == "Software Engineer" and link.valid_to is None
    assert link.valid_from == datetime(2024, 6, 1, tzinfo=UTC)

    thread(db, "Atlas", "2024-06-10", "2024-08-20")
    thread(db, "Kiosk", "2025-03-01", "2025-05-01")
    thread(db, "Hiring Day", "2024-03-01", "2024-04-10")
    backbone.run(db, log=None)
    assert parents(db) == {"Software Engineering Intern": "Acme Robotics", "Software Engineer": "Acme Robotics",
                           "Atlas": "Software Engineering Intern", "Kiosk": "Software Engineer",
                           "Hiring Day": "Acme Robotics"}
    why = dict(rows(db, """SELECT c.name, e.props->>'why' FROM edges e JOIN nodes c ON c.id = e.src
                           WHERE e.type = 'PART_OF' AND c.name IN ('Atlas', 'Software Engineering Intern')"""))
    assert why == {"Software Engineering Intern": "your resume: Software Engineering Intern at Acme Robotics, "
                                                  "Jun 2024 - Aug 2024",
                   "Atlas": "your resume lists it under Acme Robotics, while you were Software Engineering Intern "
                            "there (Jun 2024 - Aug 2024)"}
    # The explorer: the company once among your orgs, opening to its roles, each with when you held it.
    assert [o for o, _school, _until in traversal.your_orgs(db, [me_id])] == [link.id]
    opened = rows(db, "SELECT name FROM nodes WHERE id = ANY(:i)", i=traversal.children(db, link.id, "Organization"))
    assert {n for (n,) in opened} >= {"Software Engineering Intern", "Software Engineer", "Hiring Day"}
    intern = db.execute(text("SELECT id FROM nodes WHERE name = 'Software Engineering Intern'")).scalar_one()
    shown = TestClient(app).get(f"/graph/entity/{intern}").json()
    assert {k: shown[k] for k in ("kind", "start", "end", "role_of")} == {
        "kind": "role", "start": "2024-06-01", "end": "2024-08-01", "role_of": "Acme Robotics"}


def test_overlapping_roles_leave_a_thread_both_hold_under_the_org(db):
    """At State University: a degree, a TA job and a research job that overlap it in spring. A class goes under the
    degree whatever job overlaps it; work goes under the one job whose dates hold it, and work both jobs hold stays
    under the university."""
    me(db)
    linkedin(db, positions=[("State University", "Teaching Assistant", "2023-09", "2024-05"),
                            ("State University", "Research Assistant", "2024-01", "2024-08")],
             education=[("State University", "BS Computer Science", "2021-09", "2025-05")])
    resume(db, datetime(2024, 9, 1, tzinfo=UTC), experience=[],
           projects=[{"name": n, "org": "State University"} for n in ("Grading", "Robot Lab", "Survey")])
    db.commit()
    profile.run(db, log=None)
    thread(db, "CS 401", "2023-10-01", "2023-12-10", kind="class")
    thread(db, "Grading", "2023-10-01", "2023-12-01", kind="TA job")
    thread(db, "Robot Lab", "2024-02-01", "2024-04-01", kind="research")
    thread(db, "Survey", "2024-06-01", "2024-07-15", kind="research")
    backbone.run(db, log=None)
    assert parents(db) == {
        "BS Computer Science": "State University", "Teaching Assistant": "State University",
        "Research Assistant": "State University",
        "CS 401": "BS Computer Science", "Grading": "Teaching Assistant", "Survey": "Research Assistant",
        "Robot Lab": "State University"}


ME = "mo@self.test"
T0 = datetime(2024, 9, 2, 15, tzinfo=UTC)


def found(s, name: str, term: str, sender: str, subjects: list[str], start: int, kind: str = "thread",
          key: str | None = None, named_from: str = "records") -> None:
    """A thread the threads stage found, with its records: mail from `sender` with these subjects, a week apart from
    day `start`. Without the model a thread's kind is 'thread' and its name is its term and months."""
    from pipeline_data import mail

    refs = [f"{term}-{i}" for i in range(len(subjects))]
    for ref, subject, i in zip(refs, subjects, range(len(subjects)), strict=True):
        mail(s, ref, sender, [ME], subject, T0 + timedelta(days=start + 7 * i), body=f"{subject}. See you then.")
    s.commit()
    from igneous.graph import spine
    spine.run(s)
    tid = s.execute(text("""INSERT INTO nodes (type, key, name, props) VALUES ('Project', :k, :n, CAST(:p AS jsonb))
                            RETURNING id"""), {"k": key or f"thread:{term.lower()}", "n": name, "p": json.dumps({
                                "derived": "threads", "kind": kind, "terms": [term], "records": len(refs),
                                "named_from": named_from, "start": (T0 + timedelta(days=start)).isoformat(),
                                "end": (T0 + timedelta(days=start + 7 * len(refs))).isoformat()})}).scalar_one()
    s.execute(text("""INSERT INTO edges (src, dst, type, extractor, confidence)
                      SELECT n.id, :t, 'PART_OF', 'threads', 0.6 FROM nodes n JOIN source_items si ON si.id = n.source_item_id
                      WHERE n.type = 'Message' AND si.external_id = ANY(:r)"""), {"t": tid, "r": refs})


def test_only_what_an_org_holds_goes_under_it_by_its_domain(db):
    """Mo studied at State University and works at Acme Robotics, and wrote with people at both domains. A class, and
    the capstone most of whose mail names it, go under the school; a trip and a side project with classmates, and a
    project with them the model calls a project, don't. At Acme: the hiring thread named for it, the work project the
    LinkedIn description of the job names, and work the model judged work go under it; a thread whose kind no one
    knows doesn't."""
    me(db)
    linkedin(db, positions=[("Acme Robotics", "Engineer", "2025-01", "", "Built Atlas, the fleet dashboard.")],
             education=[("State University", "BS", "2021-09", "2025-05")])
    db.commit()
    profile.run(db, log=None)
    db.execute(text("""UPDATE nodes SET props = props || jsonb_build_object('domains', jsonb_build_array(
                           CASE name WHEN 'State University' THEN 'state.edu' ELSE 'acme.test' END))
                       WHERE type = 'Organization'"""))
    found(db, "CS 401 · Sep-Oct 2024", "CS 401", "cy@state.edu", ["CS 401 homework 1", "CS 401 homework 2",
                                                                  "CS 401 midterm"], 0)
    found(db, "Harborline · Nov 2024", "Harborline", "cy@state.edu", ["Harborline team (CS 481)", "CS 481: Harborline demo",
                                                                      "Harborline repo"], 60)
    found(db, "Lisbon · Mar 2025", "Lisbon", "cy@state.edu", ["Lisbon flights", "Lisbon hostel", "Lisbon!"], 180)
    found(db, "Spring break trip", "Sintra", "cy@state.edu", ["Sintra day trip", "Sintra tickets", "Sintra photos"],
          185, kind="trip")
    found(db, "Tidepool", "Tidepool", "cy@state.edu", ["Tidepool alerts", "Tidepool sensors", "Tidepool launch"], 200,
          kind="project", key="profile:tidepool", named_from="profile")
    found(db, "Hackathon app", "Kestrel", "cy@state.edu", ["Kestrel app", "Kestrel demo", "Kestrel bugs"], 210,
          kind="project")
    found(db, "Acme · Dec 2024", "Acme", "jo@acme.test", ["Acme interview", "Acme offer", "Acme start date"], 90)
    found(db, "Atlas · Feb 2025", "Atlas", "jo@acme.test", ["Atlas review", "Atlas bugs", "Atlas launch"], 160)
    found(db, "Quarterly · Mar 2025", "Quarterly", "jo@acme.test", ["Quarterly plan", "Quarterly numbers",
                                                                     "Quarterly review"], 190)
    found(db, "Beacon rollout", "Beacon", "jo@acme.test", ["Beacon rollout", "Beacon fixes", "Beacon done"], 220,
          kind="work project")
    backbone.run(db, log=None)
    assert parents(db) == {"CS 401 · Sep-Oct 2024": "State University", "Harborline · Nov 2024": "State University",
                           "Acme · Dec 2024": "Acme Robotics", "Atlas · Feb 2025": "Acme Robotics",
                           "Beacon rollout": "Acme Robotics"}
    why = dict(rows(db, """SELECT c.name, e.props->>'why' FROM edges e JOIN nodes c ON c.id = e.src
                           WHERE e.type = 'PART_OF' AND e.extractor = 'backbone'"""))
    assert why["Harborline · Nov 2024"] == "most of its records name CS 481; 100% of its conversations involve state.edu"
    assert why["Acme · Dec 2024"] == "named for Acme Robotics; 100% of its conversations involve acme.test"
    assert why["Atlas · Feb 2025"] == "what you wrote about your Engineer role (since Jan 2025) names Atlas"
    assert why["Beacon rollout"] == "a work project; 100% of its conversations involve acme.test"


def test_why_shows_a_placement_by_its_own_link_and_this_is_wrong_keeps_it_out(db):
    """The explorer draws a class the backbone placed under State University as linked to the university, which
    stands for its own thread. Why shows the placement itself (its reason and its records), drawn between the two;
    "This is wrong" on it takes it out, and the next build doesn't place the class there again."""
    me(db)
    linkedin(db, education=[("State University", "BS", "2021-09", "2025-05")])
    db.commit()
    profile.run(db, log=None)
    db.execute(text("""UPDATE nodes SET props = props || '{"domains": ["state.edu"]}' WHERE type = 'Organization'"""))
    found(db, "CS 401 · Sep-Oct 2024", "CS 401", "cy@state.edu", ["CS 401 homework 1", "CS 401 homework 2",
                                                                  "CS 401 midterm"], 0)
    backbone.run(db, log=None)
    school = db.execute(text("SELECT id FROM nodes WHERE type = 'Organization'")).scalar_one()
    course = db.execute(text("SELECT id FROM nodes WHERE name = 'CS 401 · Sep-Oct 2024'")).scalar_one()
    client = TestClient(app)
    why = client.get("/graph/explain", params={"a": school, "b": course}).json()
    (link,) = why["direct"]
    assert (link["src"], link["dst"], link["src_name"], link["dst_name"]) == (
        course, school, "CS 401 · Sep-Oct 2024", "State University")
    assert link["why"] == "most of its records name CS 401; 100% of its conversations involve state.edu"
    assert sorted(card["title"] for card in link["evidence"]) == ["CS 401 homework 1", "CS 401 homework 2",
                                                                  "CS 401 midterm"]
    assert not link["stated"] and why["path"] is None
    assert client.post("/graph/edge/wrong", json={"edge": link["id"]}).status_code == 200
    assert client.get("/graph/explain", params={"a": school, "b": course}).json()["direct"] == []
    backbone.run(db, log=None)  # the next build: the class is still one, and still mostly the school's mail
    assert "CS 401 · Sep-Oct 2024" not in parents(db)
