"""Google Calendar's iCalendar export (Takeout's Calendar/*.ics) read as Google Calendar records: occurrences of a
repeating event, its changed and cancelled ones, all-day events, time zones, and keys a later Calendar sync matches.
Synthetic data only."""
from datetime import UTC, datetime

from fakes import FakeCalendar
from sqlalchemy import text

from igneous.connectors import contentlines, ical
from igneous.connectors.base import run_sync
from igneous.connectors.gcal import CalendarConnector


def calendar(*events: str, name: str = "sam@example.com", zone: str = "America/New_York") -> bytes:
    """A calendar file as Google writes one: CRLF lines, events stamped with the export's time."""
    head = ["BEGIN:VCALENDAR", "PRODID:-//Google Inc//Google Calendar 70.9054//EN", "VERSION:2.0",
            "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{name}", f"X-WR-TIMEZONE:{zone}"]
    body = [line for e in events for line in ["BEGIN:VEVENT", *e.strip().splitlines(), "DTSTAMP:20251102T151502Z",
                                               "END:VEVENT"]]
    return ("\r\n".join([*head, *body, "END:VCALENDAR"]) + "\r\n").encode()


WEEKLY = """DTSTART;TZID=America/New_York:20241028T100000
DTEND;TZID=America/New_York:20241028T105000
RRULE:FREQ=WEEKLY;WKST=SU;UNTIL=20241109T045959Z;BYDAY=FR,MO,WE
EXDATE;TZID=America/New_York:20241101T100000
ORGANIZER;CN=Sam Lee:mailto:sam@example.com
UID:abc123@google.com
ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;CN=Jordan Lee;X-NUM-GUESTS=0:mailto:Jordan
 @Example.com
ATTENDEE;CUTYPE=INDIVIDUAL;PARTSTAT=DECLINED;CN=pat@example.com:mailto:pat@example.com
ATTENDEE;CUTYPE=RESOURCE;PARTSTAT=ACCEPTED;CN=Room 110:mailto:c_110@resource.calendar.google.com
SUMMARY:CS 395 Senior project
DESCRIPTION:Bring laptops\\, please\\nRoom change on Fridays\\n\\n-::~:~::~:~:~:~:~:~::~:~::-\\nJoin with Goo
 gle Meet: https://meet.google.com/abc-defg-hij\\nPlease do not edit this section.\\n-::~:~::~:~:~:~:~:~::~:~::-
LOCATION:Hollis 110"""
MOVED = """DTSTART;TZID=America/New_York:20241030T110000
DTEND;TZID=America/New_York:20241030T115000
RECURRENCE-ID;TZID=America/New_York:20241030T100000
UID:abc123@google.com
SUMMARY:CS 395 Senior project (moved)"""
CANCELLED = """DTSTART;TZID=America/New_York:20241106T100000
RECURRENCE-ID;TZID=America/New_York:20241106T100000
STATUS:CANCELLED
UID:abc123@google.com"""
HOLIDAY = """DTSTART;VALUE=DATE:20241225
DTEND;VALUE=DATE:20241226
UID:xmas2024@google.com
SUMMARY:Holiday at home"""


def records(data: bytes, file: str = "sam@example.com", chat_user: str | None = None):
    cal = ical.read(data, file)
    report = ical.Report()
    return {i.external_id: i for c in ical.listings([cal], ical.account([cal], chat_user), report)
            for i in c.items}, report


def test_a_weekly_series_is_each_occurrence_keyed_as_the_calendar_api_keys_it():
    found, report = records(calendar(WEEKLY, MOVED, CANCELLED, HOLIDAY))
    stem = "sam@example.com/sam@example.com/abc123"
    # Mon Oct 28, Wed Oct 30 (moved to 11:00 but still named by its original start), Mon Nov 4 (after the switch to
    # standard time, still 10:00 in New York); Fri Nov 1 excluded, Wed Nov 6 cancelled.
    assert sorted(found) == [f"{stem}_20241028T140000Z", f"{stem}_20241030T140000Z", f"{stem}_20241104T150000Z",
                             f"{stem}_20241108T150000Z", "sam@example.com/sam@example.com/xmas2024"]
    first = found[f"{stem}_20241028T140000Z"]
    assert first.source == "gcal" and first.title == "CS 395 Senior project" and first.mime == "text/calendar"
    assert first.occurred_at == datetime(2024, 10, 28, 14, tzinfo=UTC)
    assert first.body_text == "Bring laptops, please\nRoom change on Fridays\nHollis 110"  # Google's Meet block goes
    meta = first.raw_meta
    assert meta["attendees"] == [{"name": "Jordan Lee", "email": "jordan@example.com", "response": "accepted"},
                                 {"name": "", "email": "pat@example.com", "response": "declined"}]  # no rooms
    assert meta["organizer"] == {"name": "Sam Lee", "email": "sam@example.com"}
    assert meta["start"] == {"dateTime": "2024-10-28T10:00:00-04:00", "timeZone": "America/New_York"}
    assert (meta["series_key"], meta["event_key"], meta["recurring"]) == (stem, "gcal:abc123@google.com", True)
    assert (meta["account"], meta["calendar_id"], meta["calendar"]) == ("sam@example.com",) * 3
    moved = found[f"{stem}_20241030T140000Z"]
    assert moved.title.endswith("(moved)") and moved.occurred_at == datetime(2024, 10, 30, 15, tzinfo=UTC)
    assert found[f"{stem}_20241104T150000Z"].raw_meta["start"]["dateTime"] == "2024-11-04T10:00:00-05:00"
    holiday = found["sam@example.com/sam@example.com/xmas2024"].raw_meta
    assert holiday["start"] == {"date": "2024-12-25"} and holiday["end"] == {"date": "2024-12-26"}
    assert holiday["recurring"] is False and holiday["series_key"] == "sam@example.com/sam@example.com/xmas2024@google.com"
    assert holiday["organizer"] == {"name": "", "email": "sam@example.com"}  # no organizer: the calendar's own
    assert (report.events, report.occurrences, report.notes()) == (2, 5, [])


def test_a_repeating_event_is_read_up_to_the_export_and_its_floating_until_in_the_calendars_zone():
    # The export (Nov 2 2025) holds a weekly meeting with no end: its occurrences stop there, not a forecast after.
    weekly = """DTSTART:20251006T150000Z
RRULE:FREQ=WEEKLY
UID:weekly@google.com
SUMMARY:Weekly sync"""
    # Floating times, in the calendar's zone (Los Angeles): UNTIL is 8pm there on Jan 8, so Jan 8 is the last.
    floating = """DTSTART:20250106T200000
RRULE:FREQ=DAILY;UNTIL=20250108T200000
UID:floating@google.com
SUMMARY:Floating daily"""
    found, _ = records(calendar(weekly, floating, zone="America/Los_Angeles"))
    assert sorted(k.rsplit("_", 1)[1] for k in found if "/weekly_" in k) == [
        f"202510{d}T150000Z" for d in ("06", "13", "20", "27")]
    assert sorted(k.rsplit("_", 1)[1] for k in found if "/floating_" in k) == [
        "20250107T040000Z", "20250108T040000Z", "20250109T040000Z"]


def test_an_event_that_cannot_be_read_is_counted_and_the_rest_are_kept():
    broken = """DTSTART:99991231T230000Z
DURATION:P2D
RRULE:FREQ=DAILY
UID:broken@google.com
SUMMARY:Out of range"""
    found, report = records(calendar(HOLIDAY, broken))
    assert list(found) == ["sam@example.com/sam@example.com/xmas2024"]
    assert report.unreadable == 1 and "couldn't be read" in report.notes()[-1]


def test_an_all_day_series_a_rule_without_an_end_and_an_occurrence_alone():
    birthdays = """DTSTART;VALUE=DATE:20200314
DTEND;VALUE=DATE:20200315
RRULE:FREQ=YEARLY
UID:pi-day@google.com
SUMMARY:Pi day"""
    gym = """DTSTART:20250106T120000Z
RRULE:FREQ=WEEKLY;COUNT=3
UID:gym@google.com
SUMMARY:Gym"""
    # Invited to one occurrence of someone else's series: the export holds only that occurrence.
    alone = """DTSTART:20250310T150000Z
DTEND:20250310T160000Z
RECURRENCE-ID:20250310T150000Z
UID:standup@google.com
SUMMARY:Their standup"""
    found, _ = records(calendar(birthdays, gym, alone))
    days = sorted(k.rsplit("_", 1)[1] for k in found if "/pi-day_" in k)
    # Yearly with no end: through the export (Nov 2025), so 2020 to 2025.
    assert days == [f"{y}0314" for y in range(2020, 2026)]
    assert found["sam@example.com/sam@example.com/pi-day_20230314"].occurred_at == datetime(2023, 3, 14, tzinfo=UTC)
    assert sorted(k for k in found if "/gym_" in k) == [f"sam@example.com/sam@example.com/gym_202501{d}T120000Z"
                                                        for d in ("06", "13", "20")]
    lone = found["sam@example.com/sam@example.com/standup_20250310T150000Z"].raw_meta
    assert lone["recurring"] is True and lone["series_key"] == "sam@example.com/sam@example.com/standup"


def test_what_the_export_does_not_name_is_keyed_by_the_export_and_said():
    work = """DTSTART:20250310T150000Z
ORGANIZER;CN=Work:mailto:abc123xyz@group.calendar.google.com
UID:review@google.com
SUMMARY:Review"""
    outlook = """DTSTART:20250311T150000Z
ORGANIZER;CN=Kim Ito:mailto:kim@example.org
UID:040000008200E00074C5B7101A82E008000000001
SUMMARY:Vendor call"""
    # A secondary calendar is the organizer of its own events: its id is known, the account isn't (no calendar named
    # by a person's address, no Chat export), and the note says that is why.
    found, report = records(calendar(work, outlook, name="Work"), file="Work")
    assert sorted(found) == ["takeout/Work/040000008200E00074C5B7101A82E008000000001", "takeout/Work/review"]
    assert found["takeout/Work/review"].raw_meta["calendar_id"] == "abc123xyz@group.calendar.google.com"
    assert (report.no_account, report.no_id) == ({"Work": 2}, {})
    assert report.notes()[0].startswith("The export doesn't say whose calendars these are") and "Work (2)" in report.notes()[0]
    # The account known, a calendar that names no address of its own: its id is what's missing.
    trips = """DTSTART:20250312T150000Z
ORGANIZER;CN=Kim Ito:mailto:kim@example.org
UID:trip@google.com
SUMMARY:Flight"""
    found, report = records(calendar(trips, name="Trips"), file="Trips", chat_user="sam@example.com")
    assert list(found) == ["takeout/Trips/trip"] and report.no_id == {"Trips": 1}
    assert report.notes() == ["The export doesn't name the address of the calendar Trips, so its events (1) are keyed "
                              "by the export: syncing Google Calendar later stores its own record of each (still one "
                              "event in the graph)."]
    # With the account known (Chat's export names you), the secondary calendar is keyed as the API keys it; the event
    # from another calendar program keeps its own UID, and is said.
    found, report = records(calendar(work, outlook, name="Work"), file="Work", chat_user="sam@example.com")
    assert sorted(found) == ["sam@example.com/abc123xyz@group.calendar.google.com/040000008200E00074C5B7101A82E008000000001",
                             "sam@example.com/abc123xyz@group.calendar.google.com/review"]
    assert report.foreign == 1 and report.notes() == [
        "Events from other calendar programs carry no Google event id (1): those a Google Calendar sync lists later "
        "are stored under its own ids, in place of the export's."]
    for item in found.values():
        assert item.raw_meta["event_key"].startswith("gcal:") and item.raw_meta["account"] == "sam@example.com"


def test_content_lines_unfold_unescape_and_read_vcard_21():
    lines = contentlines.unfold(b"\xef\xbb\xbfSUMMARY:a long\r\n  title\r\nNOTE;ENCODING=QUOTED-PRINTABLE:caf=C3=\r\n=A9\r\n")
    assert lines == ["SUMMARY:a long title", "NOTE;ENCODING=QUOTED-PRINTABLE:caf=C3=A9"]
    (card,) = contentlines.parse("BEGIN:VCARD\nitem1.TEL;CELL;PREF:555\nN:Lee\\, Jr.;Jordan;;;\n"
                                 "NOTE;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:caf=C3=A9\nEND:VCARD\n")
    tel = card.first("TEL")
    assert (tel.group, tel.types(), tel.value) == ("item1", {"cell", "pref"}, "555")
    assert contentlines.split(card.first("N").value) == ["Lee, Jr.", "Jordan", "", "", ""]
    assert card.text("NOTE") == "café"
    assert contentlines.parse_line('ATTENDEE;CN="Lee, Jordan":mailto:j@x.com').param("CN") == "Lee, Jordan"
    # vCard 2.1's bare QUOTED-PRINTABLE is an encoding, not a type; a CHARSET nobody knows reads as UTF-8.
    bare = contentlines.parse_line("N;CHARSET=UTF-8;QUOTED-PRINTABLE:Caf=C3=A9;Jos=C3=A9")
    assert (bare.value, bare.types()) == ("Café;José", set())
    tel = contentlines.parse_line("TEL;CELL;QUOTED-PRINTABLE:555=2D0100")
    assert (tel.value, tel.types()) == ("555-0100", {"cell"})
    assert contentlines.parse_line("NOTE;ENCODING=QUOTED-PRINTABLE;CHARSET=X-NOPE:caf=C3=A9").value == "café"


def test_a_calendar_sync_later_updates_the_exported_events_instead_of_adding_them(db):
    data = calendar(WEEKLY, HOLIDAY)
    cal = ical.read(data, "sam@example.com")

    class Export:
        name = "takeout"

        def sync(self, cursor):
            for listing in ical.listings([cal], ical.account([cal])):
                yield from listing.items

    first = run_sync(db, Export())
    assert run_sync(db, Export()) == {**first, "inserted": 0, "unchanged": first["inserted"]}  # the same file again
    # The same account's calendar through the Calendar API: each occurrence under the API's own id.
    api_events = [{"id": f"abc123_{day}", "recurringEventId": "abc123", "iCalUID": "abc123@google.com",
                   "summary": "CS 395 Senior project", "start": {"dateTime": start, "timeZone": "America/New_York"},
                   "end": {"dateTime": start, "timeZone": "America/New_York"}, "organizer": {"email": "sam@example.com"}}
                  for day, start in (("20241028T140000Z", "2024-10-28T10:00:00-04:00"),
                                     ("20241104T150000Z", "2024-11-04T10:00:00-05:00"))]
    api_events.append({"id": "xmas2024", "iCalUID": "xmas2024@google.com", "summary": "Holiday at home",
                       "start": {"date": "2024-12-25"}, "end": {"date": "2024-12-26"}})
    service = FakeCalendar([{"id": "sam@example.com", "summary": "sam@example.com"}],
                           [{"items": api_events, "nextSyncToken": "t1"}])
    stats = run_sync(db, CalendarConnector(None, "sam@example.com", service=service), account="sam@example.com")
    assert (stats["inserted"], stats["updated"]) == (0, 3)
    assert db.execute(text("SELECT count(*) FROM source_items WHERE source = 'gcal'")).scalar() == first["inserted"]
    keys = db.execute(text("SELECT DISTINCT raw_meta->>'event_key' FROM source_items WHERE source = 'gcal'")).scalars()
    assert set(keys) == {"gcal:abc123@google.com", "gcal:xmas2024@google.com"}  # one Event per series, either way
