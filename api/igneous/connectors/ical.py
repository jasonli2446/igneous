"""Google Calendar's iCalendar export (Google Takeout's Calendar/*.ics, or a calendar file given on its own) read as
Google Calendar events.

Every occurrence of an event is one record, keyed and shaped as connectors/gcal.py stores it from the Calendar API:
source `gcal`, external id `<account>/<calendar id>/<event id>`, where an occurrence of a repeating event is
`<event id>_<its original start>` (UTC; a date for an all-day event), as the API's single-event ids are. So connecting
the same Google account later and syncing Google Calendar updates these records instead of storing every event again,
and in the graph a repeating event is one Event (its `event_key`: "gcal:" and the iCalendar UID, which the API calls
iCalUID), its occurrences the records behind it. Each record says it came from the export (`product`: Calendar).

An export is each calendar as it was when it was exported (Google stamps every event with that time): a repeating
event's occurrences are the ones up to then, not a forecast of the ones still to come. Each calendar is one `Listing`:
all its records, under the external-id prefix they share (its scope), and when it was exported, so the importer can
tell which stored records a newer export no longer holds (connectors/takeout.py).

What the export doesn't name, and how such an event is keyed instead:
- The account and a calendar's id. Takeout names each file after its calendar (X-WR-CALNAME too), and an account's
  own calendar is named after the account's address, its id. Another calendar's id is the address Google gives that
  calendar's own events as their organizer (ORGANIZER;CN=<the calendar's name>). The account is the address Takeout's
  Chat export names as you, else the one calendar whose id is a person's address.
- Google's event id. A Google event's UID is `<event id>@google.com`; an event that came from another calendar program
  keeps that program's UID, and Google's id for it isn't in the export.
An event whose account, calendar id or event id isn't known is keyed `takeout/<calendar name>/<UID>` (or
`<account>/<calendar id>/<UID>`), and the import's notes say which calendars and how many events.

Repeating events are expanded as the API expands them: RRULE, RDATE and EXDATE, and the occurrences moved, changed or
cancelled one by one (a VEVENT with RECURRENCE-ID). Times are UTC, a TZID (an IANA zone, as Google writes them) or
floating times in the calendar's X-WR-TIMEZONE; all-day events are dates. An event whose dates or rule can't be read is
left out and counted, never the end of the import.
"""
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rruleset, rrulestr

from igneous.connectors import contentlines
from igneous.connectors.base import SourceItem
from igneous.connectors.contentlines import Component, Prop, address

MAX_OCCURRENCES = 3660       # per event, the latest kept: ten years of a daily reminder
GOOGLE_UID = "@google.com"
# Calendars that belong to no account: groups, imported and subscribed calendars (holidays, birthdays).
SHARED_CALENDAR = re.compile(r"@(group|import|group\.v)\.calendar\.google\.com$")
# The block Google adds to a description with an event's Meet details ("Please do not edit this section"), which the
# Calendar API keeps out of the description.
MEET_BLOCK = re.compile(r"-::~:~::~[:~]*::-.*?-::~:~::~[:~]*::-", re.S)
RESPONSE = {"ACCEPTED": "accepted", "DECLINED": "declined", "TENTATIVE": "tentative", "NEEDS-ACTION": "needsAction",
            "DELEGATED": "needsAction"}
# What reading one event can raise on input nobody foresaw (a year out of range, a rule dateutil rejects late): the
# event is left out and counted.
UNREADABLE = (ValueError, TypeError, KeyError, OverflowError)


@dataclass
class Calendar:
    """One calendar file: its name, its id when the export names it, its zone, and its VEVENTs."""
    name: str
    id: str | None
    zone: str | None
    events: list[Component] = field(default_factory=list)
    exported: datetime | None = None  # when it was exported (Google stamps every event then)


@dataclass
class Listing:
    """One calendar's records as of its export: every record's external id is `<scope>/<event or occurrence id>`."""
    name: str
    scope: str
    exported: datetime
    items: list[SourceItem]


def _zone(tzid: str | None) -> ZoneInfo | None:
    """An IANA zone by its id ('America/New_York', or a vendor-prefixed '/mozilla.org/.../America/New_York')."""
    tail = re.search(r"[A-Za-z_]+/[A-Za-z_+-]+$", tzid or "")
    for name in (tzid, tail.group(0) if tail else None):
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return None


def _moments(prop: Prop, default_zone: str | None) -> list[tuple[datetime, bool]]:
    """Each (moment, all day) a DTSTART, DTEND, RECURRENCE-ID, RDATE or EXDATE value gives: a timed one aware (UTC, its
    TZID, or the calendar's zone for a floating time), an all-day one a naive midnight. Values that aren't dates are
    skipped (RDATE periods)."""
    out = []
    for value in prop.value.split(","):
        value = value.strip()
        try:
            if prop.param("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
                out.append((datetime.strptime(value[:8], "%Y%m%d"), True))
            elif m := re.fullmatch(r"(\d{8}T\d{6})(Z?)", value):
                naive = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
                zone = UTC if m.group(2) else _zone(prop.param("TZID")) or _zone(default_zone) or UTC
                out.append((naive.replace(tzinfo=zone), False))
        except ValueError:
            continue
    return out


def _utc(m: datetime) -> datetime | None:
    """The moment in UTC (a naive one read as UTC), or None when it lies outside the years Python can hold."""
    try:
        return m.astimezone(UTC) if m.tzinfo else m.replace(tzinfo=UTC)
    except OverflowError:
        return None


def _moment(prop: Prop | None, default_zone: str | None) -> tuple[datetime, bool] | None:
    found = _moments(prop, default_zone) if prop is not None else []
    return found[0] if found else None


def _like(when: datetime, start: datetime) -> datetime:
    """`when` comparable with `start`: aware when it is (a floating time read in its zone), naive when it's a date."""
    if start.tzinfo is None:
        return when.astimezone(UTC).replace(tzinfo=None) if when.tzinfo else when
    return when if when.tzinfo else when.replace(tzinfo=start.tzinfo)


def _until(value: str, start: datetime) -> datetime | None:
    """An RRULE's UNTIL, comparable with `start`: a date is through the end of that day, a UTC time is that moment, and
    a floating time is in the start's zone (RFC 5545 writes UNTIL floating only when DTSTART is). None if unreadable."""
    value = value.strip()
    try:
        if re.fullmatch(r"\d{8}", value):
            end = datetime.strptime(value, "%Y%m%d") + timedelta(days=1, microseconds=-1)
        elif m := re.fullmatch(r"(\d{8}T\d{6})(Z?)", value):
            end = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
            if m.group(2):
                return _like(end.replace(tzinfo=UTC), start)
        else:
            return None
    except ValueError:
        return None
    return end if start.tzinfo is None else end.replace(tzinfo=start.tzinfo)


def _rule(text: str, start: datetime):
    """An RRULE starting at `start`. UNTIL is read here (`_until`), because dateutil only takes one as aware as
    DTSTART; with COUNT it is ignored (RFC 5545 allows only one of them)."""
    parts = [p for p in text.strip().removeprefix("RRULE:").split(";") if p]
    until = next((p.split("=", 1)[1] for p in parts if p.upper().startswith("UNTIL=")), None)
    rule = rrulestr(";".join(p for p in parts if not p.upper().startswith("UNTIL=")), dtstart=start)
    if until and not any(p.upper().startswith("COUNT=") for p in parts) and (end := _until(until, start)):
        rule = rule.replace(until=end)
    return rule


def _original(when: datetime, all_day: bool) -> date | datetime:
    """How an occurrence is told apart, and how a changed occurrence names the one it replaces: the UTC moment, or the
    date."""
    return when.date() if all_day else when.astimezone(UTC)


def _suffix(when: datetime, all_day: bool) -> str:
    return when.strftime("%Y%m%d") if all_day else when.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def read(data: bytes | str, name: str) -> Calendar | None:
    """One .ics file, or None if it holds no calendar. `name` is the file's: Takeout names it after the calendar,
    Google Calendar's own export after the calendar's id."""
    cal = next((c for c in contentlines.parse(data) if c.name == "VCALENDAR"), None)
    if cal is None:
        return None
    title = cal.text("X-WR-CALNAME") or name
    events = [c for c in cal.children if c.name == "VEVENT" and c.text("UID")]
    # When it was exported: Google stamps every event with the export's time; another program's file may only say
    # when its events were last changed, or when they start.
    found: list[datetime] = []
    for stamp in (("DTSTAMP",), ("LAST-MODIFIED", "CREATED"), ("DTSTART",)):
        found = [t for e in events for p in e.props if p.name in stamp for m, _ in _moments(p, None)
                 if (t := _utc(m)) is not None]
        if found:
            break
    return Calendar(title, calendar_id([title, name], events), cal.text("X-WR-TIMEZONE") or None, events,
                    max(found, default=None))


def calendar_id(names: list[str], events: list[Component]) -> str | None:
    """The calendar's id, when the export names it: an account's own calendar is named by its address (and Google
    Calendar's export names every file by its calendar's id), and another calendar is the organizer of its own events
    (named like the calendar)."""
    for name in names:
        if found := address(name):
            return found
    for e in events:
        org = e.first("ORGANIZER")
        if org is not None and (org.param("CN") or "").strip() == names[0].strip() and (found := address(org.value)):
            return found
    return None


def account(calendars: list[Calendar], chat_user: str | None = None) -> str | None:
    """Whose export it is: the address Takeout's Chat export names as you, else the one calendar whose id is a
    person's address (the account's own calendar; the others are groups, imports and subscriptions)."""
    own = {c.id for c in calendars if c.id and not SHARED_CALENDAR.search(c.id)}
    return chat_user or (own.pop() if len(own) == 1 else None)


@dataclass
class Report:
    events: int = 0          # events read (a repeating one is one)
    occurrences: int = 0     # records made
    no_account: dict[str, int] = field(default_factory=dict)  # calendar -> its events, keyed by the export: no account
    no_id: dict[str, int] = field(default_factory=dict)  # calendar -> its events, keyed by the export: no calendar id
    foreign: int = 0         # events from other calendar programs (no Google event id), on calendars keyed by Google
    cut: list[str] = field(default_factory=list)  # repeating events with more than MAX_OCCURRENCES
    unreadable: int = 0      # events left out: their dates or rule couldn't be read

    def notes(self) -> list[str]:
        later = "syncing Google Calendar later stores its own record of each (still one event in the graph)."
        out = []
        if self.no_account:
            out.append(f"The export doesn't say whose calendars these are (Google Chat's export would name you, as would "
                       f"a calendar named by your address), so the events of {', '.join(sorted(self.no_account))} "
                       f"({sum(self.no_account.values())}) are keyed by the export: {later}")
        out += [f"The export doesn't name the address of the calendar {name}, so its events ({n}) are keyed by the "
                f"export: {later}" for name, n in sorted(self.no_id.items())]
        if self.foreign:
            out.append(f"Events from other calendar programs carry no Google event id ({self.foreign}): those a Google "
                       "Calendar sync lists later are stored under its own ids, in place of the export's.")
        out += [f"{title} repeats more than {MAX_OCCURRENCES:,} times; its latest {MAX_OCCURRENCES:,} are kept."
                for title in self.cut]
        if self.unreadable:
            out.append(f"Events whose dates or repeat rule couldn't be read were left out ({self.unreadable}).")
        return out


def listings(calendars: list[Calendar], owner: str | None, report: Report | None = None) -> list[Listing]:
    """Each calendar's records: every occurrence of every event up to the calendar's export, as Google Calendar
    records (gcal), in time order. A calendar without a single event has no listing: nothing says when it was
    exported."""
    report = report if report is not None else Report()
    out = []
    for cal in calendars:
        if cal.exported is None:
            continue
        keyed = bool(owner and cal.id)
        scope = f"{owner}/{cal.id}" if keyed else f"takeout/{cal.name}"
        masters: dict[str, Component] = {}
        changed: dict[str, dict] = {}  # UID -> an occurrence's original start -> the VEVENT that replaces it
        for e in cal.events:
            uid = e.text("UID")
            try:
                rid = _moment(e.first("RECURRENCE-ID"), cal.zone)
                original = _original(*rid) if rid else None
            except UNREADABLE:
                report.unreadable += 1
                continue
            if original is not None:
                changed.setdefault(uid, {})[original] = e
            else:
                masters.setdefault(uid, e)
        rows: list[tuple[datetime, str, SourceItem]] = []
        for uid in sorted(set(masters) | set(changed)):
            google = uid.lower().endswith(GOOGLE_UID)
            event = _Event(cal, uid, f"{scope}/{uid[:-len(GOOGLE_UID)] if google else uid}", scope, owner)
            try:
                found, cut = event.records(masters.get(uid), changed.get(uid, {}), cal.exported)
            except UNREADABLE:
                report.unreadable += 1
                continue
            report.events += 1
            if cut:
                report.cut.append(event.title(masters.get(uid)))
            if not keyed:
                unkeyed = report.no_id if owner else report.no_account
                unkeyed[cal.name] = unkeyed.get(cal.name, 0) + 1
            elif not google:
                report.foreign += 1
            rows += found
        items = [item for _start, _id, item in sorted(rows, key=lambda r: (r[0], r[1]))]
        report.occurrences += len(items)
        out.append(Listing(cal.name, scope, cal.exported, items))
    return out


@dataclass
class _Event:
    """One event of a calendar (all its occurrences), and how its records are keyed: `key` is the record of a single
    event and the stem of each occurrence's (`<key>_<original start>`)."""
    cal: Calendar
    uid: str
    key: str
    scope: str
    owner: str | None

    def title(self, master: Component | None) -> str:
        return (master.text("SUMMARY") if master is not None else "") or "(no title)"

    def records(self, master: Component | None, changed: dict,
                horizon: datetime) -> tuple[list[tuple[datetime, str, SourceItem]], bool]:
        """(when it starts, external id, record) for a single event, whenever it is, or for each occurrence of a
        repeating one up to `horizon` (the export); and whether it repeats more than MAX_OCCURRENCES times by then."""
        zone = self.cal.zone
        if master is not None and master.text("STATUS").upper() == "CANCELLED":
            return [], False
        found = _moment(master.first("DTSTART"), zone) if master is not None else None
        if master is not None and not (master.all("RRULE") or master.all("RDATE") or changed):
            if not found:
                return [], False
            start, all_day = found
            return [self._record(master, self.key, start, _end(master, start, all_day, zone), all_day, False)], False
        # As the API names a series: its event id (iCalUID only for a single event).
        series = self.key.removeprefix(self.scope + "/")
        out, cut = [], False
        if found:
            start, all_day = found
            length = _end(master, start, all_day, zone) - start
            occurrences, cut = _occurrences(master, start, all_day, zone, horizon)
            for when in occurrences:
                iid = f"{self.key}_{_suffix(when, all_day)}"
                replaced = changed.pop(_original(when, all_day), None)
                if replaced is None:
                    out.append(self._record(master, iid, when, when + length, all_day, True, series))
                elif moved := _changed(replaced, zone):
                    out.append(self._record(replaced, iid, *moved, True, series))
        # Occurrences changed on their own whose original start the rule doesn't give (the rule changed since, or the
        # export holds only the occurrence you were invited to), up to the export like the rest.
        for replaced in changed.values():
            rid = _moment(replaced.first("RECURRENCE-ID"), zone)
            if rid and rid[0] <= _like(horizon, rid[0]) and (moved := _changed(replaced, zone)):
                out.append(self._record(replaced, f"{self.key}_{_suffix(*rid)}", *moved, True, series))
        return out, cut

    def _record(self, e: Component, external_id: str, start: datetime, end: datetime, all_day: bool,
                recurring: bool, series: str | None = None) -> tuple[datetime, str, SourceItem]:
        attendees = []
        for p in e.all("ATTENDEE"):
            person = _person(p)
            if person["email"] and (p.param("CUTYPE") or "").upper() not in ("RESOURCE", "ROOM"):
                attendees.append({**person, "response": RESPONSE.get((p.param("PARTSTAT") or "").upper(),
                                                                     "needsAction")})
        org = e.first("ORGANIZER")
        # With no organizer, the event is the calendar's own, as the API reports it.
        organizer = _person(org) if org is not None else {
            "name": "" if address(self.cal.name) else self.cal.name, "email": self.cal.id or ""}
        description = MEET_BLOCK.sub("", e.text("DESCRIPTION")).strip()

        def when(t: datetime) -> dict:
            if all_day:
                return {"date": t.date().isoformat()}
            return {"dateTime": t.isoformat(), **({"timeZone": t.tzinfo.key} if isinstance(t.tzinfo, ZoneInfo) else {})}

        at = start.replace(tzinfo=UTC) if all_day else start.astimezone(UTC)
        item = SourceItem(
            source="gcal", external_id=external_id, title=e.text("SUMMARY") or "(no title)",
            body_text="\n".join(v for v in (description, e.text("LOCATION")) if v), occurred_at=at,
            mime="text/calendar",
            raw_meta={"calendar": self.cal.name, "calendar_id": self.cal.id, "attendees": attendees,
                      "organizer": organizer, "start": when(start), "end": when(end), "account": self.owner,
                      "series_key": f"{self.scope}/{series or self.uid}", "recurring": recurring,
                      "event_key": f"gcal:{self.uid}", "product": "Calendar"})
        return at, external_id, item


def _person(p: Prop) -> dict:
    email = address(p.value) or ""
    name = (p.param("CN") or "").strip()
    return {"name": "" if address(name) == email else name, "email": email}


def _end(e: Component, start: datetime, all_day: bool, zone: str | None) -> datetime:
    """DTEND, else DTSTART plus DURATION, else the start (a day later for an all-day event)."""
    found = _moment(e.first("DTEND"), zone)
    if found and found[1] == all_day:
        return found[0]
    if (d := _duration(e.text("DURATION"))) is not None:
        return start + d
    return start + timedelta(days=1) if all_day else start


def _duration(value: str) -> timedelta | None:
    """An RFC 5545 duration ('PT1H30M', 'P1D', 'P2W', '-PT15M'), or None."""
    m = re.fullmatch(r"([+-]?)P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not m or not any(m.groups()[1:]):
        return None
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    out = timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)
    return -out if m.group(1) == "-" else out


def _changed(e: Component, zone: str | None) -> tuple[datetime, datetime, bool] | None:
    """An occurrence changed on its own: (start, end, all day), or None when it was cancelled."""
    found = _moment(e.first("DTSTART"), zone)
    if e.text("STATUS").upper() == "CANCELLED" or not found:
        return None
    start, all_day = found
    return start, _end(e, start, all_day, zone), all_day


def _occurrences(master: Component, start: datetime, all_day: bool, zone: str | None,
                 horizon: datetime) -> tuple[list[datetime], bool]:
    """The original start of every occurrence up to `horizon` (the latest MAX_OCCURRENCES), and whether there were
    more. DTSTART is always one, as RFC 5545 says. A rule that can't be read is skipped: the event keeps its first
    occurrence and its RDATEs."""
    rset = rruleset()
    rset.rdate(start)
    for p in master.all("RRULE"):
        try:
            rset.rrule(_rule(p.value, start))
        except (ValueError, TypeError, KeyError):
            continue
    for p in master.all("RDATE"):
        for when, _ in _moments(p, zone):
            rset.rdate(_like(when, start))
    for p in master.all("EXDATE"):
        for when, _ in _moments(p, zone):
            rset.exdate(_like(when, start))
    limit = _like(horizon, start)
    kept: deque[datetime] = deque(maxlen=MAX_OCCURRENCES)
    seen = 0
    for when in rset:
        if when > limit:
            break
        kept.append(when)
        seen += 1
    return list(kept), seen > MAX_OCCURRENCES
