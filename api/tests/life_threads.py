"""A synthetic life, written the way the connectors write records, with its gold threads and a stubbed model.

One student over a year: a summer internship at Acme Robotics (and the Atlas Platform project there, which their
LinkedIn lists), two trips (Portland in August, Lisbon in December), two classes at State University (CS 401 in the
fall, EE 221 in the spring) and a kiosk prototype built for CS 401. Each lives in mail, calendar, group chats and
documents at once, among hundreds of unrelated everyday messages, as a real mailbox would hold them. Every name,
address and place is invented.

`build(s)` writes the records; `GOLD` is what the threads pipeline should find (the format of eval/threads_eval.py);
`model` answers every hosted call the pipeline makes, deterministically and from the prompt alone, the way a careful
model would. The `life` fixture is a test database holding it, with that model in place of the hosted one.
"""
import re
from datetime import UTC, datetime, timedelta

import pytest
from pipeline_data import put
from sqlalchemy import text

ME = "mo@self.test"
NAMES = {ME: "Mo Self", "ana@pals.test": "Ana Ames", "bo@pals.test": "Bo Berg", "cy@pals.test": "Cy Cole",
         "di@pals.test": "Di Diaz", "prof.kim@state.edu": "Ida Kim", "ta.lee@state.edu": "Ray Lee",
         "prof.moss@state.edu": "Ola Moss", "jo@acmerobotics.com": "Jo Park", "eli@acmerobotics.com": "Eli Ross",
         "fay@pals.test": "Fay Gold"}
PHONES = {"+15550000001": "ana@pals.test", "+15550000002": "bo@pals.test", "+15550000003": "cy@pals.test",
          "+15550000004": "di@pals.test"}
T0 = datetime(2025, 6, 2, 15, tzinfo=UTC)


def at(days: float, hour: int = 0) -> datetime:
    return T0 + timedelta(days=days, hours=hour)


def _mail(s, ref: str, sender: str, to: list[str], subject: str, when: datetime, body: str, thread: str = "") -> None:
    put(s, "gmail", ref, subject, {
        "from": [{"email": sender, "name": NAMES.get(sender, "")}],
        "to": [{"email": t, "name": NAMES.get(t, "")} for t in to], "fingerprint": f"fp-{ref}",
        "gmail_thread_id": thread or f"t-{ref}", "labels": ["SENT"] if sender == ME else ["INBOX"]}, when, body)


def _event(s, ref: str, title: str, when: datetime, attendees: list[str], series: str | None = None) -> None:
    put(s, "gcal", ref, title, {
        "attendees": [{"email": a, "name": NAMES.get(a, ""), "response": "accepted"} for a in attendees],
        "organizer": {"email": ME, "name": NAMES[ME]}, "recurring": bool(series),
        **({"series_key": series, "event_key": f"gcal:{series}"} if series else {})}, when, f"{title}.")


def _chat_day(s, chat: str, name: str, phones: list[str], when: datetime, body: str) -> None:
    put(s, "imessage", f"{chat}/{when:%Y-%m-%d}", f"{name} — {when:%Y-%m-%d}", {
        "chat": chat, "group": True, "participants": phones, "senders": [*phones[:1], "me"]}, when, body)


def _doc(s, source: str, path: str, title: str, when: datetime, body: str) -> None:
    put(s, source, path, title, {"path": path, **({"drive_id": path} if source == "drive" else {})}, when, body)


def _github(s, ref: str, title: str, when: datetime, body: str) -> None:
    put(s, "github", f"atlas-platform/{ref}", title, {
        "repo": "atlas-platform", "kind": "issue", "number": ref, "author": {"login": "moself"},
        "comments": [{"login": "jopark"}]}, when, body)


def _linkedin(s) -> None:
    profile = {"is_about_owner": True, "person_name": "Mo Self",
               "experience": [{"org": "Acme Robotics", "title": "Software Intern", "start": "2025-06", "end": "2025-08",
                               "domain_hint": "", "parent": "", "project": ""}],
               "education": [{"school": "State University", "degree": "BS Computer Science", "start": "2023-09",
                              "end": "2027-05", "domain_hint": ""}],
               "projects": [{"name": "Atlas Platform", "description": "fleet dashboard", "org": "Acme Robotics"}]}
    put(s, "linkedin", "profile", "LinkedIn profile: Mo Self", {"kind": "profile", "person_name": "Mo Self",
                                                                "profile": profile}, None, "LinkedIn profile of Mo Self")


def _everyday(s) -> None:
    """A year of unrelated everyday mail, lowercase, from many people: the background every term is measured against."""
    friends = [f"friend{i}@pals.test" for i in range(40)]
    for i in range(760):
        who = friends[i % len(friends)]
        when = at(i * 0.47, hour=i % 9)
        if i % 3:
            _mail(s, f"day{i}", who, [ME], f"quick question {i}", when, f"hey, are you free later? about item {i}.")
        else:
            _mail(s, f"day{i}", ME, [who], f"re: quick question {i}", when, f"sure, around six works. ({i})")


def _internship(s) -> None:
    """June to August at Acme Robotics: mail with the team, a daily standup, and the Atlas Platform repo."""
    team = ["jo@acmerobotics.com", "eli@acmerobotics.com"]
    for i in range(10):
        _mail(s, f"acme{i}", team[i % 2], [ME], f"Acme Robotics onboarding step {i}", at(2 + 7 * i),
              f"welcome to Acme Robotics. step {i}: set up your laptop and badge.")
    for i in range(8):
        _event(s, f"cal/acme-standup/{i}", "Team standup", at(3 + 9 * i, hour=1), team, series="acme-standup")
    for i in range(7):
        _github(s, str(100 + i), f"atlas-platform: dashboard bug {i}", at(10 + 8 * i),
                f"the Atlas Platform dashboard crashes on load ({i}).")
        _mail(s, f"atlas{i}", "jo@acmerobotics.com", [ME], f"Atlas Platform review {i}", at(12 + 8 * i),
              f"notes on the Atlas Platform fleet view, round {i}.")
    _doc(s, "drive", "drive/atlas-spec", "Atlas Platform spec", at(15), "Atlas Platform: what the fleet dashboard shows.")


def _trip(s, place: str, start: float, crew: list[str], chat: str, chat_name: str, doc: str) -> None:
    """A trip: flight and hotel mail, an itinerary document, calendar holds and the friends' group chat."""
    phones = [p for p, e in PHONES.items() if e in crew]
    _mail(s, f"{place}-flight", "bookings@flyaway.test", [ME], f"Your flight to {place} is confirmed", at(start),
          f"flight to {place} confirmed for you and {len(crew)} guests.")
    _mail(s, f"{place}-hotel", ME, crew, f"{place} hotel options", at(start + 2),
          f"found three hotels in {place}, which one?")
    for i, who in enumerate(crew):
        _mail(s, f"{place}-plan{i}", who, [ME, *[c for c in crew if c != who]], f"Re: {place} plans", at(start + 4 + i),
              f"{place} sounds great, i booked my seat.")
    _doc(s, "drive", f"drive/{place.lower()}-itinerary", f"{place} itinerary", at(start + 6),
         f"day by day in {place}: museums, food, the river walk.")
    _event(s, f"cal/{place.lower()}-flight", f"Flight to {place}", at(start + 20), crew)
    _event(s, f"cal/{place.lower()}-dinner", f"{place} dinner", at(start + 22, hour=5), crew)
    for d in range(6):
        _chat_day(s, chat, chat_name, phones, at(start + 8 + 2 * d), f"countdown to {place}: {6 - d} more days to pack")


def _class(s, code: str, start: float, prof: str, ta: str, extra: str) -> None:
    """A semester-long class: the professor's mail, the weekly lecture series, homework documents."""
    for i in range(9):
        _mail(s, f"{code}-hw{i}", prof, [ME, ta], f"{code} homework {i + 1}", at(start + 10 * i),
              f"{code} homework {i + 1} is posted, due friday. {extra}")
    for i in range(12):
        _event(s, f"cal/{code}-lecture/{i}", code, at(start + 1 + 7 * i, hour=2), [prof, ta], series=f"{code}-lecture")
    for i in range(4):
        _doc(s, "fs", f"school/{code.lower().replace(' ', '')}/notes{i}.md", f"{code} notes week {i + 1}",
             at(start + 3 + 14 * i), f"{code} lecture notes, week {i + 1}.")


def _kiosk(s) -> None:
    """A prototype built for CS 401: its own mail, docs and a chat, a few of which name the class."""
    crew = ["di@pals.test"]
    for i in range(5):
        _mail(s, f"kiosk{i}", "di@pals.test", [ME], f"Kiosk Prototype sprint {i}", at(132 + 3 * i),
              f"the Kiosk Prototype touch screen works now ({i})." + (" demo for CS 401 on friday." if i < 2 else ""))
    _doc(s, "drive", "drive/kiosk-design", "Kiosk Prototype design", at(134), "Kiosk Prototype: screens and flows.")
    _event(s, "cal/kiosk-demo", "Kiosk Prototype demo", at(146), crew)
    for d in range(4):
        _chat_day(s, "chat-kiosk", "Build buddies", ["+15550000004"], at(133 + 3 * d),
                  f"Kiosk Prototype status, day {d}: wiring the card reader")


def build(s) -> None:
    """Every record of the synthetic life, committed."""
    for phone, email in PHONES.items():
        put(s, "gcontacts", f"acct/people/{phone}", NAMES[email], {"emails": [email], "phones": [phone],
                                                                   "orgs": [], "account": "acct"})
    _linkedin(s)
    _everyday(s)
    _internship(s)
    _trip(s, "Portland", 60, ["ana@pals.test", "bo@pals.test"], "chat-road", "Road crew", "portland")
    _trip(s, "Lisbon", 185, ["cy@pals.test", "fay@pals.test"], "chat-wander", "Wanderers", "lisbon")
    _class(s, "CS 401", 92, "prof.kim@state.edu", "ta.lee@state.edu", "office hours are in the lab.")
    _class(s, "EE 221", 215, "prof.moss@state.edu", "ta.lee@state.edu", "bring a calculator.")
    _kiosk(s)
    s.commit()


GOLD = {"threads": [
    {"name": "Acme Robotics", "kind": "job", "terms": ["Acme Robotics"]},
    {"name": "Atlas Platform", "kind": "project", "terms": ["Atlas Platform"], "parent": "Acme Robotics"},
    {"name": "Portland trip", "kind": "trip", "terms": ["Portland"], "participants": ["Ana", "Bo"]},
    {"name": "Lisbon trip", "kind": "trip", "terms": ["Lisbon"], "participants": ["Cy", "Fay"]},
    {"name": "State University", "kind": "school", "terms": ["State University"]},
    {"name": "CS 401", "kind": "class", "terms": ["CS 401"], "parent": "State University"},
    {"name": "EE 221", "kind": "class", "terms": ["EE 221"], "parent": "State University"},
    {"name": "Kiosk Prototype", "kind": "project", "terms": ["Kiosk Prototype"], "parent": "CS 401"},
]}


def _judge_thread(user: str) -> dict:
    """keep / kind / name / defining terms / members for a thread candidate, from its shared terms and records."""
    terms = [t for t in user.split("\n", 1)[0].removeprefix("Shared terms: ").split(", ") if t]
    records = re.findall(r"^(\d+)\. \[(\w+), [\d-]+\] (.*)$", user, re.M)
    term = terms[0] if terms else ""
    text = " ".join(line for _i, _src, line in records).lower()
    if re.fullmatch(r"[A-Z]{2,5} \d{3}", term):
        kind, name = "class", term
    elif "flight" in text or "itinerary" in text:
        kind, name = "trip", f"{term} trip"
    elif "onboarding" in text or "standup" in text:
        kind, name = "job", term
    else:
        kind, name = "project", term.title() if term.islower() else term
    members = [int(i) for i, _src, line in records if term and term.lower() in line.lower()]
    return {"keep": bool(term), "kind": kind, "name": name, "confidence": 0.9, "defining_terms": [term] if term else [],
            "members": members}


def _cards(user: str) -> list[tuple[str, str, str]]:
    """(name, kind, records line) of each thread in a pair question."""
    return re.findall(r"'(.*?)' \((.*?), .*?\n.*?\n\s+records: (.*)", user)


def _relate(user: str) -> dict:
    """same (one thing found twice), part_of (a project whose records name a class, job or org it was done for) or
    distinct."""
    (a, a_kind, a_records), (b, b_kind, b_records) = _cards(user)
    out = {"label": "", "confidence": 0.9, "reason": "", "shared_identity": "", "container": ""}
    if a.lower() == b.lower() or a.lower().replace("-", " ") == b.lower().replace("-", " "):
        return {**out, "relation": "same", "shared_identity": a, "reason": f"both are {a}"}
    larger = ("class", "job", "organization")
    named = b.lower() in a_records.lower() or a.lower() in b_records.lower()
    for small_kind, big, big_kind in ((a_kind, b, b_kind), (b_kind, a, a_kind)):
        if small_kind == "project" and big_kind in larger and named:
            return {**out, "relation": "part_of", "container": big, "label": f"done for {big}"}
    return {**out, "relation": "related" if named else "distinct", "label": "named together"}


def model(system: str, user: str, schema: dict, **_options):
    """Every hosted call the pipeline makes, answered from the prompt alone."""
    props = schema.get("properties", {})
    if "edges" in props:  # the verifier: what the records show is kept
        n = len(re.findall(r"^### Edge \d+$", user, re.M))
        return {"edges": [{"edge": i, "verdict": "keep", "confidence": 0.9, "reason": "shown in the records"}
                          for i in range(n)]}, {}
    if "defining_terms" in props:
        return _judge_thread(user), {}
    if "relation" in props:
        return _relate(user), {}
    if "umbrella" in props:
        return {"umbrella": False, "confidence": 0.9}, {}
    if "keep" in props:  # entity screening: every name is a specific thing
        return {"keep": [line[2:] for line in user.splitlines() if line.startswith("- ")]}, {}
    return {k: {"array": [], "boolean": False, "number": 0, "integer": 0}.get(v.get("type"), "")
            for k, v in props.items()}, {}


def arrived_later(s) -> None:
    """Records a test added after a run, as a later sync's would be: newer than what that run read. (A test's
    database is one transaction, where now() stands still, and the spine reads only what was updated after it last
    ran.)"""
    s.execute(text("""UPDATE source_items SET updated_at = clock_timestamp()
                      WHERE updated_at >= (SELECT max(watermark) FROM pipeline_state)"""))
    s.commit()


SOURCES = "gmail,gcal,imessage,drive,fs,github"  # every source the life is in, opted in


@pytest.fixture
def life(db, local_models, monkeypatch):
    """The synthetic life in the test database, every source opted in, `model` answering hosted calls."""
    from igneous import llm

    monkeypatch.setattr(llm, "structured", model)
    monkeypatch.setattr(llm.settings, "llm_sources", SOURCES)
    monkeypatch.setattr(llm.settings, "local_sources_model_enabled", True)
    build(db)
    return db


@pytest.fixture
def trips(db, local_models, monkeypatch):
    """A smaller life: a year of everyday mail and the two trips, every source opted in, `model` answering."""
    from igneous import llm

    monkeypatch.setattr(llm, "structured", model)
    monkeypatch.setattr(llm.settings, "llm_sources", SOURCES)
    _everyday(db)
    _trip(db, "Portland", 60, ["ana@pals.test", "bo@pals.test"], "chat-road", "Road crew", "portland")
    _trip(db, "Lisbon", 185, ["cy@pals.test", "fay@pals.test"], "chat-wander", "Wanderers", "lisbon")
    db.commit()
    return db
