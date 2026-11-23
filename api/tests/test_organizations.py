"""Where people work: WORKED_AT facts from evidence only, one per person and organization, named in people's words."""
import pytest
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


def test_a_domain_takes_the_name_its_people_give_it(db):
    """Ana's contact card says Acme Robotics: the domain is named so, and her card's company is that node."""
    exchange(db, ACME, [0, 10, 21])
    card(db, "acct/people/3", "Ana Ames", emails=["ana@acme.test"], orgs=["Acme Robotics"])
    build(db)
    assert worked_at(db) == {("me", "Acme Robotics"), *((p, "Acme Robotics") for p in ACME)}
    assert db.execute(text("SELECT key FROM nodes WHERE type = 'Organization'")).scalars().all() == ["acme.test"]
    why = db.execute(text("""SELECT e.props->'why' FROM edges e JOIN nodes p ON p.id = e.src
                             WHERE e.extractor = 'organizations' AND p.key = 'ana@acme.test'""")).scalar()
    assert why == ["contact card", "wrote with you from ana@acme.test"]


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


def test_a_name_your_resume_gave_an_organization_stays(db):
    db.execute(text("""INSERT INTO nodes (type, key, name, props)
                       VALUES ('Organization', 'acme.test', 'ACME (resume)', '{"named_by": "profile"}')"""))
    exchange(db, ACME, [0, 10, 21])
    card(db, "acct/people/3", "Ana Ames", emails=["ana@acme.test"], orgs=["Acme Robotics"])
    build(db)
    assert ("me", "ACME (resume)") in worked_at(db)


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
