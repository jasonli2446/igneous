"""Where people work: WORKED_AT facts from evidence only, one per person and organization, named in people's words;
one node per organization, whichever stage meets it first, so two builds of the same data agree."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import API
from pipeline_data import card, day, event, local_models, mail, put, rows  # noqa: F401
from sqlalchemy import text

from igneous import llm, pipeline
from igneous.graph import organizations, spine

pytestmark = pytest.mark.usefixtures("local_models")
ME = "me@gmail.com"  # your address at a mail provider: never an employer
ACME = ["ana@acme.test", "bo@acme.test", "cy@acme.test"]


def build(s) -> None:
    s.commit()
    pipeline.run(s, ["spine", "self", "resolve", "organizations"])


def worked_at(s) -> set[tuple[str, str]]:
    """(person key, organization name) for every WORKED_AT this stage wrote."""
    return set(rows(s, """SELECT p.key, o.name FROM edges e JOIN nodes p ON p.id = e.src JOIN nodes o ON o.id = e.dst
                          WHERE e.type = 'WORKED_AT' AND e.extractor = 'organizations'"""))


def exchange(s, people: list[str], days: list[int]) -> None:
    """You write to each of `people` and each writes back, on each of `days`."""
    for p in people:
        for d in days:
            mail(s, f"to-{p}-{d}", ME, [p], f"Plan {d}", day(d), sent=True)
            mail(s, f"from-{p}-{d}", p, [ME], f"Re: Plan {d}", day(d + 1))


def test_two_spellings_of_one_company_make_one_fact(db):
    """Sam's Google and Mac contact cards spell the company two ways."""
    card(db, "acct/people/7", "Sam Ito", emails=["sam@ito.dev"], orgs=["Acme Inc"])
    card(db, "card-7", "Sam Ito", emails=["sam@ito.dev"], orgs=["Acme Inc."], source="maccontacts")
    db.commit()
    spine.run(db)
    organizations.run(db)
    facts = rows(db, """SELECT p.key, o.key, o.name, (SELECT count(*) FROM edge_evidence ev WHERE ev.edge_id = e.id)
                        FROM edges e JOIN nodes p ON p.id = e.src JOIN nodes o ON o.id = e.dst
                        WHERE e.type = 'WORKED_AT' AND e.extractor = 'organizations'""")
    assert len(facts) == 1
    (person, key, org, evidence), = facts
    assert person == "sam@ito.dev" and key == "org:acme-inc" and org in ("Acme Inc", "Acme Inc.") and evidence == 2


def test_a_sustained_two_way_exchange_with_three_people_is_where_you_worked(db):
    exchange(db, ACME, [0, 10, 21])
    build(db)
    assert worked_at(db) == {("me", "acme.test"), *((p, "acme.test") for p in ACME)}
    start, end = db.execute(text("""SELECT valid_from, valid_to FROM edges e JOIN nodes p ON p.id = e.src
                                    WHERE e.extractor = 'organizations' AND p.key = 'me'""")).one()
    assert (start.date(), end.date()) == (day(0).date(), day(22).date())


@pytest.mark.parametrize(("people", "days"), [(ACME[:2], [0, 10, 21]), (ACME, [0, 1, 2]), (ACME, [0])])
def test_fewer_people_or_a_shorter_exchange_says_nothing(db, people, days):
    exchange(db, people, days)
    build(db)
    assert worked_at(db) == set()


def test_one_message_or_a_calendar_invitation_is_not_employment(db):
    mail(db, "m1", ME, ["kai@corp.test"], "Hello", day(0), sent=True)
    for i in range(3):
        event(db, f"acct/cal/demo_{i}", "Vendor demo", day(7 * i), ["guest@othercorp.test", ME], "host@acme.test",
              "demo")
    build(db)
    assert worked_at(db) == set()


def test_your_own_address_at_a_domain_where_you_wrote_with_someone(db):
    """Your work address (you sent from it) with a colleague there; a vanity domain only you use is no employer."""
    mail(db, "w1", "me@employer.test", ["boss@employer.test"], "Report", day(0), sent=True)
    mail(db, "w2", "boss@employer.test", ["me@employer.test"], "Re: Report", day(30))
    mail(db, "v1", "me@mo.dev", ["friend@gmail.com"], "Hi", day(5), sent=True)
    build(db)
    assert worked_at(db) == {("me", "employer.test"), ("boss@employer.test", "employer.test")}
    start, end = db.execute(text("""SELECT valid_from, valid_to FROM edges e JOIN nodes p ON p.id = e.src
                                    WHERE e.extractor = 'organizations' AND p.key = 'me'""")).one()
    assert (start.date(), end.date()) == (day(0).date(), day(30).date())


def test_a_phone_only_card_names_the_employer_of_whoever_texts_from_that_number(db):
    card(db, "acct/people/2", "Lee Wong", phones=["(614) 555-0199"], orgs=["Initech"])
    put(db, "imessage", "c2/2025-03-05", "Lee Wong", {"chat": "c2", "participants": ["+16145550199"],
                                                      "senders": ["+16145550199", "me"]}, day(2))
    build(db)
    assert worked_at(db) == {("+16145550199", "Initech")}


def organizations_(s) -> list[tuple]:
    """(key, name, domains) of every Organization."""
    return rows(s, "SELECT key, name, props->'domains' FROM nodes WHERE type = 'Organization' ORDER BY key")


def test_a_domain_takes_the_name_its_people_give_it(db):
    """Ana's contact card says Acme Robotics: the domain is that company's, one node keyed as the company is (as
    LinkedIn and every card key it), and her card's fact and her mail's are one."""
    exchange(db, ACME, [0, 10, 21])
    card(db, "acct/people/3", "Ana Ames", emails=["ana@acme.test"], orgs=["Acme Robotics"])
    build(db)
    assert worked_at(db) == {("me", "Acme Robotics"), *((p, "Acme Robotics") for p in ACME)}
    assert organizations_(db) == [("org:acme-robotics", "Acme Robotics", ["acme.test"])]
    why = db.execute(text("""SELECT e.props->'why' FROM edges e JOIN nodes p ON p.id = e.src
                             WHERE e.extractor = 'organizations' AND p.key = 'ana@acme.test'""")).scalar()
    assert why == ["contact card", "wrote with you from ana@acme.test"]


def test_a_domain_is_the_linkedin_company_its_people_work_at(db):
    """Two connections at acme.test list Acme Robotics: the domain, your mail there and their LinkedIn company are one
    organization, the one the spine made for their company."""
    exchange(db, ACME, [0, 10, 21])
    for who in ("ana", "bo"):
        put(db, "linkedin", f"connection/{who}", who.title(), {
            "kind": "connection", "name": who.title(), "email": f"{who}@acme.test", "company": "Acme Robotics",
            "url": f"https://www.linkedin.com/in/{who}"})
    build(db)
    assert organizations_(db) == [("org:acme-robotics", "Acme Robotics", ["acme.test"])]
    assert ("me", "Acme Robotics") in worked_at(db)
    assert rows(db, """SELECT p.key, o.key FROM edges e JOIN nodes p ON p.id = e.src JOIN nodes o ON o.id = e.dst
                       WHERE e.type = 'MEMBER_OF' ORDER BY 1""") == [("ana@acme.test", "org:acme-robotics"),
                                                                     ("bo@acme.test", "org:acme-robotics")]


def test_a_domain_the_model_names_is_the_company_your_records_name_too(db, monkeypatch):
    """With no word from its people, the model's name joins the domain to a company only one of your records names;
    otherwise the domain is an organization of its own, under the model's name."""
    monkeypatch.setattr(llm, "structured", lambda *a, **k: ({"name": "Acme Robotics", "confidence": 0.9}, {}))
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    exchange(db, ACME, [0, 10, 21])
    build(db)
    assert organizations_(db) == [("acme.test", "Acme Robotics", ["acme.test"])]
    card(db, "acct/people/9", "Lu Park", emails=["lu@park.dev"], orgs=["Acme Robotics"])  # her own address
    build(db)
    assert organizations_(db) == [("org:acme-robotics", "Acme Robotics", ["acme.test"])]
    assert worked_at(db) == {("me", "Acme Robotics"), ("lu@park.dev", "Acme Robotics"),
                             *((p, "Acme Robotics") for p in ACME)}


def test_without_their_words_a_domain_is_named_once_by_the_model_then_kept(db, monkeypatch):
    asked = []

    def model(system, user, schema, **options):
        asked.append((user, options.get("model")))
        return {"name": "Acme Robotics", "confidence": 0.9}, {}

    exchange(db, ACME, [0, 10, 21])
    monkeypatch.setattr(llm, "structured", model)
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    build(db)
    assert ("me", "Acme Robotics") in worked_at(db)
    assert len(asked) == 1 and "Domain: acme.test" in asked[0][0] and asked[0][1] == llm.settings.llm_cheap_model
    build(db)
    assert len(asked) == 1  # the answer is stored, one per domain


def resume(s, experience: list[dict], education: list[dict] = (), when=None) -> None:
    """Your resume in your documents folder, with the reading the model gave it earlier (so no call is needed)."""
    from igneous.graph import profile

    body = "Mo Self\nExperience\n" + "\n".join(f"{e['org']} - {e['title']}" for e in experience) + "\n" + "skills " * 80
    put(s, "fs", "docs/Mo_Self_Resume.pdf", "Mo_Self_Resume.pdf", {"path": "docs/Mo_Self_Resume.pdf"},
        when or day(30), body)
    reading = {"is_about_owner": True, "person_name": "Mo Self", "projects": [],
               "experience": [{"start": "", "end": "", "domain_hint": "", "parent": "", "project": "", **e}
                              for e in experience],
               "education": [{"degree": "", "start": "", "end": "", "domain_hint": "", **e} for e in education]}
    llm.cache_store(profile.cache_key(body), reading)


def test_a_name_your_resume_gave_an_organization_stays(db):
    """Your resume calls your employer ACME, at acme.test, where Ana's card says Acme Robotics: one organization, keyed
    as its people's company and named as you name it, holding your resume's link, your mail's and Ana's card."""
    names = {ME: "Mo Self"}
    for p in ACME:
        for d in (0, 10, 21):
            mail(db, f"to-{p}-{d}", ME, [p], f"Plan {d}", day(d), sent=True, names=names)
            mail(db, f"from-{p}-{d}", p, [ME], f"Re: Plan {d}", day(d + 1), names=names)
    card(db, "acct/people/3", "Ana Ames", emails=["ana@acme.test"], orgs=["Acme Robotics"])
    resume(db, [{"org": "ACME (resume)", "title": "Engineer", "start": "2025-01", "domain_hint": "acme.test"}])
    db.commit()
    pipeline.run(db, ["spine", "self", "resolve", "profile", "organizations"])
    assert organizations_(db) == [("org:acme-robotics", "ACME (resume)", ["acme.test"])]
    assert ("me", "ACME (resume)") in worked_at(db) and ("ana@acme.test", "ACME (resume)") in worked_at(db)
    assert rows(db, """SELECT e.extractor, e.props->>'domain' FROM edges e JOIN nodes o ON o.id = e.dst
                       JOIN nodes p ON p.id = e.src WHERE p.key = 'me' AND e.type = 'WORKED_AT' ORDER BY 1""") == [
        ("organizations", None), ("profile", "acme.test")]


def test_a_company_keeps_a_name_when_the_record_that_named_it_goes(db):
    """A LinkedIn connection names Globex Corp; your card says Lu works there. Without the connection, the company is
    still where Lu works, named as the card spells it rather than by its key."""
    put(db, "linkedin", "connection/kim", "Kim Wu", {"kind": "connection", "name": "Kim Wu", "company": "Globex Corp",
                                                     "url": "https://www.linkedin.com/in/kim"})
    card(db, "acct/people/1", "Lu Park", emails=["lu@park.dev"], orgs=["Globex Corp."])
    build(db)
    assert worked_at(db) == {("lu@park.dev", "Globex Corp")}
    db.execute(text("DELETE FROM source_items WHERE source = 'linkedin'"))
    build(db)
    assert worked_at(db) == {("lu@park.dev", "Globex Corp.")}


def career(s) -> None:
    """A career as the records state it: LinkedIn's positions and school (with no domain), a resume employer with its
    domain, two-way mail with three people at each one's domain, and the company names their people give (a LinkedIn
    connection, contact cards)."""
    names = {ME: "Mo Self"}
    profile = {"is_about_owner": True, "person_name": "Mo Self", "projects": [],
               "experience": [{"org": "Acme Robotics", "title": "Engineer", "start": "2024-06", "end": "",
                               "domain_hint": ""}],
               "education": [{"school": "State University", "degree": "BS", "start": "2020-09", "end": "2024-05"}]}
    put(s, "linkedin", "profile", "LinkedIn profile: Mo Self", {"kind": "profile", "person_name": "Mo Self",
                                                                "profile": profile}, None, "LinkedIn profile of Mo Self")
    put(s, "linkedin", "connection/ana", "Ana Ames", {"kind": "connection", "name": "Ana Ames", "email": "ana@acme.test",
                                                      "company": "Acme Robotics", "url": "https://www.linkedin.com/in/ana"})
    card(s, "acct/people/1", "Cy Cole", emails=["cy@state.edu"], orgs=["State University"])
    card(s, "acct/people/2", "Ida Ito", emails=["ida@initech.test"], orgs=["Initech LLC"])
    resume(s, [{"org": "Initech", "title": "Analyst", "start": "2023-01", "end": "2023-12",
                "domain_hint": "initech.test"}], when=day(200))
    for domain, start in (("acme.test", 0), ("state.edu", 40), ("initech.test", 80)):
        for who in ("ana", "bo", "cy") if domain != "initech.test" else ("ida", "jo", "kai"):
            for d in (start, start + 10, start + 21):
                mail(s, f"to-{who}-{d}", ME, [f"{who}@{domain}"], f"Plan {d}", day(d), sent=True, names=names)
                mail(s, f"from-{who}-{d}", f"{who}@{domain}", [ME], f"Re: Plan {d}", day(d + 1), names=names)
    s.commit()


def org_snapshot(s) -> dict:
    """The organizations a build derived, by natural keys: every Organization node (its id must not change either)
    and every WORKED_AT link."""
    def jsonable(r) -> tuple:
        return tuple(json.dumps(v, sort_keys=True, default=str) if isinstance(v, (dict, list)) else str(v) for v in r)
    out = {"orgs": [jsonable(r) for r in s.execute(text(
               "SELECT id, key, name, props FROM nodes WHERE type = 'Organization' ORDER BY key"))],
           "worked_at": [jsonable(r) for r in s.execute(text("""
               SELECT p.key, o.key, e.extractor, e.props, e.valid_from, e.valid_to, e.confidence
               FROM edges e JOIN nodes p ON p.id = e.src JOIN nodes o ON o.id = e.dst
               WHERE e.type = 'WORKED_AT' ORDER BY 1, 2, 3"""))]}
    s.rollback()
    return out


REBUILD = """
import sys
sys.path.insert(0, sys.argv[1])
import pipeline_data
from igneous import pipeline, providers
from igneous.db import SessionLocal

models = pipeline_data.LocalModels()
for factory in ("_embedder", "_reranker", "_chat_model"):
    setattr(providers, factory, lambda: models)
with SessionLocal() as s:
    pipeline.run(s)
"""


def test_a_fresh_build_and_a_second_one_give_the_same_organizations_whatever_the_hash_seed(committed_db):
    """Your resume and LinkedIn name your employers and school; your mail finds them by domain; their people's
    LinkedIn and cards name them too. Each is one node from the first build on: the profile stage, which runs first,
    and the organizations stage key it alike, so a second build, in a process with another hash seed, changes
    nothing."""
    career(committed_db)
    snapshots = []
    for seed in ("1", "2"):
        subprocess.run([sys.executable, "-c", REBUILD, str(Path(__file__).parent)], check=True, cwd=API,
                       env={**os.environ, "PYTHONHASHSEED": seed})
        snapshots.append(org_snapshot(committed_db))
    first, second = snapshots
    assert second == first
    orgs = rows(committed_db, """SELECT key, name, props->'domains' FROM nodes WHERE type = 'Organization'
                                 ORDER BY key""")
    assert orgs == [("org:acme-robotics", "Acme Robotics", ["acme.test"]),
                    ("org:initech-llc", "Initech", ["initech.test"]),  # its people's key, your resume's name
                    ("org:state-university", "State University", ["state.edu"])]
    yours = rows(committed_db, """SELECT o.key, e.extractor, e.props->>'school' FROM edges e
                                  JOIN nodes o ON o.id = e.dst JOIN nodes p ON p.id = e.src
                                  WHERE e.type = 'WORKED_AT' AND p.key = 'me' ORDER BY 1, 2""")
    assert yours == [("org:acme-robotics", "organizations", None), ("org:acme-robotics", "profile", None),
                     ("org:initech-llc", "organizations", None), ("org:initech-llc", "profile", None),
                     ("org:state-university", "organizations", "true"), ("org:state-university", "profile", "true")]
    threads = rows(committed_db, """SELECT o.key, t.key FROM nodes o JOIN nodes t ON t.id = (o.props->>'thread')::bigint
                                    WHERE o.type = 'Organization' ORDER BY 1""")
    assert threads == [(k, f"backbone:{k}") for k, _n, _d in orgs]  # one backbone thread each
    from igneous.graph.invariants import check_graph
    assert [v for v in check_graph(committed_db) if v.code == "domain_held_twice"] == []


def test_check_graph_reports_a_domain_two_organizations_hold(db):
    from igneous.graph.derived_invariants import domain_violations

    db.execute(text("""INSERT INTO nodes (type, key, name, props) VALUES
                       ('Organization', 'acme.test', 'Acme', '{}'),
                       ('Organization', 'org:acme', 'Acme', '{"domains": ["acme.test"]}'),
                       ('Organization', 'org:globex', 'Globex', '{"domains": ["globex.test"]}')"""))
    assert [(v.code, v.details) for v in domain_violations(db)] == [
        ("domain_held_twice", {"domain": "acme.test", "organizations": ["acme.test", "org:acme"]})]
