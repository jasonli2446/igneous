"""Contacts exports (vCard): Google Contacts' (3.0), Apple's (3.0, with its item groups and X-ABUID) and vCard 4.0,
read into cards the spine builds people from, keyed so a later contacts sync doesn't duplicate them. Synthetic only."""
import zipfile

import click
import pytest
from fakes import FakePeople
from pipeline_data import day, local_models, mail  # noqa: F401
from sqlalchemy import text
from typer.testing import CliRunner

from igneous import pipeline, sources
from igneous.cli import app as cli
from igneous.connectors import takeout, vcard
from igneous.connectors.base import run_sync
from igneous.connectors.gcal import ContactsConnector
from igneous.graph.identity import card_keys
from igneous.graph.spine import card_name

GOOGLE = """BEGIN:VCARD
VERSION:3.0
FN:Owen Brandt
N:Brandt;Owen;;;
EMAIL;TYPE=INTERNET;TYPE=WORK:Owen@Northwind.example.com
TEL;TYPE=CELL:+1 440-555-0181
TEL;TYPE=WORK:(440) 555-0100
TEL;TYPE=HOME:440.555.0181
ADR;TYPE=WORK:;;22 Pier Rd;Port Avalon;OH;44000;United States
ORG:Northwind Robotics;Software
TITLE:Co-founder & CTO
CATEGORIES:myContacts
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:
N:;;;;
TEL;TYPE=CELL:440.555.0199
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:Lakeside Plumbing
N:;;;;
ORG:Lakeside Plumbing
TEL;TYPE=WORK:(440) 555-0177
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:Only A Name
N:Name;Only;A;;
NOTE:met at the fair
END:VCARD
"""
APPLE = """BEGIN:VCARD
VERSION:3.0
PRODID:-//Apple Inc.//macOS 15.1//EN
N:Rangel;Ina;;;
FN:Ina Rangel
item1.TEL;type=pref:+1 (614) 555-0144
item1.X-ABLabel:_$!<Mobile>!$_
item2.EMAIL;type=INTERNET;type=pref:ina@example.com
X-ABUID:5B9F0E2A-1234-4D2A-9C0E-AB12CD34EF56:ABPerson
END:VCARD
BEGIN:VCARD
VERSION:3.0
N:;;;;
FN:Harbor Street Coffee
ORG:Harbor Street Coffee;
TEL;type=WORK:(440) 555-0150
X-ABShowAs:COMPANY
X-ABUID:9C0E2A5B-4321-4D2A-9C0E-AB12CD34EF56:ABPerson
END:VCARD
"""
V4 = """BEGIN:VCARD
VERSION:4.0
UID:urn:uuid:4fbe8971-0bc3-424c-9c26-36c3e1eff6b1
FN:Jordan Lee
N:Lee;Jordan;;;
TEL;VALUE=uri;TYPE="voice,cell":tel:+1-614-555-0101
TEL;VALUE=uri;TYPE=home:tel:+1-614-555-0102;ext=7
EMAIL;TYPE=work:jlee@example.com
END:VCARD
"""


def by_id(items):
    return {i.external_id: i for i in items}


def test_a_google_contacts_export_several_phones_a_card_without_a_name_and_a_company():
    items, skipped = vcard.cards(GOOGLE.replace("\n", "\r\n").encode())
    assert skipped == 1  # the card with no number or address names no one we could key
    owen, nameless, company = items
    assert owen.source == "vcard" and owen.title == "Owen Brandt" and owen.mime == "text/vcard"
    # Three numbers, two of them one number written two ways: one each.
    assert owen.raw_meta["phones"] == ["+1 440-555-0181", "(440) 555-0100"]
    assert card_keys(owen.raw_meta) == ["owen@northwind.example.com", "+14405550181", "+14405550100"]
    assert owen.raw_meta["orgs"] == ["Northwind Robotics"] and owen.raw_meta["titles"] == ["Co-founder & CTO"]
    assert owen.body_text.splitlines() == [
        "Owen Brandt", "Northwind Robotics, Co-founder & CTO", "Phone (cell): +1 440-555-0181",
        "Phone (work): (440) 555-0100", "Email: owen@northwind.example.com",
        "Address (work): 22 Pier Rd, Port Avalon, OH 44000, United States"]
    assert nameless.title is None and card_name(nameless.title, card_keys(nameless.raw_meta)) is None
    assert company.title is None and company.raw_meta["orgs"] == ["Lakeside Plumbing"]  # a company names no person
    # The same export read again: the same keys.
    assert [i.external_id for i in vcard.cards(GOOGLE.encode())[0]] == [i.external_id for i in items]


def test_apple_and_vcard_4_cards_keep_their_own_ids_and_numbers():
    found = by_id(vcard.cards(APPLE + V4)[0])
    ina = found["uid/5B9F0E2A-1234-4D2A-9C0E-AB12CD34EF56:ABPerson"]
    assert (ina.title, ina.raw_meta["phones"], ina.raw_meta["emails"]) == ("Ina Rangel", ["+1 (614) 555-0144"],
                                                                          ["ina@example.com"])
    assert found["uid/9C0E2A5B-4321-4D2A-9C0E-AB12CD34EF56:ABPerson"].title is None  # X-ABShowAs:COMPANY
    jordan = found["uid/urn:uuid:4fbe8971-0bc3-424c-9c26-36c3e1eff6b1"]
    assert jordan.raw_meta["phones"] == ["+1-614-555-0101", "+1-614-555-0102"]  # tel: scheme and extension gone
    assert card_keys(jordan.raw_meta) == ["jlee@example.com", "+16145550101", "+16145550102"]
    assert "Phone (cell): +1-614-555-0101" in jordan.body_text


def test_files_folders_and_zips_are_read_and_nothing_else(tmp_path):
    (tmp_path / "contacts.vcf").write_text(GOOGLE)
    folder = tmp_path / "icloud"
    folder.mkdir()
    (folder / "Ina Rangel.vcf").write_text(APPLE)
    with zipfile.ZipFile(tmp_path / "export.zip", "w") as zf:
        zf.writestr("Contacts/All Contacts/All Contacts.vcf", V4)
        zf.writestr("Contacts/Friends/Friends.vcf", V4)  # a card in two lists is one record
    items, report = vcard.read([tmp_path / "contacts.vcf", folder, tmp_path / "export.zip"])
    assert (report["cards"], report["without_phone_or_email"], len(report["files"])) == (6, 1, 4)
    assert len({i.external_id for i in items}) == 6
    (tmp_path / "notes.txt").write_text("hi")
    with pytest.raises(vcard.VCardError, match="not a contacts export"):
        vcard.preflight([tmp_path / "notes.txt"])
    (tmp_path / "empty").mkdir()
    with pytest.raises(vcard.VCardError, match=r"No \.vcf"):
        vcard.preflight([tmp_path / "empty"])


@pytest.mark.usefixtures("local_models")
def test_phone_keyed_people_join_their_mail_identities_and_a_later_contacts_sync_adds_no_one(db, tmp_path):
    me = "me@x.test"
    for i, (who, name) in enumerate((("owen@northwind.example.com", "Owen Brandt"), ("priya@north.test", "Priya Raman"))):
        mail(db, f"in{i}", who, [me], f"Hello {i}", day(i), names={who: name})
        mail(db, f"out{i}", me, [who], f"Re: Hello {i}", day(i + 0.5), sent=True)
    export = tmp_path / "contacts.vcf"
    export.write_text(GOOGLE + "BEGIN:VCARD\nVERSION:3.0\nFN:Priya Raman\nTEL;TYPE=CELL:+1 440 555 0162\nEND:VCARD\n")
    first = vcard.run_import(db, [export])
    assert (first["inserted"], first["cards"]) == (4, 4)
    assert vcard.run_import(db, [export])["unchanged"] == 4  # the same export again changes nothing
    db.commit()
    pipeline.run(db, ["spine", "self", "resolve"])

    def cluster(key: str) -> set[str]:
        return set(db.execute(text("""
            WITH h AS (SELECT COALESCE(c.canonical_id, p.id) AS id FROM nodes p LEFT JOIN canonical c ON c.node_id = p.id
                       WHERE p.type = 'Person' AND p.key = :k)
            SELECT n.key FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id
            WHERE n.type = 'Person' AND COALESCE(c.canonical_id, n.id) = (SELECT id FROM h)"""), {"k": key}).scalars())

    # The card ties Owen's numbers to his address; Priya's card has only her number and name, and resolution joins it
    # to her mail by that name.
    assert cluster("+14405550181") == {"owen@northwind.example.com", "+14405550181", "+14405550100"}
    assert cluster("+14405550162") == {"priya@north.test", "+14405550162"}
    phone = db.execute(text("SELECT name, props FROM nodes WHERE type = 'Person' AND key = '+14405550162'")).one()
    assert phone.name == "Priya Raman" and phone.props["named_by"] == "contacts"
    people = db.execute(text("SELECT count(*) FROM nodes WHERE type = 'Person'")).scalar()
    # Google Contacts synced later holds the same card: the same people, named the same, no one added.
    synced = FakePeople([{"resourceName": "people/c1", "names": [{"displayName": "Owen Brandt"}],
                          "emailAddresses": [{"value": "owen@northwind.example.com"}],
                          "phoneNumbers": [{"value": "(440) 555-0181", "canonicalForm": "+14405550181"}]}])
    run_sync(db, ContactsConnector(None, "me@x.test", service=synced), account="me@x.test")
    db.commit()
    pipeline.run(db, ["spine", "self", "resolve"])
    assert db.execute(text("SELECT count(*) FROM nodes WHERE type = 'Person'")).scalar() == people
    assert cluster("+14405550181") == {"owen@northwind.example.com", "+14405550181", "+14405550100"}
    counts = {row["id"]: row["items"] for row in sources.describe(db)}
    assert counts["vcard"] == 4 and counts["gcontacts"] == 1


def test_takeout_contacts_are_the_same_cards(tmp_path):
    z = tmp_path / "takeout-20250801T000000Z-001.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Takeout/Contacts/All Contacts/All Contacts.vcf", GOOGLE)
        zf.writestr("Takeout/Contacts/Work/Work.vcf", GOOGLE.split("END:VCARD")[0] + "END:VCARD\n")
    items, report = takeout.parse([z])
    direct = {i.external_id: i for i in vcard.cards(GOOGLE)[0]}
    assert {i.external_id for i in items if i.source == "vcard"} == set(direct)  # Owen's card in two lists: once
    assert report["parts"] == {"contacts": 3} and all(i.raw_meta["product"] == "Contacts" for i in items)


def test_the_cli_imports_an_export(db, tmp_path, monkeypatch):
    export = tmp_path / "contacts.vcf"
    export.write_text(V4)
    result = CliRunner().invoke(cli, ["import", "vcard", str(export)])
    assert result.exit_code == 0, result.output
    assert click.unstyle(result.output).startswith("contact cards: 1, phone numbers: 2, email addresses: 1; 1 new")
