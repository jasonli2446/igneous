"""The persona's records as the storylines write them, before any export format: a mail, a chat line, an AI
conversation, a document, a task, a place visit, a search. `formats.py` turns them into the files each importer reads.

Every random choice comes from `Life.rng(stream)`, a generator seeded by the persona seed and the storyline's name,
so one storyline's choices never shift another's and the same seed gives the same records.
"""
import hashlib
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from igneous.persona.cast import ME, Person, at, local


@dataclass
class Mail:
    at: datetime
    sender: Person | tuple[str, str]          # a person, or (name, address) for an organization's sender
    to: list[Person | tuple[str, str]]
    subject: str
    body: str                                 # what it says, then the message it quotes, if any
    cc: list[Person | tuple[str, str]] = field(default_factory=list)
    text: str = ""                            # what it says, without the quote
    message_id: str = ""
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    folder: str = ""                          # INBOX, Sent, Archive, Travel; set when the maildir is written
    headers: dict[str, str] = field(default_factory=dict)
    calendar: str | None = None               # an invitation's text/calendar part
    tag: str | None = None                    # the gold set names this record by its tag


@dataclass(frozen=True)
class Space:
    key: str
    name: str             # "" for a direct message
    members: tuple[Person, ...]
    space_id: str         # Takeout's folder name: "Space AAAA..." or "DM ..."


@dataclass
class Line:
    at: datetime
    space: str
    sender: Person
    text: str
    tag: str | None = None


@dataclass
class Conversation:
    """A ChatGPT conversation: turns (when, role "user" | "assistant", text)."""
    title: str
    turns: list[tuple[datetime, str, str]]
    tag: str | None = None
    conversation_id: str = ""


@dataclass
class Doc:
    """A file in the documents folder. `body` is text for text formats; for docx, xlsx, pptx and pdf it is the
    structure formats.py lays out (paragraphs, sheets of rows, slides, pages)."""
    path: str
    modified: datetime
    body: object
    tag: str | None = None


@dataclass
class Task:
    title: str
    created: datetime
    due: date | None = None
    done: datetime | None = None
    notes: str = ""


@dataclass
class TaskList:
    title: str
    tasks: list[Task]
    tag: str | None = None


@dataclass
class Visit:
    place: str
    start: datetime
    end: datetime


@dataclass
class Search:
    """A My Activity entry: product (Search, Maps, YouTube), its title line, an optional URL and subtitle."""
    at: datetime
    product: str
    title: str
    url: str | None = None
    subtitle: str | None = None


@dataclass
class PageVisit:
    at: datetime
    title: str
    url: str


@dataclass
class Event:
    """A Google Calendar event, as Takeout's Calendar export writes it: one that repeats is one event with a rule."""
    uid: str
    summary: str
    start: datetime
    end: datetime
    location: str = ""
    description: str = ""
    attendees: tuple[Person, ...] = ()
    organizer: Person = ME
    rrule: str = ""
    zone: str = "home"
    tag: str | None = None  # the gold set names its (first) occurrence by this tag

    def occurrence_id(self, near: datetime) -> str:
        """Google Calendar's id for the event on the day of `near`: an occurrence of a repeating event is
        `<id>_<its start in UTC>`, a single event its own id."""
        if not self.rrule:
            return self.uid
        start = at(local(near, self.zone).date(), local(self.start, self.zone).strftime("%H:%M"), self.zone)
        return f"{self.uid}_{start:%Y%m%dT%H%M%SZ}"


@dataclass
class Meeting:
    """A Google Meet conference or direct call, as Takeout's Meet history lists it."""
    start: datetime
    minutes: int
    code: str = ""                     # a meeting's code; "" for a direct call
    event_id: str = ""                 # the calendar event it was joined from (its occurrence's, for a repeating one)
    counterparts: tuple[Person, ...] = ()


@dataclass
class Prompt:
    """A Gemini Apps prompt and its reply (Takeout My Activity)."""
    at: datetime
    prompt: str
    reply: str


class Life:
    def __init__(self, seed: int):
        self.seed = seed
        self.mails: list[Mail] = []
        self.spaces: dict[str, Space] = {}
        self.lines: list[Line] = []
        self.conversations: list[Conversation] = []
        self.docs: list[Doc] = []
        self.task_lists: list[TaskList] = []
        self.visits: list[Visit] = []
        self.saved: dict[str, list[tuple[str, str]]] = {}   # list name -> (place key, note)
        self.reviews: list[tuple[str, int, str, datetime]] = []  # (place key, stars, text, when)
        self.searches: list[Search] = []
        self.pages: list[PageVisit] = []
        self.bookmarks: list[tuple[str, str, str]] = []      # (folder, title, url)
        self.events: list[Event] = []
        self.meetings: list[Meeting] = []
        self.prompts: list[Prompt] = []
        self._ids: set[str] = set()

    def rng(self, stream: str) -> random.Random:
        return random.Random(f"{self.seed}:{stream}")

    def unique(self, kind: str, *parts) -> str:
        """A stable hex id for a record, from its kind and parts (and the seed); never the same twice."""
        n = 0
        while True:
            h = hashlib.sha256(f"{self.seed}|{kind}|{'|'.join(map(str, parts))}|{n}".encode()).hexdigest()
            if h not in self._ids:
                self._ids.add(h)
                return h
            n += 1

    # ---- mail ----

    def mail(self, when: datetime, sender, to, subject: str, body: str, cc=(), parent: Mail | None = None,
             headers: dict | None = None, tag: str | None = None, calendar: str | None = None,
             quote: bool = False) -> Mail:
        """One message. A reply (`parent`) carries In-Reply-To and References, and with `quote` quotes its parent
        under its own text, as a mail client does."""
        domain = (sender.email if isinstance(sender, Person) else sender[1]).rsplit("@", 1)[1]
        stamp = local(when).strftime("%Y%m%d%H%M%S")
        mid = f"<{stamp}.{self.unique('mail', subject, when.isoformat())[:16]}@{domain}>"
        text = body.strip()
        full = text + ("\n\n" + quoted(parent) if parent is not None and quote else "")
        m = Mail(when, sender, list(to), subject, full + "\n", list(cc), text, mid, headers=dict(headers or {}),
                 tag=tag, calendar=calendar)
        if parent is not None:
            m.in_reply_to = parent.message_id
            m.references = [*parent.references, parent.message_id]
        self.mails.append(m)
        return m

    def thread(self, subject: str, beats: list[tuple], quote: bool = True, tag: str | None = None) -> list[Mail]:
        """A conversation: each beat is (when, sender, to, body), then optionally cc and the beat's own tag; each
        reply is titled "Re: subject" and most quote the message they answer. `tag` names the first message."""
        out: list[Mail] = []
        rng = self.rng(f"thread:{subject}")
        for i, beat in enumerate(beats):
            when, sender, to, body = beat[:4]
            cc = beat[4] if len(beat) > 4 and beat[4] else ()
            beat_tag = beat[5] if len(beat) > 5 else (tag if i == 0 else None)
            parent = out[-1] if out else None
            subj = subject if parent is None else ("Re: " + subject.removeprefix("Re: "))
            out.append(self.mail(when, sender, to, subj, body, cc=cc, parent=parent, tag=beat_tag,
                                 quote=quote and rng.random() < 0.6))
        return out

    # ---- chat ----

    def space(self, key: str, name: str, members: list[Person]) -> Space:
        sid = self.unique("space", key)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        code = "".join(alphabet[int(sid[i:i + 2], 16) % 62] for i in range(0, 14, 2))
        folder = f"Space AAAA{code}" if name else f"DM {code}AAAAE"
        sp = Space(key, name, tuple(members), folder)
        self.spaces[key] = sp
        return sp

    def say(self, space: str, when: datetime, sender: Person, text: str, tag: str | None = None) -> datetime:
        self.lines.append(Line(when, space, sender, text, tag))
        return when

    def chat(self, space: str, start: datetime, lines: list[tuple[Person, str]], gap: tuple[int, int] = (1, 9),
             tag: str | None = None) -> datetime:
        """Lines said one after another from `start`, a few minutes apart. `tag` names the day's first line."""
        rng = self.rng(f"chat:{space}:{start.isoformat()}")
        when = start
        for i, (who, text) in enumerate(lines):
            self.say(space, when, who, text, tag if i == 0 else None)
            when += timedelta(minutes=rng.randint(*gap), seconds=rng.randint(0, 59))
        return when

    # ---- the rest ----

    def ask(self, title: str, turns: list[tuple[datetime, str]], tag: str | None = None) -> Conversation:
        """A ChatGPT conversation: (when, text) pairs, alternating user and assistant, user first."""
        rows = [(when, "user" if i % 2 == 0 else "assistant", text) for i, (when, text) in enumerate(turns)]
        c = Conversation(title, rows, tag)
        self.conversations.append(c)
        return c

    def doc(self, path: str, modified: datetime, body, tag: str | None = None) -> Doc:
        d = Doc(path, modified, body, tag)
        self.docs.append(d)
        return d

    def visit(self, place: str, start: datetime, minutes: int) -> None:
        self.visits.append(Visit(place, start, start + timedelta(minutes=minutes)))

    def search(self, when: datetime, product: str, title: str, url: str | None = None,
               subtitle: str | None = None) -> None:
        self.searches.append(Search(when, product, title, url, subtitle))

    def event(self, summary: str, start: datetime, minutes: int, **kw) -> Event:
        e = Event(self.unique("event", summary, start.isoformat())[:26], summary, start,
                  start + timedelta(minutes=minutes), **kw)
        self.events.append(e)
        return e

    def meet(self, start: datetime, minutes: int, counterparts=(), event: Event | None = None,
             call: bool = False) -> Meeting:
        h = self.unique("meet", start.isoformat())
        letters = "".join(chr(97 + int(h[i:i + 2], 16) % 26) for i in range(0, 20, 2))
        code = "" if call else f"{letters[:3]}-{letters[3:7]}-{letters[7:10]}"
        m = Meeting(start, minutes, code, event.occurrence_id(start) if event else "", tuple(counterparts))
        self.meetings.append(m)
        return m

    def cut(self, until: date) -> None:
        """Drop everything after `until`: the persona as it stood that day (the slice tests import)."""
        end = at(until, "23:59")

        def keep(when: datetime) -> bool:
            return when <= end
        self.mails = [m for m in self.mails if keep(m.at)]
        self.lines = [x for x in self.lines if keep(x.at)]
        self.conversations = [c for c in self.conversations if keep(c.turns[0][0])]
        for c in self.conversations:
            c.turns = [t for t in c.turns if keep(t[0])]
        self.docs = [d for d in self.docs if keep(d.modified)]
        for tl in self.task_lists:
            tl.tasks = [t for t in tl.tasks if keep(t.created)]
            for t in tl.tasks:
                t.done = t.done if t.done and keep(t.done) else None
        self.task_lists = [tl for tl in self.task_lists if tl.tasks]
        self.visits = [v for v in self.visits if keep(v.start)]
        self.reviews = [r for r in self.reviews if keep(r[3])]
        self.searches = [s for s in self.searches if keep(s.at)]
        self.pages = [x for x in self.pages if keep(x.at)]
        self.events = [e for e in self.events if keep(e.start)]
        self.meetings = [m for m in self.meetings if keep(m.start)]
        self.prompts = [x for x in self.prompts if keep(x.at)]


def name_of(who) -> str:
    return who.name if isinstance(who, Person) else who[0]


def address_of(who) -> str:
    return who.email if isinstance(who, Person) else who[1]


def quoted(m: Mail) -> str:
    """The quoted original under a reply, as Gmail writes it."""
    when = local(m.at)
    hour = when.strftime("%I:%M %p").lstrip("0")
    head = f"On {when:%a}, {when:%b} {when.day}, {when.year} at {hour} {name_of(m.sender)} <{address_of(m.sender)}> wrote:"
    return head + "\n" + "\n".join(f"> {line}" if line else ">" for line in m.text.splitlines())


def utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC)
