"""People's names in the order they are read: what `natural_name` turns and what it leaves alone, the spine taking
every source's names that way, identity resolution reading both orders alike, and a database built with surname-first
names after the upgrade and one run."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from igneous.connectors.base import SourceItem, store
from igneous.graph import spine
from igneous.graph.names import natural_name
from igneous.graph.resolve import split_name

T0 = datetime(2001, 5, 14, 16, tzinfo=UTC)


@pytest.mark.parametrize(("written", "read"), [
    ("Rangel, Ina", "Ina Rangel"),
    ("Allen, Phillip K.", "Phillip K. Allen"),
    ("Lavorato, John J.", "John J. Lavorato"),
    ("O'Rourke, Ryan", "Ryan O'Rourke"),
    ("Wesner-Soong, Sarah", "Sarah Wesner-Soong"),
    ("García, José", "José García"),
    ("van Buren, Martin", "Martin van Buren"),
    ("McLaughlin Jr., Errol", "Errol McLaughlin Jr."),   # Outlook writes a suffix with the surname
    ("Nowlan Jr., John L.", "John L. Nowlan Jr."),
    ("Smith, John III", "John Smith III"),
    ("Rangel,Ina", "Ina Rangel"),
    ("Ina Rangel", "Ina Rangel"),                         # already in that order
    ("Pallen", "Pallen"),
])
def test_a_name_written_surname_first_reads_first_name_first(written, read):
    assert natural_name(written) == read
    assert natural_name(read) == read


@pytest.mark.parametrize("name", [
    "Smith, Jr.", "Jones, III",                           # a suffix after the comma
    "Robert W. Huntley, CFP", "Smith, PhD",               # a credential
    "Acme, Inc.", "Foo, LLC", "Globex, Corp.",            # a company
    "Manager, Sales", "Desk, Help", "CApipeline, Team",   # a role, a department, a shared mailbox
    "Smith, John, Jr.", "Houston, TX, USA",               # more than one comma
    "Kevin Hannon, Jim Hughes and John Lavorato",         # a list of people
    "RANGEL, INA", "Butler, TJ",                          # capitals
    "rangel, ina", "Brown, Michael - COO London", "Rangel, Ina (Houston)",
    "Rangel,", ", Ina", "",
])
def test_a_comma_that_means_something_else_is_left_alone(name):
    assert natural_name(name) == name


@pytest.mark.parametrize(("written", "first", "last"), [
    ("Rangel, Ina", "ina", "rangel"),
    ("Allen, Phillip K.", "phillip", "allen"),
    ("Le Dain, Eric", "eric", "ledain"),                  # a surname keeps its particle
    ("de la Cruz, Maria", "maria", "delacruz"),
])
def test_resolution_reads_a_name_alike_in_either_order(written, first, last):
    assert split_name(written, "") == split_name(natural_name(written), "") == (first, last)


def test_a_generational_suffix_is_never_the_surname():
    """Turned, Outlook's "Hardy Jr, John" and "Nowlan Jr., John L." end in "Jr": read as their surname, the two
    would share one (and merge on the six Enron mailboxes); read without it, each is one with his other addresses."""
    assert split_name("John Hardy Jr", "") == ("john", "hardy")
    assert split_name("John L. Nowlan Jr.", "") == split_name("John Nowlan", "") == ("john", "nowlan")
    assert split_name("Errol McLaughlin Jr.", "") == ("errol", "mclaughlin")
    assert split_name("John Jr", "") == ("john", "jr")   # two words: nothing else to call the surname


# --- the spine ---------------------------------------------------------------------------------------------------

def put(s, source, external_id, meta, title="x", hours=0):
    store(s, SourceItem(source=source, external_id=external_id, title=title, body_text="body",
                        occurred_at=T0 + timedelta(hours=hours), raw_meta=meta))


def named(s, key):
    return s.execute(text("SELECT name FROM nodes WHERE type = 'Person' AND key = :k"), {"k": key}).scalar()


def test_the_spine_takes_every_sources_names_first_name_first(db):
    """Enron gives a name only in X-From; Gmail, calendars, cards and LinkedIn give display names. Each record's own
    contribution is stored that way too, so a record changed or deleted later takes the same name it gave."""
    put(db, "email", "allen-p/inbox/1", {
        "from": [{"email": "ina.rangel@enron.com"}], "to": [{"email": "k..allen@enron.com"}], "fingerprint": "fp-1",
        "x_from": "Rangel, Ina </O=ENRON/OU=NA/CN=RECIPIENTS/CN=IRANGEL>"}, "Schedule")
    put(db, "email", "allen-p/sent/1", {
        "from": [{"email": "k..allen@enron.com"}], "to": [{"email": "ina.rangel@enron.com"}], "fingerprint": "fp-2",
        "x_from": "Allen, Phillip K. </O=ENRON/OU=NA/CN=RECIPIENTS/CN=PALLEN>"}, "Re: Schedule", 1)
    put(db, "gcal", "acct/cal/1", {"attendees": [
        {"email": "kirk.mcdaniel@enron.com", "name": "McDaniel, Kirk", "response": "accepted"}]}, "Review", 2)
    put(db, "gcontacts", "acct/people/1", {"emails": ["errol.mclaughlin@enron.com"]}, "McLaughlin Jr., Errol")
    put(db, "linkedin", "connection/ryan", {"kind": "connection", "name": "O'Rourke, Ryan", "email": "ryan@x.test",
                                            "url": "https://www.linkedin.com/in/ryan"}, "O'Rourke, Ryan")
    put(db, "gmail", "acct/3", {"from": [{"email": "orders@acme.test", "name": "Acme, Inc."}],
                                "to": [{"email": "jo@x.test"}], "fingerprint": "fp-3"}, "Receipt", 3)
    spine.run(db)
    assert named(db, "ina.rangel@enron.com") == "Ina Rangel"
    assert named(db, "k..allen@enron.com") == "Phillip K. Allen"
    assert named(db, "kirk.mcdaniel@enron.com") == "Kirk McDaniel"
    assert named(db, "errol.mclaughlin@enron.com") == "Errol McLaughlin Jr."
    assert named(db, "ryan@x.test") == named(db, "linkedin:ryan") == "Ryan O'Rourke"
    assert named(db, "orders@acme.test") == "Acme, Inc."   # a company keeps its comma
    assert db.execute(text("""
        SELECT count(*) FROM spine_node_support x JOIN nodes n ON n.id = x.node_id
        WHERE n.type = 'Person' AND x.name LIKE '%,%'""")).scalar() == 1


def test_a_header_name_written_surname_first_is_still_the_header_name(db):
    """A display name in a header outranks X-From and an address's local part, in either order."""
    put(db, "email", "box/1", {"from": [{"email": "john.lavorato@enron.com"}], "to": [{"email": "jo@x.test"}],
                               "fingerprint": "fp-1", "x_from": "John J Lavorato"}, "Bonus")
    put(db, "gmail", "acct/2", {"from": [{"email": "john.lavorato@enron.com", "name": "Lavorato, John"}],
                                "to": [{"email": "jo@x.test"}], "fingerprint": "fp-2"}, "Re: Bonus", 1)
    spine.run(db)
    assert named(db, "john.lavorato@enron.com") == "John Lavorato"


def test_two_cards_writing_one_name_in_either_order_name_one_person(db):
    """A phone on two cards names its person when the cards name one person, whichever order each writes it in."""
    home = {"phones": ["(614) 555-0100"]}
    put(db, "gcontacts", "acct/people/1", {"emails": ["ina@x.test"], **home}, "Rangel, Ina")
    put(db, "maccontacts", "card/2", {"emails": ["ina@home.test"], **home}, "Ina Rangel", 1)
    spine.run(db)
    assert named(db, "+16145550100") == named(db, "ina@x.test") == "Ina Rangel"
    assert db.execute(text("SELECT count(*) FROM edges WHERE type = 'SAME_AS' AND extractor = 'contacts'")).scalar() == 2


# --- upgrading a database built with surname-first names -----------------------------------------------------------

def test_a_database_built_with_surname_first_names_reads_them_first_name_first_after_one_run(test_db, monkeypatch):
    """Migration 0014: the records whose stored contribution names a person with a comma are pending for the spine
    again, and the next run builds them, and only them, with the names they give now."""
    from alembic import command
    from alembic.config import Config
    from conftest import API, TEST_DB
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session

    from igneous.config import settings

    name = f"{TEST_DB}_names"
    url = make_url(settings.database_url).set(database=name).render_as_string(hide_password=False)
    admin = create_engine(make_url(settings.database_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "migrations"))
    monkeypatch.setattr(settings, "database_url", url)  # migrations/env.py connects to settings.database_url
    engine = create_engine(url)
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{name}"'))
        command.upgrade(config, "0013")
        with Session(engine) as s:   # the spine as it was: names as the records write them
            monkeypatch.setattr(spine, "natural_name", lambda name: name)
            put(s, "email", "allen-p/inbox/1", {
                "from": [{"email": "ina.rangel@enron.com"}], "to": [{"email": "phillip.allen@enron.com"}],
                "fingerprint": "fp-1", "x_from": "Rangel, Ina"}, "Schedule")
            put(s, "email", "allen-p/sent/1", {
                "from": [{"email": "phillip.allen@enron.com"}], "to": [{"email": "ina.rangel@enron.com"}],
                "fingerprint": "fp-2", "x_from": "Phillip K Allen"}, "Re: Schedule", 1)
            put(s, "gcontacts", "acct/people/1", {"emails": ["orders@acme.test"]}, "Acme, Inc.", 2)
            assert spine.run(s) == 3
            assert named(s, "ina.rangel@enron.com") == "Rangel, Ina"
            monkeypatch.setattr(spine, "natural_name", natural_name)
        command.upgrade(config, "head")
        with Session(engine) as s:
            assert s.execute(text("SELECT external_id FROM source_items WHERE spined_hash IS NULL ORDER BY id")) \
                .scalars().all() == ["allen-p/inbox/1", "acct/people/1"]
            assert spine.run(s) == 2
            assert (named(s, "ina.rangel@enron.com"), named(s, "phillip.allen@enron.com"),
                    named(s, "orders@acme.test")) == ("Ina Rangel", "Phillip K Allen", "Acme, Inc.")
            assert s.execute(text("SELECT DISTINCT name FROM spine_node_support WHERE name LIKE '%Ina%'")) \
                .scalars().all() == ["Ina Rangel"]
            assert spine.run(s) == 0
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
