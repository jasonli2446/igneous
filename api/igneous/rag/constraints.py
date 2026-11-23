"""The retrieval filters a question states: who sent or received it, who it mentions, where to look, and when.

Rules read common, unambiguous phrasing locally: "from Jeff Skilling", "regarding Kenneth Lay", "in my calendar",
"my texts", "in May 2001". A source word narrows a search only when the question says where to look: "in my
calendar" does, "the events that led to the crisis" and "the text of the contract" don't. "regarding",
"involving", "mentioning" and "concerning" name someone the records mention, not their correspondent.

When the rules find nothing, the cheap hosted model reads the question, never a record, through `llm.structured`,
and only once some source is opted in (IGNEOUS_LLM_SOURCES). Its answer is cached under a hash of the question, so
the model reads a question once, and validated before use; any failure leaves the search unfiltered.

Filters the caller passes win; a source restriction in the question can only narrow the caller's.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import llm, source_catalog
from igneous.config import settings
from igneous.rag.search import HUMAN_NAME, Filters

_MONTHS = {name: number for number, name in enumerate((
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
), 1)}
_MONTH_RE = "|".join(_MONTHS)

# Where a question can ask to look, as stored source values.
PLACES: dict[str, tuple[str, ...]] = {
    "mail": source_catalog.values({"mail"}),
    "texts": ("imessage",),
    "calendar": source_catalog.values({"calendar"}),
    "documents": source_catalog.values({"document"}),
    "notes": ("notes",),
    "drive": ("drive",),
    "code": source_catalog.values({"code"}),
    "ai_chats": source_catalog.values({"ai_chat"}),
}
_DET = r"(?:my|the|an?|his|her|their|our|your)"
# Explicit phrasing only: a possessive ("my texts"), a place to look in ("in my calendar", "on the calendar"), or a
# compound that can only mean the source ("text messages", "calendar invites").
_SOURCE_PHRASES = tuple((re.compile(pattern, re.I), place) for pattern, place in (
    (rf"\bmy\s+(?:e-?mails?|mail|inbox)\b|\b(?:in|from|across|through)\s+(?:{_DET}\s+)?(?:e-?mails?|inbox)\b"
     r"|\b(?:in|on|from)\s+gmail\b", "mail"),
    (r"\bmy\s+(?:texts|text\s+messages|i-?messages|sms)\b|\btext\s+messages\b|\bi-?messages\b"
     r"|\b(?:in|on|from)\s+(?:i-?message|sms)\b", "texts"),
    (rf"\b(?:my|(?:in|on|from)\s+{_DET})\s+calendars?\b|\bcalendar\s+(?:events?|entries|entry|invites?|invitations?)\b",
     "calendar"),
    (r"\bmy\s+(?:local\s+)?(?:files|documents|docs)\b|\b(?:in|from|across)\s+my\s+(?:files|documents|docs)\b",
     "documents"),
    (r"\bmy\s+(?:apple\s+)?notes\b|\bin\s+apple\s+notes\b", "notes"),
    (r"\bmy\s+(?:google\s+)?drive\b|\b(?:in|on|from)\s+(?:my\s+)?(?:google\s+)?drive\b", "drive"),
    (r"\bmy\s+(?:github|repos|repositories)\b|\b(?:in|on|from)\s+(?:my\s+)?github\b", "code"),
    (r"\bmy\s+(?:ai|chatgpt|claude)\s+(?:chats|conversations)\b|\bmy\s+coding\s+sessions\b"
     r"|\bin\s+(?:chatgpt|claude\s+code|codex)\b", "ai_chats"),
))

# A person's name as written: two or three capitalized words ("Jeff Skilling", "Jeffery McMahon"), after an
# optional title ("Dr.", "Ms").
_TITLE = r"(?:(?i:dr|mr|mrs|ms|prof)\.?\s+)?"
_NAME = r"([A-Z][a-zA-Z'\u2019-]*[a-z](?:\s+[A-Z][a-zA-Z'\u2019-]*[a-z]){1,2})\b"
_CORRESPONDENT = re.compile(rf"\b(?i:from|with|to|by)\s+{_TITLE}{_NAME}")
_MENTIONED = re.compile(rf"\b(?i:regarding|involving|mentioning|concerning)\s+{_TITLE}{_NAME}")

FALLBACK_VERSION = "v1"  # part of the cache key: change it when the prompt or schema changes
_NULLABLE = {"type": ["string", "null"]}
_FALLBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "correspondent": {**_NULLABLE, "description": "full name of a person the records were sent by or to"},
        "mentioned": {**_NULLABLE, "description": "full name of a person the records are about"},
        "source": {"anyOf": [{"type": "string", "enum": sorted(PLACES)}, {"type": "null"}]},
        "since": {**_NULLABLE, "description": "ISO-8601 date"},
        "until": {**_NULLABLE, "description": "exclusive ISO-8601 date"},
    },
    "required": ["correspondent", "mentioned", "source", "since", "until"],
    "additionalProperties": False,
}
_FALLBACK_SYSTEM = (
    "Extract the retrieval filters a question about someone's own records states explicitly, and return null for "
    "anything it doesn't state or leaves ambiguous. correspondent: a person named as sender or recipient ('from', "
    "'to', 'with'). mentioned: a person the records are about ('regarding', 'involving'). source: only when the "
    "question says where to look ('in my calendar', 'my texts'), never because a word like 'event' or 'text' "
    "appears. since and until: only dates the question states (a day, month or year); until is exclusive; never "
    "resolve relative dates such as 'last week'.")


@dataclass(frozen=True)
class Constraints:
    correspondent: str | None = None
    mentioned: str | None = None
    sources: tuple[str, ...] = ()
    since: datetime | None = None
    until: datetime | None = None

    def __bool__(self) -> bool:
        return any((self.correspondent, self.mentioned, self.sources, self.since, self.until))


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
    return parsed if 1900 <= parsed.year <= 2100 else None


def _rule_dates(question: str) -> tuple[datetime | None, datetime | None]:
    q = question.lower()
    match = re.search(rf"\b(?:on\s+)?({_MONTH_RE})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", q)
    if match:
        month, day, year = _MONTHS[match.group(1)], int(match.group(2)), int(match.group(3))
        try:
            start = datetime(year, month, day)
            return start, start + timedelta(days=1)
        except ValueError:
            return None, None
    match = re.search(rf"\bin\s+({_MONTH_RE})\s+(\d{{4}})\b", q)
    if match:
        month, year = _MONTHS[match.group(1)], int(match.group(2))
        start = datetime(year, month, 1)
        return start, datetime(year + (month == 12), month % 12 + 1, 1)
    match = re.search(r"\bbetween\s+(\d{4})\s+(?:and|to)\s+(\d{4})\b", q)
    if match:
        first, last = sorted((int(match.group(1)), int(match.group(2))))
        return datetime(first, 1, 1), datetime(last + 1, 1, 1)
    match = re.search(r"\b(since|from|after|before|until)\s+(\d{4})\b", q)
    if match:
        kind, year = match.group(1), int(match.group(2))
        if kind == "after":
            return datetime(year + 1, 1, 1), None
        if kind in ("before", "until"):
            return None, datetime(year, 1, 1)
        return datetime(year, 1, 1), None
    match = re.search(r"\bin\s+(\d{4})\b", q)
    if match:
        year = int(match.group(1))
        return datetime(year, 1, 1), datetime(year + 1, 1, 1)
    return None, None


def _rule_constraints(question: str) -> Constraints:
    places = [place for pattern, place in _SOURCE_PHRASES if pattern.search(question)]
    sources = tuple(sorted({value for place in places for value in PLACES[place]}))
    correspondent, mentioned = _CORRESPONDENT.search(question), _MENTIONED.search(question)
    since, until = _rule_dates(question)
    return Constraints(correspondent=correspondent.group(1) if correspondent else None,
                       mentioned=mentioned.group(1) if mentioned else None,
                       sources=sources, since=since, until=until)


def _name(value: object) -> str | None:
    """A model-given person name, when it looks like one."""
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value if 0 < len(value) <= 80 and re.fullmatch(r"[^\W\d_][\w .'\u2019-]*", value) else None


def _fallback(question: str) -> Constraints:
    """The cheap model's reading of the question, validated; empty when nothing is opted in or anything fails."""
    key = f"constraints:{FALLBACK_VERSION}:" + hashlib.sha256(question.encode()).hexdigest()
    try:
        raw = llm.cache_lookup(key)
        if raw is None:
            if not llm.allowed_sources():  # with nothing opted in, not even the question leaves this computer
                return Constraints()
            raw, _ = llm.structured(_FALLBACK_SYSTEM, question, _FALLBACK_SCHEMA, max_tokens=400,
                                    model=settings.llm_cheap_model)
            llm.cache_store(key, raw)
    except Exception:  # A filter hint must never make otherwise-valid retrieval fail.
        return Constraints()
    since, until = _parse_iso(raw.get("since")), _parse_iso(raw.get("until"))
    if since and until and since >= until:
        since = until = None
    return Constraints(correspondent=_name(raw.get("correspondent")), mentioned=_name(raw.get("mentioned")),
                       sources=PLACES.get(str(raw.get("source")), ()), since=since, until=until)


def resolve_person(s: Session, name: str | None) -> int | None:
    """The identity head of the Person whose display name is exactly `name` (case aside), never a partial match. A
    three-word name ("Jeff Skilling In") is also tried without its last word."""
    if not name:
        return None
    words = name.split()
    for candidate in (" ".join(words[:n]) for n in range(len(words), 1, -1)):
        row = s.execute(text("""
            SELECT COALESCE(c.canonical_id, n.id) AS id
            FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id
            WHERE n.type = 'Person' AND lower(n.name) = lower(:name)
            ORDER BY COALESCE(c.canonical_id, n.id), n.id
            LIMIT 1
        """), {"name": candidate}).scalar()
        if row is not None:
            return int(row)
    return None


def person_by_address_or_name(s: Session, value: str) -> int | None:
    """The identity head of the Person a caller names by address (a Person's key, stored lowercase) or by exact
    display name."""
    value = value.strip()
    if not value:
        return None
    row = s.execute(text("""
        SELECT COALESCE(c.canonical_id, n.id) FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id
        WHERE n.type = 'Person' AND n.key = lower(:v)
    """), {"v": value}).scalar()
    return int(row) if row is not None else resolve_person(s, value)


def _servable_mention(s: Session, person_id: int) -> bool:
    """Whether a mention filter can find this person: one of their identities has a name to look for in text (two
    or more words, not an address), or extraction linked a record to them."""
    return bool(s.execute(text("""
        WITH ids AS (SELECT CAST(:p AS bigint) AS id UNION SELECT node_id FROM canonical WHERE canonical_id = :p)
        SELECT EXISTS (SELECT 1 FROM nodes n JOIN ids ON ids.id = n.id WHERE n.name ~ :human_name)
            OR EXISTS (SELECT 1 FROM edges e JOIN ids ON ids.id = e.dst WHERE e.type = 'MENTIONS')
    """), {"p": person_id, "human_name": HUMAN_NAME}).scalar())


def apply(s: Session, question: str, filters: Filters | None = None) -> Filters:
    """`filters` with what the question states filled in. A filter the caller set wins (a date range as a whole),
    except that a source restriction in the question narrows the caller's where they overlap. A name that matches no
    Person, or a mentioned person the records can't be searched for, adds no filter. `from_question` lists what the
    question set."""
    base = filters or Filters()
    found = _rule_constraints(question)
    if not found:
        found = _fallback(question)
    narrowed = tuple(sorted(set(base.sources) & set(found.sources))) if base.sources else found.sources
    person_id = resolve_person(s, found.correspondent) if not base.person_id else None
    mentions_id = resolve_person(s, found.mentioned) if not base.mentions_id else None
    if mentions_id and not _servable_mention(s, mentions_id):
        mentions_id = None
    caller_dates = bool(base.since or base.until)
    since, until = (base.since, base.until) if caller_dates else (found.since, found.until)
    from_question = {name for name, value in (
        ("sources", narrowed and set(narrowed) != set(base.sources)), ("person", person_id),
        ("mentions", mentions_id), ("since", not caller_dates and since), ("until", not caller_dates and until))
        if value}
    return replace(base, sources=narrowed or base.sources, person_id=base.person_id or person_id,
                   mentions_id=base.mentions_id or mentions_id, since=since, until=until,
                   from_question=frozenset(from_question))


def describe(s: Session, f: Filters) -> dict:
    """The filters a search or an answer used, for its response: the sources, the person it is from or to and the
    person it mentions (id and name), the date range (until is exclusive), and which of them the question set."""
    ids = [i for i in (f.person_id, f.mentions_id) if i]
    names = dict(s.execute(text("SELECT id, name FROM nodes WHERE id = ANY(:ids)"), {"ids": ids}).all()) if ids else {}

    def who(node_id: int | None) -> dict | None:
        return {"id": node_id, "name": names.get(node_id)} if node_id else None
    return {"sources": list(f.sources), "person": who(f.person_id), "mentions": who(f.mentions_id),
            "since": f.since, "until": f.until, "from_question": sorted(f.from_question)}
