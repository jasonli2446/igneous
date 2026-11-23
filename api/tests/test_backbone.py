"""The backbone: your organizations, each once, with the roles you had there, and the threads of your life under the
org (or the role) they belong to."""
import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from pipeline_data import card, put, rows
from sqlalchemy import text

from igneous import llm, pipeline
from igneous.graph import backbone, profile, summary, traversal
from igneous.main import app


def me(s, name: str = "Mo Self") -> int:
    return s.execute(text("""INSERT INTO nodes (type, key, name, props) VALUES ('Person', 'me', :n, '{"self": true}')
                             RETURNING id"""), {"n": name}).scalar_one()


def linkedin(s, positions=(), education=()) -> None:
    """Your LinkedIn export's profile item, as connectors/linkedin.py stores it."""
    put(s, "linkedin", "profile", "LinkedIn profile: Mo Self", {"kind": "profile", "person_name": "Mo Self", "profile": {
        "is_about_owner": True, "person_name": "Mo Self", "projects": [],
        "experience": [{"org": o, "title": t, "start": a, "end": b, "domain_hint": ""} for o, t, a, b in positions],
        "education": [{"school": o, "degree": d, "start": a, "end": b} for o, d, a, b in education]}},
        None, "LinkedIn profile of Mo Self")


def resume(s, when: datetime, experience=(), projects=(), education=()) -> None:
    """A resume in your documents folder, with the reading the model gave it earlier."""
    body = "Mo Self\nResume\n" + " ".join(e["title"] for e in experience) + "\n" + "experience " * 60
    put(s, "fs", f"docs/resume-{when:%Y}.pdf", f"Mo_Self_Resume_{when:%Y}.pdf", {"path": f"docs/resume-{when:%Y}.pdf"},
        when, body)
    llm.cache_store(profile.cache_key(body), {
        "is_about_owner": True, "person_name": "Mo Self",
        "education": [{"degree": "", "start": "", "end": "", "domain_hint": "", **e} for e in education],
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
    # Its summary without the model says what it is: a role, where and when, not "a project".
    assert summary.fallback(summary.gather(db, intern)).startswith(
        "Software Engineering Intern is your role at Acme Robotics, Jun 2024 to Aug 2024.")


def roles_at(s, me_id: int) -> dict[str, list[tuple]]:
    """Your organizations by key, each with its roles (title, start, end, via) as its one WORKED_AT link holds them."""
    return {key: [(r["title"], r["start"], r["end"], r["via"]) for r in props["roles"]]
            for key, props in rows(s, """SELECT o.key, e.props FROM edges e JOIN nodes o ON o.id = e.dst
                                         WHERE e.src = :me AND e.type = 'WORKED_AT' AND e.extractor = 'profile'""",
                                   me=me_id)}


def test_one_organization_spelled_two_ways_keeps_every_role_and_one_degree_spelled_two_ways_is_one(db):
    """Your resume calls your employer Northwind, at northwind.test, whose people's LinkedIn says Northwind Robotics;
    your LinkedIn calls it Northwind Robotics. One organization, keyed as its people's company and named as your newest
    document names it, holding the internship from the resume and the job from LinkedIn. Your degree at State
    University is spelled two ways by the two documents, at the same dates: one degree. A lab your resume gives the
    university's domain is the university's: its job joins the degree there, and the university keeps its name."""
    me_id = me(db)
    put(db, "linkedin", "connection/kai", "Kai Lee", {"kind": "connection", "name": "Kai Lee", "email": "kai@northwind.test",
                                                      "company": "Northwind Robotics", "url": "https://linkedin.com/in/kai"})
    put(db, "linkedin", "connection/cy", "Cy Cole", {"kind": "connection", "name": "Cy Cole", "email": "cy@state.test",
                                                     "company": "State University", "url": "https://linkedin.com/in/cy"})
    linkedin(db, positions=[("Northwind Robotics", "Software Engineer", "2025-01", "")],
             education=[("State University", "Bachelor of Science - BS, Computer Science", "2020-09", "2024-05")])
    resume(db, datetime(2024, 9, 1, tzinfo=UTC),
           experience=[{"org": "Northwind", "title": "Software Engineering Intern", "start": "2024-06",
                        "end": "2024-08", "domain_hint": "northwind.test"},
                       {"org": "Robotics Lab", "title": "Research Assistant", "start": "2023-01", "end": "2023-06",
                        "domain_hint": "state.test"}],
           education=[{"school": "State University", "degree": "B.S. Computer Science", "start": "2020-09",
                       "end": "2024-05"}])
    db.commit()
    profile.run(db, log=None)
    assert roles_at(db, me_id) == {
        "org:northwind-robotics": [("Software Engineer", "2025-01-01", None, "your LinkedIn"),
                                   ("Software Engineering Intern", "2024-06-01", "2024-08-01", "your resume")],
        "org:state-university": [("Research Assistant", "2023-01-01", "2023-06-01", "your resume"),
                                 ("B.S. Computer Science", "2020-09-01", "2024-05-01", "your resume")]}
    assert dict(rows(db, "SELECT key, name FROM nodes WHERE type = 'Organization'")) == {
        "org:northwind-robotics": "Northwind", "org:state-university": "State University"}


def test_your_name_for_a_role_stays_when_its_organization_takes_its_peoples_name(db):
    """Your resume lists two roles at ACME, at acme.test: org:acme, a thread per role, and you rename the internship's.
    Then Ana's card says acme.test is Acme Robotics: the organization is org:acme-robotics, and so are its roles'
    threads (backbone:org:acme-robotics/...), the internship's still by your name."""
    me(db)
    resume(db, datetime(2025, 9, 1, tzinfo=UTC), experience=[
        {"org": "ACME", "title": "Software Engineer", "start": "2025-01", "domain_hint": "acme.test"},
        {"org": "ACME", "title": "Software Engineering Intern", "start": "2024-06", "end": "2024-08",
         "domain_hint": "acme.test"}])
    db.commit()
    stages = ["profile", "organizations", "backbone"]
    pipeline.run(db, stages)
    intern = db.execute(text("SELECT id FROM nodes WHERE key = 'backbone:org:acme/software-engineering-intern'")).scalar()
    renamed = TestClient(app).post(f"/graph/entity/{intern}/rename", json={"name": "My internship"}).json()
    assert renamed["applied"] == "now"
    card(db, "acct/people/1", "Ana Ames", emails=["ana@acme.test"], orgs=["Acme Robotics"])
    db.commit()
    pipeline.run(db, stages)
    assert dict(rows(db, "SELECT key, name FROM nodes WHERE key LIKE 'backbone:%' ORDER BY key")) == {
        "backbone:org:acme-robotics": "ACME", "backbone:org:acme-robotics/software-engineer": "Software Engineer",
        "backbone:org:acme-robotics/software-engineering-intern": "My internship"}


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
