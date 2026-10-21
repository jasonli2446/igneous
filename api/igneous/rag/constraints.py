"""Conservative natural-language retrieval constraints.

Rules handle common, unambiguous phrasing locally.  A schema-constrained provider
call is only a best-effort fallback and its output is validated before use, so a
model outage or malformed response never changes an unconstrained search.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import providers
from igneous.rag.search import Filters

_MONTHS = {name: number for number, name in enumerate((
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
), 1)}
_MONTH_RE = "|".join(_MONTHS)
_SOURCE_PATTERNS = (
    (re.compile(r"\b(?:gmail|emails?|mail)\b", re.I), ("email", "gmail")),
    (re.compile(r"\b(?:calendar|events?)\b", re.I), ("gcal",)),
    (re.compile(r"\b(?:in\s+(?:my\s+)?files?|documents?)\b", re.I), ("fs",)),
    (re.compile(r"\b(?:texts?|iMessages?)\b", re.I), ("imessage",)),
)
_PERSON_RE = re.compile(r"\b(?:from|with|to|by|involving|regarding)\s+((?:dr\.\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b")

_FALLBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "person": {"type": ["string", "null"]},
        "source": {"type": ["string", "null"]},
        "since": {"type": ["string", "null"], "description": "ISO-8601 timestamp"},
        "until": {"type": ["string", "null"], "description": "exclusive ISO-8601 timestamp"},
    },
    "required": ["person", "source", "since", "until"],
    "additionalProperties": False,
}
_FALLBACK_SYSTEM = """Extract only explicit retrieval constraints from the question. Return null for anything absent or ambiguous. Sources must be one of email, gmail, calendar, files, or imessage. Dates must be ISO-8601; until is exclusive."""


@dataclass(frozen=True)
class Constraints:
    person_name: str | None = None
    person_id: int | None = None
    sources: tuple[str, ...] = ()
    since: datetime | None = None
    until: datetime | None = None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


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
    sources = next((sources for pattern, sources in _SOURCE_PATTERNS if pattern.search(question)), ())
    person = _PERSON_RE.search(question)
    since, until = _rule_dates(question)
    return Constraints(person_name=person.group(1).removeprefix("Dr. ") if person else None,
                       sources=sources, since=since, until=until)


def _fallback(question: str) -> Constraints:
    """Return provider output only when it conforms to our narrow local contract."""
    try:
        result = providers.chat([
            {"role": "system", "content": _FALLBACK_SYSTEM},
            {"role": "user", "content": question},
        ], _FALLBACK_SCHEMA, max_tokens=180)
        raw = json.loads(result.text)
    except Exception:  # A constraint hint must never make otherwise-valid retrieval fail.
        return Constraints()
    source = str(raw.get("source") or "").lower()
    source_map = {"email": ("email", "gmail"), "gmail": ("gmail",), "calendar": ("gcal",),
                  "files": ("fs",), "imessage": ("imessage",)}
    person = raw.get("person")
    return Constraints(person_name=person.strip() if isinstance(person, str) and person.strip() else None,
                       sources=source_map.get(source, ()), since=_parse_iso(raw.get("since")),
                       until=_parse_iso(raw.get("until")))


def resolve_person(s: Session, name: str | None) -> int | None:
    """Resolve an exact display name to its canonical Person node, never guessing partial names."""
    if not name:
        return None
    row = s.execute(text("""
        SELECT COALESCE(c.canonical_id, n.id) AS id
        FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id
        WHERE n.type = 'Person' AND lower(n.name) = lower(:name)
        ORDER BY COALESCE(c.canonical_id, n.id), n.id
        LIMIT 1
    """), {"name": name}).mappings().first()
    return int(row["id"]) if row else None


def extract(s: Session, question: str) -> Constraints:
    """Extract and resolve constraints, falling back safely if rules find none."""
    found = _rule_constraints(question)
    if not any((found.person_name, found.sources, found.since, found.until)):
        found = _fallback(question)
    return Constraints(**{**found.__dict__, "person_id": resolve_person(s, found.person_name)})


def apply(s: Session, question: str, filters: Filters | None = None) -> Filters:
    """Fill unset retrieval filters from a question without overriding explicit API filters."""
    base = filters or Filters()
    found = extract(s, question)
    return Filters(
        source=base.source or (found.sources[0] if len(found.sources) == 1 else None),
        person=base.person,
        person_id=base.person_id or found.person_id,
        sources=base.sources or (() if base.source else (found.sources if len(found.sources) > 1 else ())),
        since=base.since or found.since,
        until=base.until or found.until,
    )
