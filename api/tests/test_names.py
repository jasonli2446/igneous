"""People's names in the order they are read: what `natural_name` turns and what it leaves alone, the spine taking
every source's names that way, identity resolution reading both orders alike, the names Enron's X-headers give (quoted
ones too), and databases built before either after the upgrade and one run."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.connectors.base import SourceItem, store
from igneous.connectors.email_maildir import parse_message
from igneous.graph import spine
from igneous.graph.names import natural_name
from igneous.graph.resolve import split_name
from igneous.graph.spine import x_names

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
    ("Rahman, Md", "Md Rahman"),
    ("Rangel,Ina", "Ina Rangel"),
    ("Ina Rangel", "Ina Rangel"),                         # already in that order
    ("Pallen", "Pallen"),
])
def test_a_name_written_surname_first_reads_first_name_first(written, read):
    assert natural_name(written) == read
    assert natural_name(read) == read


@pytest.mark.parametrize("name", [
    "Smith, Jr.", "Jones, III",                           # a suffix after the comma
    "Robert W. Huntley, CFP", "Smith, PhD", "Smith, MD",  # a credential
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
    """A display name outranks what an address's local part spells, written surname first too: in a From header
    ("Lavorato, John"), or in the X-From where Enron keeps the display name its From header lacks."""
    put(db, "email", "box/1", {"from": [{"email": "john.j.lavorato@enron.com"}], "to": [{"email": "jo@x.test"}],
                               "fingerprint": "fp-1"}, "Bonus")
    put(db, "gmail", "acct/2", {"from": [{"email": "john.j.lavorato@enron.com", "name": "Lavorato, John"}],
                                "to": [{"email": "jo@x.test"}], "fingerprint": "fp-2"}, "Re: Bonus", 1)
    put(db, "email", "box/3", {"from": [{"email": "phillip.k.allen@enron.com"}], "to": [{"email": "jo@x.test"}],
                               "fingerprint": "fp-3", "x_from": "Allen, Phil </O=ENRON/OU=NA/CN=RECIPIENTS/CN=PALLEN>"},
        "Gas", 2)
    put(db, "email", "box/4", {"from": [{"email": "jo@x.test"}], "to": [{"email": "phillip.k.allen@enron.com"}],
                               "fingerprint": "fp-4"}, "Re: Gas", 3)
    spine.run(db)
    assert named(db, "john.j.lavorato@enron.com") == "John Lavorato"   # not "John J Lavorato", the longer name
    assert named(db, "phillip.k.allen@enron.com") == "Phil Allen"


def test_two_cards_writing_one_name_in_either_order_name_one_person(db):
    """A phone on two cards names its person when the cards name one person, whichever order each writes it in."""
    home = {"phones": ["(614) 555-0100"]}
    put(db, "gcontacts", "acct/people/1", {"emails": ["ina@x.test"], **home}, "Rangel, Ina")
    put(db, "maccontacts", "card/2", {"emails": ["ina@home.test"], **home}, "Ina Rangel", 1)
    spine.run(db)
    assert named(db, "+16145550100") == named(db, "ina@x.test") == "Ina Rangel"
    assert db.execute(text("SELECT count(*) FROM edges WHERE type = 'SAME_AS' AND extractor = 'contacts'")).scalar() == 2


# --- the names in Enron's X-headers ------------------------------------------------------------------------------

SENDER = "sender@x.test"


@pytest.mark.parametrize(("x_from", "name"), [
    ('"Mark Skilling" <markskilling@hotmail.com>', "Mark Skilling"),                   # quoted
    ('"Garten, Jeffrey" <jeffrey.garten@yale.edu>', "Garten, Jeffrey"),                # a comma inside the quotes
    ('"Mark \\"Sharky\\" Skilling" <mark@x.test>', 'Mark "Sharky" Skilling'),          # an escaped quote
    ('\\"Jose Garcia\\" <jg@x.test>@ENRON', "Jose Garcia"),                            # quotes escaped outside quotes
    ('"Susan Varga" <svarga@kudlow.com>@ENRON', "Susan Varga"),                        # after Lotus Notes' gateway
    ('"Dennis Elkins" <delkins@interwoven.com>@ENRON <IMCEANOTES-+22Dennis+20Elkins+22@ENRON.com>', "Dennis Elkins"),
    ("Rob Bradley <iertx@hern.org>@ENRON", "Rob Bradley"),                             # unquoted
    ("Faircloth, Linda </O=ENRON/OU=NA/CN=RECIPIENTS/CN=LFAIRCLO>", "Faircloth, Linda"),  # Exchange: surname first
    ("Phillip K Allen", "Phillip K Allen"),                                            # a name alone
    ("Kristin Armstrong (E-mail) <karmstrong@x.test>", "Kristin Armstrong"),           # Outlook's comment
    ('"\\"Rob Bradley\\" <Rob Bradley" <rbradle@x.test>', "Rob Bradley"),               # an address in the name
    ('"\'Ken Lay\'" <klay@enron.com>', "Ken Lay"),
    ('"julia@x.test" <julia@x.test>@ENRON', None),                                     # an address is no name
    ("<svarga@kudlow.com>@ENRON", None),
    ("MZinCS@aol.com@ENRON", None),
    ('"Sender" <sender@x.test>', None),                                                # nor the address's local part
    ("", None),
])
def test_x_from_names_the_sender_as_the_standard_address_parser_reads_it(x_from, name):
    assert x_names({"from": [{"email": SENDER}], "x_from": x_from}).get(SENDER) == name


def test_x_to_x_cc_and_x_bcc_name_each_recipient_by_its_own_entry():
    """Several addresses in one header, each entry matched to its recipient by address. A comma can be inside an
    unquoted name, so an unquoted name after an entry without an address may be the rest of that entry's name
    ("Zuver, Christina") and names no one; a quoted name always counts."""
    to = ["twells@x.test", "markskilling@hotmail.com", "christinazuver@msn.com", "czenner@x.test", "klay@enron.com"]
    meta = {"from": [{"email": SENDER}], "to": [{"email": e} for e in to],
            "cc": [{"email": "jgarten@yale.test"}], "bcc": [{"email": "rob@x.test"}],
            "x_to": ('Rosalee Fleming, Tori L Wells <twells@x.test>, "Mark Skilling" <MarkSkilling@hotmail.com>, '
                     "Zuver, Christina <christinazuver@msn.com>, Lay, Kenneth </O=ENRON/OU=NA/CN=RECIPIENTS/CN=KLAY>, "
                     'Eric Zenner <czenner@x.test>, klay@enron.com, "Pat Ames" <pat@x.test>'),
            "x_cc": '"Garten, Jeffrey" <jgarten@yale.test>@ENRON',
            "x_bcc": "Rob Bradley <rob@x.test>"}
    assert x_names(meta) == {"markskilling@hotmail.com": "Mark Skilling", "czenner@x.test": "Eric Zenner",
                             "jgarten@yale.test": "Garten, Jeffrey", "rob@x.test": "Rob Bradley"}


def test_the_maildir_keeps_the_recipients_x_headers_only_when_a_message_has_them():
    raw = (b'From: ann@x.test\nTo: bo@x.test\nCc: cy@x.test\nSubject: Hi\nX-From: "Ann Lee" <ann@x.test>\n'
           b'X-To: "Bo Ray" <bo@x.test>\nX-cc: Cy Young <cy@x.test>\nX-bcc: \n\nbody\n')
    meta = parse_message(raw, "box/1").raw_meta
    assert (meta["x_from"], meta["x_to"], meta["x_cc"]) == ('"Ann Lee" <ann@x.test>', '"Bo Ray" <bo@x.test>',
                                                            "Cy Young <cy@x.test>")
    assert "x_bcc" not in meta
    plain = parse_message(b"From: ann@x.test\nTo: bo@x.test\nSubject: Hi\n\nbody\n", "box/2").raw_meta
    assert not {"x_to", "x_cc", "x_bcc"} & set(plain)   # mail without them keeps its content hash


def test_the_spine_names_people_by_the_names_their_x_headers_quote(db):
    """"Mark Skilling" <markskilling@hotmail.com> names him "Mark Skilling", never "Markskilling", as the sender in
    X-From and as a recipient in X-To. A name an X-header gives that reads as a person's is a header name: of those,
    the one most records give names the person, not the longest ("Mr. Jeffrey Skilling" in one invitation)."""
    put(db, "email", "lay-k/inbox/1", {
        "from": [{"email": "markskilling@hotmail.com"}], "to": [{"email": "jeff.skilling@enron.com"}],
        "fingerprint": "fp-1", "x_from": '"Mark Skilling" <markskilling@hotmail.com>'}, "Hello")
    for n in (2, 3):
        put(db, "email", f"skilling-j/sent/{n}", {
            "from": [{"email": "jeff.skilling@enron.com"}], "fingerprint": f"fp-{n}", "x_from": "Jeff Skilling",
            "to": [{"email": "markskilling@hotmail.com"}, {"email": "sweison@enron.com"}],
            "x_to": '"Mark Skilling" <markskilling@hotmail.com>, "Weison, Susan" <sweison@enron.com>'}, "Re: Hello", n)
    put(db, "email", "skilling-j/inbox/4", {
        "from": [{"email": "events@x.test"}], "to": [{"email": "jeff.skilling@enron.com"}], "fingerprint": "fp-4",
        "x_to": '"Mr. Jeffrey Skilling" <jeff.skilling@enron.com>'}, "Invitation", 4)
    spine.run(db)
    assert named(db, "markskilling@hotmail.com") == "Mark Skilling"
    assert named(db, "sweison@enron.com") == "Susan Weison"
    assert named(db, "jeff.skilling@enron.com") == "Jeff Skilling"
    assert named(db, "events@x.test") == "Events"   # no X-From: its address's local part


# --- upgrading a database built before ------------------------------------------------------------------------------

@contextmanager
def database_at(revision, monkeypatch):
    """A database of its own migrated to `revision`: yields its engine and a function that migrates it further."""
    from alembic import command
    from alembic.config import Config
    from conftest import API, TEST_DB
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

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
        command.upgrade(config, revision)
        yield engine, lambda to: command.upgrade(config, to)
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def pending(s) -> list[str]:
    return s.execute(text("SELECT external_id FROM source_items WHERE spined_hash IS NULL ORDER BY id")).scalars().all()


def test_a_database_built_with_surname_first_names_reads_them_first_name_first_after_one_run(test_db, monkeypatch):
    """Migration 0014: the records whose stored contribution names a person with a comma are pending for the spine
    again, and the next run builds them, and only them, with the names they give now."""
    with database_at("0013", monkeypatch) as (engine, upgrade):
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
        upgrade("0014")
        with Session(engine) as s:
            assert pending(s) == ["allen-p/inbox/1", "acct/people/1"]
            assert spine.run(s) == 2
            assert (named(s, "ina.rangel@enron.com"), named(s, "phillip.allen@enron.com"),
                    named(s, "orders@acme.test")) == ("Ina Rangel", "Phillip K Allen", "Acme, Inc.")
            assert s.execute(text("SELECT DISTINCT name FROM spine_node_support WHERE name LIKE '%Ina%'")) \
                .scalars().all() == ["Ina Rangel"]
            assert spine.run(s) == 0


def test_a_database_built_before_reads_the_names_x_from_quotes_after_one_run(test_db, monkeypatch):
    """Migration 0016: every mail record with an X-From is pending for the spine again, and the next run builds it, and
    only it, with the names its X-headers give: "Mark Skilling" for "Mark Skilling" <markskilling@hotmail.com>."""
    with database_at("0015", monkeypatch) as (engine, upgrade):
        with Session(engine) as s:   # a quoted X-From named no one
            current = spine.x_names
            monkeypatch.setattr(spine, "x_names", lambda meta: {})
            put(s, "email", "lay-k/inbox/1", {
                "from": [{"email": "markskilling@hotmail.com"}], "to": [{"email": "kenneth.lay@enron.com"}],
                "fingerprint": "fp-1", "x_from": '"Mark Skilling" <markskilling@hotmail.com>'}, "Hello")
            put(s, "email", "lay-k/inbox/2", {
                "from": [{"email": "jeff.skilling@enron.com"}], "to": [{"email": "kenneth.lay@enron.com"}],
                "fingerprint": "fp-2", "x_from": None}, "Hi", 1)
            put(s, "gmail", "acct/3", {"from": [{"email": "ann@x.test", "name": "Ann Lee"}],
                                       "to": [{"email": "jo@x.test"}], "fingerprint": "fp-3"}, "Lunch", 2)
            assert spine.run(s) == 3
            assert named(s, "markskilling@hotmail.com") == "Markskilling"
            monkeypatch.setattr(spine, "x_names", current)
        upgrade("head")
        with Session(engine) as s:
            assert pending(s) == ["lay-k/inbox/1"]
            assert spine.run(s) == 1
            assert named(s, "markskilling@hotmail.com") == "Mark Skilling"
            assert spine.run(s) == 0
