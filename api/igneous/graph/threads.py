"""Life threads: the trips, classes, jobs, hobbies and projects that no single record names.

A weekend trip is a flight email, an itinerary doc, browsing, a calendar block and a group chat, and nothing
labels all of them. What they share is a distinctive term that suddenly shows up in several kinds of source at
once. So a thread is found, not configured:

  1. Terms: capitalized phrases from item titles and text ("Portland", "Powell's", "CS 401", "Gloomhaven Night").
  2. Burst: a term in 6+ records from 3+ kinds of source within BURST_DAYS, denser there than its own background
     and in at most MAX_DF of all records, is a candidate; its densest window is its span. Name seeds add
     long-running things named outright (a repository, a course code in the titles of your mail or calendar, a
     calendar series, a named group chat); a seed's window follows its anchor, and a course's anchor is its term.
  3. Members: records that name one of its defining terms (inside its span, give or take a month, unless it is
     ongoing); people who took part (its group chat, its events) or talk about it in its small conversations.
  4. Overlapping candidates (same window, mostly the same records) merge into one. A shared course code within one
     term makes one thread: a candidate led by another word, most of whose records name a class's code within the
     class's window (the capstone's project, its demo day), is part of that class.
  5. Judge: with the model enabled and sources opted in (IGNEOUS_LLM_SOURCES), one cached structured call per
     candidate decides keep / kind (trip, class, job, hobby, project, ... free text) / the name you would use / its
     defining terms, from titles and short snippets of opted-in records only (counts for the rest). Without it, a
     thread is named by its term and month ("Portland · Aug 2026"), kind "thread", and stays unjudged.

A judged thread is a persistent identity in `thread_registry`, keyed by its first term (and month, for a burst): later
runs attach new records to it by its defining terms, with no model call, and judge only what is new. Its lifecycle:

  active    built every run from its terms; an unjudged one (named without the model) is judged once its records may
            be read, within the run's budget, unless you renamed it (one you merged others into stays, whatever
            the judge says)
  rejected  judged not a thread; never built, never judged again. What had been merged into it (found to be the same
            thing) stands on its own again.
  merged    a facet of the thread it was folded into (merged_into): two found to be one (graph/resolve_threads.py), a
            part of a class (registered as its facet, unjudged), or a merge you made (merged_by 'user'). Its own terms
            and span keep finding its records, for that thread.

A failed model call writes no row (the candidate is judged on a later run), and the budget (JUDGE_BUDGET model calls)
is spent only on candidates that are new. Profile projects (graph/profile.py) are pinned rows: built whenever a record
names them. Terms, candidates, keys and names come out the same whatever the order of the records or the hash seed.

Writes `Project` nodes (props.derived 'threads', through graph/derived.py's Writer, so a name you gave one stays and a
link you rejected is never written), PART_OF item -> thread and INVOLVED_IN person -> thread with their evidence; a
thread no registry row builds any more is deleted. Then: duplicates resolved and related threads linked
(resolve_threads), umbrellas over them (an org, lab or program several threads sit under), the hierarchy you stated
(thread_parents), and the group chats and calendar series that are about a thread.
"""
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import source_catalog
from igneous.config import settings
from igneous.graph.derived import Writer, follow, norm

EXTRACTOR = "threads"
VERSION = "1"
BURST_DAYS = 60          # a thread's mentions cluster inside this many days
MIN_SOURCES = 3          # ...across at least this many kinds of source (mail, chat, calendar, files, ...)
MIN_ITEMS = 6
MAX_DF = 0.02            # a term in more than 2% of all items is background (a hometown, a school), not a thread
JUDGE_BUDGET = 80        # model judgments per run (new candidates and unjudged threads); the rest wait for the next
UMBRELLA_MONTHS = 6      # an umbrella (org, program, lab) is present across at least this many months...
UMBRELLA_SHARE = 0.2     # ...and makes up at least this share of the items of...
UMBRELLA_THREADS = 2     # ...at least this many threads
PAD = timedelta(days=30)  # around a thread's or anchor's own dates: planning before, photos and follow-ups after

# Where the threads of a life show up: messages, calendars, documents, code, AI chats and browsing.
SOURCES = source_catalog.LIFE

# Kinds of thread that don't happen in one window (the judge names kinds freely, so match words).
ONGOING = re.compile(r"^(?!.*\b(project|application|trip|event)\b).*\b(hobby|hobbies|club|sport|recurring|friend|friends|"
                     r"group|team|community|band|league|lessons|practice)\b", re.I)

# A course code: a department of 2-5 letters and a 3-digit number, each a whole word ("CS 401", "cs401", "MATH 221";
# not "June 2025" or "Invoice 2025"). A month is never a department.
MONTHS = frozenset({"jan", "feb", "mar", "apr", "may", "jun", "june", "jul", "july", "aug", "sep", "sept", "oct",
                    "nov", "dec"})
COURSE = r"([A-Za-z]{2,5})\s?(\d{3})"
COURSE_CODE = re.compile(rf"\b{COURSE}\b")
COURSE_SQL = r"\m[A-Za-z]{2,5} ?[0-9]{3}\M"  # the same, as a Postgres regular expression
COURSE_TITLES = source_catalog.values({"mail", "calendar"})  # where a class is named in titles: mail, calendar
TERM_GAP = timedelta(days=45)  # a course's mentions with no longer gap between them are one term
MOSTLY = 0.5  # of a candidate's records naming a course code inside its term, for the candidate to be part of it


def course_code(text_: str) -> str | None:
    """The course code `text_` is ("CS 401" for "cs401"), or None."""
    m = re.fullmatch(COURSE, (text_ or "").strip())
    return f"{m.group(1).upper()} {m.group(2)}" if m and m.group(1).lower() not in MONTHS else None


def course_codes(text_: str) -> list[str]:
    """Every course code in `text_`, as course_code spells it."""
    return [f"{d.upper()} {n}" for d, n in COURSE_CODE.findall(text_ or "") if d.lower() not in MONTHS]


# Capitalized phrases (1-3 words), or course codes.
TERM = re.compile(r"\b(?:[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,2}|[A-Z]{2,5}\s?\d{3})\b")
STOP = {
    # sentence starters, days, months, generic product/UI words and greetings that are capitalized for other reasons
    "The", "This", "That", "These", "Those", "There", "Their", "They", "What", "When", "Where", "Which", "Who",
    "Why", "How", "And", "But", "For", "From", "With", "Your", "You", "Our", "Hey", "Hello", "Thanks", "Thank",
    "Please", "Sorry", "Yes", "Yeah", "Okay", "Sure", "Also", "Just", "Here", "Have", "Has", "Will", "Can", "Could",
    "Would", "Should", "Let", "Lets", "Did", "Does", "Not", "Get", "Got", "See", "Sent", "Reply", "Subject",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February", "March",
    "April", "May", "June", "July", "August", "September", "October", "November", "December", "Today", "Tomorrow",
    "Google", "Gmail", "Chrome", "Search", "Maps", "Drive", "Docs", "Calendar", "Meet", "Messages", "Message",
    "Email", "Mail", "Inbox", "Account", "Password", "Security", "Notification", "Update", "Order", "Receipt",
    "Unsubscribe", "Privacy", "Terms", "Click", "View", "Read", "More", "New", "Re", "Fwd", "Fw", "Visited",
    "Searched", "Watched", "Used", "Viewed", "Assistant", "User", "Igneous",  # AI chat roles + this tool
}


def _terms(text_: str) -> list[str]:
    """The distinct terms in `text_`, sorted."""
    out = set()
    for m in TERM.findall(text_ or ""):
        if re.fullmatch(COURSE, m):
            if code := course_code(m):  # "JUNE 202" is no course
                out.add(code)
            continue
        words = m.split()
        while words and words[0] in STOP:
            words = words[1:]
        while words and words[-1] in STOP:
            words = words[:-1]
        if words and not (len(words) == 1 and len(words[0]) < 4):
            out.add(" ".join(words))
    return sorted(out)


def _items(s: Session) -> list:
    return s.execute(text("""
        SELECT DISTINCT ON (si.id) si.id, si.source, si.occurred_at, coalesce(si.title, '') AS title,
               left(coalesce(si.body_text, ''), 4000) AS body, n.id AS node_id
        FROM source_items si
        LEFT JOIN nodes n ON n.source_item_id = si.id AND n.type IN ('Message', 'File', 'Event')
        WHERE si.source = ANY(:src) AND si.occurred_at IS NOT NULL
          -- Notifications and marketing never make or join a thread (they stay searchable and as evidence):
          -- a promo that mentions "Chinatown" isn't part of anything.
          AND COALESCE(si.item_class, 'conversation') NOT IN ('notification', 'marketing')
        ORDER BY si.id, n.id
    """), {"src": list(SOURCES)}).all()


def _by_time(rows) -> list:
    return sorted(rows, key=lambda r: (r.occurred_at, r.id))


def _bursts(its: list) -> list[tuple[int, int]]:
    """Windows where a term is far denser than its own background: (lo, hi) index ranges into the time-sorted
    items. A place mentioned for years (Portland) can still have a trip-shaped spike; each spike is a candidate."""
    if len(its) < MIN_ITEMS:
        return []
    span_days = max(1.0, (its[-1].occurred_at - its[0].occurred_at).total_seconds() / 86400)
    background = len(its) / span_days * BURST_DAYS  # mentions per window if they were spread evenly
    out, lo, best = [], 0, None
    for hi in range(len(its)):
        while its[hi].occurred_at - its[lo].occurred_at > timedelta(days=BURST_DAYS):
            lo += 1
        n = hi - lo + 1
        srcs = len({r.source for r in its[lo:hi + 1]})
        dense = n >= MIN_ITEMS and srcs >= MIN_SOURCES and (n >= 3 * background or n >= 0.5 * len(its))
        if dense and (best is None or (srcs, n) > best[0]):
            best = ((srcs, n), lo, hi)
        if best and its[hi].occurred_at - its[best[2]].occurred_at > timedelta(days=BURST_DAYS):
            out.append((best[1], best[2]))  # this spike is over; look for the next one
            best = None
    if best:
        out.append((best[1], best[2]))
    return out


def burst_candidates(items: list, excluded: set[str]) -> list[dict]:
    """Terms that spike across several kinds of source in one window, with their member items (pure: the same
    items give the same candidates, in the same order, whatever order they come in). `excluded`: lowercase terms
    never a thread on their own (people's names)."""
    if not items:
        return []
    by_term: dict[str, list] = defaultdict(list)
    for it in items:
        # Names of trips, classes and projects show up in titles and opening lines; the first 1,500 characters
        # of a body are enough and keep this fast on a large mailbox.
        for t in sorted(set(_terms(it.title)) | set(_terms(it.body[:1500]))):
            by_term[t].append(it)
    out = []
    for term in sorted(by_term):
        its = by_term[term]
        if len(its) < MIN_ITEMS or len(its) > MAX_DF * len(items) or term.lower() in excluded:
            continue
        its = _by_time(its)
        for lo, hi in _bursts(its):
            win = its[lo:hi + 1]
            out.append({"term": term, "start": win[0].occurred_at, "end": win[-1].occurred_at,
                        "items": {r.id: r for r in win}, "sources": Counter(r.source for r in win)})
    # Repeating terms are kept (a weekly game repeats): whether a candidate names a real thing is the judge's call.
    return _merge(out)


def candidates(s: Session) -> list[dict]:
    """Bursts in every record of the life sources, never on a person's name (the owner's own name words included:
    they are on everything the owner writes)."""
    people = {r[0] for r in s.execute(text(
        "SELECT lower(name) FROM nodes WHERE type = 'Person' AND name ~ ' '"))}
    people |= {w.lower() for (n,) in s.execute(text("""
        SELECT n.name FROM nodes n WHERE n.type = 'Person' AND (COALESCE((n.props->>'self')::bool, false)
           OR n.id IN (SELECT c.node_id FROM canonical c JOIN nodes h ON h.id = c.canonical_id
                       WHERE COALESCE((h.props->>'self')::bool, false)))""")) for w in (n or "").split() if "@" not in w}
    return burst_candidates(_items(s), people)


def _merge(cands: list[dict]) -> list[dict]:
    """Candidates over mostly the same items in overlapping windows are one thread ("Portland" + "Powell's"). The
    biggest leads, ties broken by window and term, so its first term (the thread's key) never depends on order."""
    cands = sorted(cands, key=lambda c: (-len(c["sources"]), -len(c["items"]), c["start"], c["term"]))
    merged: list[dict] = []
    for c in cands:
        for m in merged:
            shared = len(c["items"].keys() & m["items"].keys())
            if shared >= 0.6 * min(len(c["items"]), len(m["items"])) and c["start"] <= m["end"] and m["start"] <= c["end"]:
                m["items"].update(c["items"])
                m["sources"].update(c["sources"])
                m["terms"].append(c["term"])
                m["start"], m["end"] = min(m["start"], c["start"]), max(m["end"], c["end"])
                break
        else:
            merged.append({**c, "items": dict(c["items"]), "sources": Counter(c["sources"]), "terms": [c["term"]]})
    return merged


def thread_key(c: dict) -> str:
    """A candidate's registry key: its first term (and month, for a burst: the same word in another season is
    another trip)."""
    seed = c["terms"][0].lower() + ("" if c.get("seed") else c["start"].strftime("%Y-%m"))
    return "thread:" + hashlib.sha256(seed.encode()).hexdigest()[:16]


CORE = 3            # the thread's most active people set the bar for everyone else
RELATIVE_FLOOR = 0.15  # of the core's typical count: below it, a mention is noise, not membership


def relative_members(counts: list[tuple[int, int]], keep: set[int] = frozenset()) -> set[int]:
    """Who is really in it, judged against how active its core is. When a few people mention it 20 times each,
    someone with one or two mentions is passing by; in a small thread where everyone has 2-3, they all count.
    `keep` (group chat members, event attendees) is direct evidence and always stays."""
    if not counts:
        return set(keep)
    top = sorted((n for _p, n in counts), reverse=True)[:CORE]
    core = top[len(top) // 2]  # median of the core: one hyperactive person doesn't raise the bar for all
    bar = max(2, round(core * RELATIVE_FLOOR))
    return {p for p, n in counts if n >= bar} | set(keep)


def _people(s: Session, item_ids: list[int]) -> list[tuple[int, int, list[int]]]:
    """(canonical person, qualifying items) for people who are really in the thread, excluding services and you.

    Being one of many recipients of a mention (a club mailing that names the game) isn't membership. An item
    counts for a person if they wrote it, or it's a small conversation (<= 4 people), or it's an event they
    attended. Two qualifying items make a member."""
    return s.execute(text("""
        WITH msg AS (SELECT n.id, n.type FROM nodes n WHERE n.source_item_id = ANY(:ids)),
        size AS (SELECT m.id, (SELECT count(*) FROM edges r WHERE (r.src = m.id AND r.type = 'RECEIVED')
                                                          OR (r.dst = m.id AND r.type IN ('SENT', 'ATTENDED'))) AS n
                 FROM msg m),
        ppl AS (
            SELECT e.src AS p, e.dst AS m, true AS wrote FROM edges e JOIN msg ON msg.id = e.dst
            WHERE e.type IN ('SENT', 'ATTENDED')
            UNION SELECT e.dst, e.src, false FROM edges e JOIN msg ON msg.id = e.src WHERE e.type = 'RECEIVED'),
        ok AS (SELECT ppl.p, ppl.m FROM ppl JOIN size ON size.id = ppl.m JOIN msg ON msg.id = ppl.m
               WHERE ppl.wrote OR size.n <= 4 OR msg.type = 'Event')
        SELECT COALESCE(c.canonical_id, ok.p) AS p, count(DISTINCT ok.m) AS n,
               (array_agg(DISTINCT (SELECT source_item_id FROM nodes WHERE id = ok.m)))[1:8] AS items
        FROM ok LEFT JOIN canonical c ON c.node_id = ok.p JOIN nodes pn ON pn.id = ok.p
        JOIN nodes head ON head.id = COALESCE(c.canonical_id, ok.p)
        WHERE pn.type = 'Person' AND COALESCE(pn.props->>'kind', '') <> 'service'
          AND NOT COALESCE((head.props->>'self')::bool, false)  -- you, under any of your addresses
        GROUP BY 1 HAVING count(DISTINCT ok.m) >= 2 ORDER BY 2 DESC, 1 LIMIT 30
    """), {"ids": item_ids}).all()


def _sample(items: list, n: int = 40) -> list:
    """Spread across the window and the sources, so the judge sees the whole candidate, not its first days."""
    items = _by_time(items)
    if len(items) <= n:
        return items
    by_src: dict[str, list] = defaultdict(list)
    for r in items:
        by_src[r.source].append(r)
    out: list = []
    while len(out) < n and any(by_src.values()):
        for src in sorted(by_src):
            lst = by_src[src]
            if lst:
                out.append(lst.pop(len(lst) // 2 if len(out) % 2 else 0))
    return _by_time(out[:n])


def _fallback_name(c: dict) -> str:
    """A candidate's name without the model: its first term and its months ("Portland · Aug 2026")."""
    month = c["start"].strftime("%b %Y") if c["start"].strftime("%b %Y") == c["end"].strftime("%b %Y") \
        else f"{c['start']:%b}-{c['end']:%b %Y}"
    return f"{c['terms'][0]} · {month}"


def _judge(s: Session, c: dict, may_call: bool = True) -> tuple[dict | None, bool]:
    """Decide what the candidate is, which of its records belong, and which words define it: (verdict, whether a
    model call was made). A model only reads opted-in records, and only the shared terms those records contain;
    judgments are cached per question. Without a model (or a readable record) the verdict is the fallback: its seed
    term defines it, judged=False. None when the call failed, or a call was needed and `may_call` is False: the
    candidate waits for a later run. Cached per candidate: all its terms and the records shown."""
    from igneous.llm import LLMDisabled, allowed_sources, cache_lookup, cache_store, structured

    allowed = allowed_sources()
    fallback = {"keep": True, "kind": "thread", "name": _fallback_name(c), "confidence": 0.6,
                "judged": False, "defining_terms": [c["terms"][0]], "members": None}
    shown = _sample([r for r in c["items"].values() if r.source in allowed])
    if not settings.llm_enabled or not shown:
        return fallback, False
    lines = [f"{i}. [{r.source}, {r.occurred_at:%Y-%m-%d}] {r.title[:120]} :: {re.sub(r'\\s+', ' ', r.body)[:240]}"
             for i, r in enumerate(shown)]
    others = {src: n for src, n in sorted(c["sources"].items()) if src not in allowed}

    def prompt_with(terms: list[str]) -> str:
        return (f"Shared terms: {', '.join(terms)}\nWindow: {c['start']:%Y-%m-%d} to {c['end']:%Y-%m-%d}"
                f" ({len(c['items'])} records; a sample is listed)\nRecords:\n" + "\n".join(lines) +
                (f"\nAlso, records not shown (counts only): {json.dumps(others)}" if others else ""))
    seen = " ".join(f"{r.title} {r.body}" for r in shown).lower()
    prompt = prompt_with([t for t in c["terms"][:8] if t.lower() in seen])  # terms mined elsewhere stay out
    key = "threads-judge:v2:" + hashlib.sha256(prompt_with(c["terms"][:8]).encode()).hexdigest()
    cached = cache_lookup(key)
    called = False
    if cached is None:
        if not may_call:
            return None, False
        called = True
        try:
            cached, _ = structured(
                "You find the threads of someone's life in their records: a trip, a class, a job or internship, a "
                "hobby, a project, a club, an event they planned. These records were grouped because they share "
                "some words within a time window, but sharing a word doesn't make them one thing. Decide: is "
                "there a real thread here? If yes: name it the way the person would say it ('weekend trip', "
                "'CS 401', 'Gloomhaven Night'), say what kind of thing it is, list the words that actually identify "
                "it (its name, nicknames, codes, not generic words like 'other', 'meeting', 'notes'), and list "
                "the numbers of ONLY the records that are about it. Leave out records that merely share a word "
                "(an unrelated meeting, a newsletter). If there is no single thread, keep=false.",
                prompt,
                {"type": "object", "additionalProperties": False,
                 "required": ["keep", "kind", "name", "confidence", "defining_terms", "members"],
                 "properties": {"keep": {"type": "boolean"}, "kind": {"type": "string"}, "name": {"type": "string"},
                                "confidence": {"type": "number"},
                                "defining_terms": {"type": "array", "items": {"type": "string"}},
                                "members": {"type": "array", "items": {"type": "integer"}}}},
                max_tokens=1200)
        except LLMDisabled:
            return fallback, True
        except Exception:  # no verdict, no registry row: the candidate is judged again on a later run
            return None, True
        cache_store(key, cached)
    judged_ids = [shown[i].id for i in cached.get("members", []) if isinstance(i, int) and 0 <= i < len(shown)]
    return {**cached, "judged": True, "members": judged_ids, "shown": [r.id for r in shown]}, called


def _belongs(c: dict, verdict: dict) -> list:
    """The candidate's records that belong: those the judge picked from its sample, plus unseen records that
    name one of its defining terms. A shown record the judge left out is out."""
    if verdict.get("members") is None:  # no judge: the seed term defines it
        terms = [t.lower() for t in verdict.get("defining_terms") or c["terms"][:1]]
        return [r for r in c["items"].values() if any(t in (r.title + " " + r.body).lower() for t in terms)]
    shown, picked = set(verdict.get("shown", [])), set(verdict["members"])
    terms = [t.lower() for t in verdict.get("defining_terms") or [] if len(t) >= 3]
    out = []
    for r in c["items"].values():
        if r.id in shown:
            if r.id in picked:
                out.append(r)
        elif terms and any(t in (r.title + " " + r.body).lower() for t in terms):
            out.append(r)
    return out


def name_seeds(s: Session) -> list[dict]:
    """Long-running things named outright: repositories, course codes (in the titles of your mail, from any mail
    source, and your calendar: a class's own notices count, a promo naming a course doesn't), recurring calendar
    series, named group chats. A burst finds a trip; a name finds a semester-long course or a year-long project."""
    rows = s.execute(text("""
        SELECT 'repo' AS how, lower(raw_meta->>'repo') AS name FROM source_items
        WHERE source IN ('github', 'ai_chats', 'fs') AND coalesce(raw_meta->>'repo', '') <> ''
        GROUP BY 2 HAVING count(*) >= 5
        UNION ALL
        SELECT 'course', upper(regexp_replace(substring(title from :course), '\\s+', ' ', 'g'))
        FROM source_items WHERE source = ANY(:titled) AND title ~ :course AND item_class IS DISTINCT FROM 'marketing'
        GROUP BY 2 HAVING count(*) >= 5
        UNION ALL
        SELECT 'series', min(title) FROM source_items
        WHERE source = 'gcal' AND coalesce(title, '') <> '' AND coalesce(raw_meta->>'event_key', '') <> ''
        GROUP BY raw_meta->>'event_key' HAVING count(*) >= 4
        UNION ALL
        SELECT 'chat', name FROM nodes WHERE type = 'Thread' AND COALESCE((props->>'group')::bool, false)
          AND name !~ '[0-9]{4}' AND name !~ ',' AND length(name) >= 3
        ORDER BY 1, 2
    """), {"course": f"({COURSE_SQL})", "titled": list(COURSE_TITLES)}).all()
    seeds: list[dict] = []
    seen = set()
    for how, name in rows:
        n = (name or "").strip()
        if how == "course":
            n = course_code(n) or ""  # a month is no department
        if len(n) < 3 or n.lower() in seen:
            continue
        seen.add(n.lower())
        seeds.append({"how": how, "name": n, "term": n.replace("-", " ") if how == "repo" else n})
    return seeds


def _term(s: Session, code: str):
    """A course's term: the longest run of the records naming its code (its notices too, never marketing) with no gap
    over TERM_GAP inside (the first of runs as long), as (first, last); None when nothing names it. A planning note
    months before, or a resume line a year after, is outside it."""
    dept, num = code.lower().split()
    runs: list[list] = []
    for (at,) in s.execute(text("""
        SELECT si.occurred_at FROM source_items si
        WHERE si.source = ANY(:src) AND si.occurred_at IS NOT NULL AND si.item_class IS DISTINCT FROM 'marketing'
          AND (coalesce(si.title, '') || ' ' || left(coalesce(si.body_text, ''), 4000)) ~* :code
        ORDER BY 1"""), {"src": list(SOURCES), "code": rf"\m{dept} ?{num}\M"}):
        if runs and at - runs[-1][-1] <= TERM_GAP:
            runs[-1].append(at)
        else:
            runs.append([at])
    term = max(runs, key=len, default=None)
    return (term[0], term[-1]) if term else None


def _anchor_span(s: Session, seed: dict):
    """When the seed's own anchor was active: (first, last), or None for anchors without dates of their own. A
    course's anchor is its term."""
    name = seed["name"]
    if seed["how"] == "course":
        return _term(s, name)
    if seed["how"] == "series":
        q = "SELECT min(occurred_at), max(occurred_at) FROM source_items WHERE source = 'gcal' AND title = :n"
    elif seed["how"] == "repo":
        q = ("SELECT min(occurred_at), max(occurred_at) FROM source_items WHERE source IN ('github', 'ai_chats', 'fs') "
             "AND lower(raw_meta->>'repo') = lower(:n)")
    elif seed["how"] == "chat":
        q = ("SELECT min(si.occurred_at), max(si.occurred_at) FROM nodes t JOIN edges it ON it.dst = t.id AND it.type = 'IN_THREAD' "
             "JOIN nodes m ON m.id = it.src JOIN source_items si ON si.id = m.source_item_id WHERE t.type = 'Thread' AND t.name = :n")
    else:
        return None
    row = s.execute(text(q), {"n": name}).first()
    return (row[0], row[1]) if row and row[0] else None


def _seed_candidate(s: Session, seed: dict, items_by_id: dict) -> dict | None:
    """Records naming a seed (in title or text), across all time: a long-running candidate."""
    term = seed["term"]
    variants = sorted({term, term.replace(" ", "-"), term.replace(" ", "")})
    q = " OR ".join(f"(si.title ILIKE :v{i} OR left(si.body_text, 4000) ILIKE :v{i})" for i in range(len(variants)))
    ids = s.execute(text(f"SELECT si.id FROM source_items si WHERE si.source = ANY(:src) AND ({q}) "
                         "ORDER BY si.id LIMIT 3000"),
                    {"src": list(SOURCES), **{f"v{i}": f"%{v}%" for i, v in enumerate(variants)}}).scalars().all()
    items = {i: items_by_id[i] for i in ids if i in items_by_id}
    # The thread lives when its anchor does: a calendar series' occurrences, a repo's activity, a chat's days.
    # A one-time hangout is its evening, not every later text naming the neighborhood.
    anchor = _anchor_span(s, seed)
    if anchor:
        lo, hi = anchor
        items = {k: v for k, v in items.items() if lo - PAD <= v.occurred_at <= hi + PAD}
    if len(items) < MIN_ITEMS or len({r.source for r in items.values()}) < 2:
        return None
    ts = sorted(r.occurred_at for r in items.values())
    return {"term": term, "terms": [term], "start": ts[0], "end": ts[-1], "items": items,
            "sources": Counter(r.source for r in items.values()), "seed": seed["how"], "seed_name": seed["name"],
            "anchor": anchor}


def _pattern(t: str) -> str:
    """A defining term as a whole-word pattern; a course code matches with or without its space, never without its
    department."""
    code = course_code(t)
    if code:
        dept, num = code.lower().split()
        return rf"{dept}\s?{num}"
    return re.escape(t)


def window(row) -> tuple[datetime, datetime] | None:
    """When a registry row's records happen: its span give or take PAD, or None when they count whenever they happened
    (a hobby, club or friend group, a pinned project, a row with no span)."""
    if row.span_start and row.span_end and row.origin in ("burst", "name") and not ONGOING.search(row.kind or ""):
        return row.span_start - PAD, row.span_end + PAD
    return None


def registered(rows, items: list) -> dict[str, dict]:
    """Every registry row's records now, in one pass over the items: those naming one of its defining terms, inside
    its window (`window`). Deterministic, which is what keeps a found thread found."""
    specs = []
    for row in rows:
        # A bare number ("395") is shared by different courses, rooms and years: never a defining term alone.
        terms = sorted({t.lower() for t in (row.terms or []) if len(t) >= 3 and not t.strip().isdigit()})
        if not terms:
            continue
        lo, hi = window(row) or (None, None)
        # Whole words: a short defining term ("eta") must not match inside other words ("beta", "theta").
        pattern = re.compile(r"(?<![a-z0-9])(" + "|".join(_pattern(t) for t in terms) + r")(?![a-z0-9])")
        specs.append((row.key, pattern, lo, hi))
    out: dict[str, dict] = {k: {} for k, *_ in specs}
    for it in items:
        hay = (it.title + " " + it.body).lower()
        for key, pattern, lo, hi in specs:
            if lo is not None and not (lo <= it.occurred_at <= hi):
                continue
            if pattern.search(hay):
                out[key][it.id] = it
    return out


def _course(row) -> tuple[str, str, tuple | None] | None:
    """(key, course code, window) of a class thread: one a course code defines; None for any other."""
    code = next((c for t in row.terms or [] if (c := course_code(t))), None)
    return (row.key, code, window(row)) if code else None


def _course_part(c: dict, courses: list[tuple[str, str, tuple | None]]) -> str | None:
    """The key of the class thread a candidate is part of: a shared course code within one term makes one thread. A
    candidate led by another word (the capstone's project, its demo day) is part of a class when most of its records
    name the class's code inside the class's window; of two such classes, the one more of them name. None for a
    candidate that is a course itself, or no course's."""
    if course_code(c["terms"][0]):
        return None
    best = None
    for key, code, win in courses:
        pattern = re.compile(rf"(?<![a-z0-9]){_pattern(code)}(?![a-z0-9])")
        n = sum(1 for r in c["items"].values()
                if (win is None or win[0] <= r.occurred_at <= win[1]) and pattern.search((r.title + " " + r.body).lower()))
        if n >= MOSTLY * len(c["items"]) and (best is None or n > best[0]):
            best = (n, key)
    return best[1] if best else None


def _named_from(row) -> str:
    """Where a thread's name came from (llm.nameable reads it): your resume, you, a model reading opted-in records, or
    a term found in its own records."""
    if row.origin == "profile":
        return "profile"
    if row.origin in ("gold", "user"):
        return "you"
    return "model" if row.judged else "records"


def _write_thread(w: Writer, row, members: list, facet_of: dict[int, str], aliases: list[str]) -> int:
    """Upsert one thread and its links: PART_OF from each member record (marked with the facet it came through, for
    a merge you made, so its undo can take them back), and its people."""
    s = w.s
    # Its span is what happened plus what's planned soon: a calendar repeat set for 2040 isn't activity.
    horizon = datetime.now(UTC) + timedelta(days=180)
    span = sorted(r.occurred_at for r in members if r.occurred_at <= horizon) or sorted(r.occurred_at for r in members)
    terms = list(row.terms)
    props = {"derived": "threads", "kind": row.kind or "thread", "terms": terms[:6], "start": span[0].isoformat(),
             "end": span[-1].isoformat(), "sources": dict(sorted(Counter(r.source for r in members).items())),
             "records": len(members), "pinned": row.pinned, "named_from": _named_from(row), "aliases": aliases}
    tid = w.project(row.key, row.name, props)
    for it in _by_time(members):
        if it.node_id:
            why = {"why": f"mentions {terms[0]}"}
            if it.id in facet_of:
                why["merged_from"] = {"key": facet_of[it.id], "end": "dst"}
            w.edge(it.node_id, tid, "PART_OF", why, confidence=0.6, source_item_id=it.id,
                   evidence=[(it.id, why["why"], why)])
    # Who took part comes from the strongest evidence: the thread's own group chat (the chat that most of its
    # conversation happened in) and the attendees of its events. The record itself lists them, so the verifier
    # settles those links without a model (props.direct). Someone who only talked about it with you ("how was the
    # summer trip?") is related to it, not part of it.
    item_ids = sorted(r.id for r in members)
    participants = _participants(s, item_ids)
    # No group chat or events to say who took part (a trip planned over email and one-on-one texts): then the
    # people in its small conversations are the best evidence we have; the verifier checks each one.
    fallback = not participants
    people = _people(s, item_ids)
    real = relative_members([(p, n) for p, n, _ev in people], set(participants))
    for p, n, ev in people:
        if p not in real:
            continue  # a passing mention next to people who talk about it all the time
        is_part = p in participants or fallback
        props = {"why": participants.get(p) or f"talked about it in {n} of its records", "items": n,
                 "role": "participant" if is_part else "mentioned"}
        if p in participants:
            props["direct"] = True
        w.edge(p, tid, "INVOLVED_IN" if is_part else "RELATED_TO", props, confidence=0.8 if is_part else 0.5,
               evidence=[(sid, "member", {}) for sid in ev or [] if sid])
    for p, why in sorted(participants.items()):  # participants the mention rule didn't find (a quiet chat member)
        if not s.execute(text("SELECT 1 FROM edges WHERE src = :p AND dst = :t AND type = 'INVOLVED_IN'"),
                         {"p": p, "t": tid}).first():
            w.edge(p, tid, "INVOLVED_IN", {"why": why, "role": "participant", "direct": True}, confidence=0.8)
    return tid


def _participants(s: Session, item_ids: list[int]) -> dict[int, str]:
    """{person: why} for the people who took part: members of the thread's main group chat (the group
    conversation with the most of the thread's days, if it holds a real share of them), and attendees of the
    thread's events. Canonical identities; you and services excluded."""
    chat = s.execute(text("""
        SELECT it.dst, count(*) AS days FROM nodes m JOIN edges it ON it.src = m.id AND it.type = 'IN_THREAD'
        JOIN nodes c ON c.id = it.dst AND COALESCE((c.props->>'group')::bool, false)
        WHERE m.source_item_id = ANY(:ids) AND m.type = 'Message'
        GROUP BY it.dst ORDER BY count(*) DESC, it.dst LIMIT 1"""), {"ids": item_ids}).first()
    out: dict[int, str] = {}
    if chat and chat.days >= max(3, 0.1 * len(item_ids)):
        for p in s.execute(text("""
            SELECT DISTINCT COALESCE(c.canonical_id, x.dst) FROM edges it JOIN edges x ON x.src = it.src AND x.type = 'RECEIVED'
            LEFT JOIN canonical c ON c.node_id = x.dst JOIN nodes pn ON pn.id = COALESCE(c.canonical_id, x.dst)
            WHERE it.dst = :chat AND it.type = 'IN_THREAD' AND pn.type = 'Person'
              AND NOT COALESCE((pn.props->>'self')::bool, false) AND COALESCE(pn.props->>'kind', '') <> 'service'
        """), {"chat": chat[0]}).scalars():
            out[p] = f"in the group chat where it was planned ({chat.days} days)"
    for p in s.execute(text("""
        SELECT DISTINCT COALESCE(c.canonical_id, e.src) FROM nodes ev JOIN edges e ON e.dst = ev.id AND e.type = 'ATTENDED'
        LEFT JOIN canonical c ON c.node_id = e.src JOIN nodes pn ON pn.id = COALESCE(c.canonical_id, e.src)
        WHERE ev.type = 'Event' AND ev.source_item_id = ANY(:ids)
          AND NOT COALESCE((pn.props->>'self')::bool, false) AND COALESCE(pn.props->>'kind', '') <> 'service'
    """), {"ids": item_ids}).scalars():
        out.setdefault(p, "attended its events")
    return out


def _candidate_from(row, members: list) -> dict:
    """A registry row's current records, shaped as a candidate for the judge."""
    ts = _by_time(members)
    return {"term": row.terms[0], "terms": list(row.terms), "start": ts[0].occurred_at, "end": ts[-1].occurred_at,
            "items": {r.id: r for r in ts}, "sources": Counter(r.source for r in ts),
            "seed": row.anchor if row.origin == "name" else None}


def _rows(s: Session) -> list:
    return list(s.execute(text("SELECT * FROM thread_registry ORDER BY key")).all())


def _build(w: Writer, row, rows: list, found: dict[str, dict]) -> set[int]:
    """Build one active row's thread from its records and those of its facets (the rows merged into it), and return
    the records it holds (none when it has too few to be built: fewer than 3, or none for a pinned row)."""
    into = {r.key: r.merged_into for r in rows if r.status == "merged" and r.merged_into}
    facets = sorted((r for r in rows if r.status == "merged" and follow(into, r.key) == row.key), key=lambda r: r.key)
    members = dict(found.get(row.key, {}))
    facet_of: dict[int, str] = {}
    for f in facets:
        for iid, it in sorted(found.get(f.key, {}).items()):
            if iid not in members:
                members[iid] = it
                if f.merged_by == "user":
                    facet_of[iid] = f"Project:{f.key}"
    if len(members) < 3 and not (row.pinned and members):
        return set()
    _write_thread(w, row, list(members.values()), facet_of, sorted({f.name for f in facets if f.name != row.name}))
    return set(members)


def run(s: Session, log=print, report=None) -> dict:
    """1. Registry rows find their records by their defining terms (no model call); merged rows as facets of the
       thread they went into. Unjudged rows are judged once their records may be read (budget permitting).
    2. New candidates (name seeds for long-running things, bursts for the rest), skipping records known threads
       cover: judged once (budget permitting); kept ones join the registry, rejected ones are remembered.
    3. Threads no row builds go; then duplicates and related threads (resolve_threads), umbrellas, the hierarchy you
       stated, and the group chats and calendar series that are about a thread.
    `report(count, total, note)` follows each part: the unjudged rows looked at, the new candidates looked at, and
    then what comes after them."""
    from igneous.graph import overrides, resolve_threads

    report = report or (lambda count=None, total=None, note=None: None)

    def step(note: str) -> None:  # a new part of the stage: its note, and no count yet
        report()
        report(note=note)
    items = _items(s)
    items_by_id = {r.id: r for r in items}
    # Only this stage's own links; yours ('user') are not its to delete.
    s.execute(text("DELETE FROM edges WHERE extractor IN (:x, 'hierarchy', 'thread-resolution')"), {"x": EXTRACTOR})
    w = Writer(s, EXTRACTOR, VERSION)
    stats = {"threads": 0, "new": 0, "parts": 0, "rejected": 0, "rejudged": 0, "calls": 0, "waiting": 0}
    yours = overrides.plan(overrides.load(s))
    renamed = {k.removeprefix("Project:") for k in yours.renames}
    kept = {k.removeprefix("Project:") for k in yours.merges.values()}  # threads you merged others into

    # A name thread's window follows its anchor (a series' occurrences, a repo's activity) as the data grows.
    for row in s.execute(text("""SELECT key, anchor, anchor_name FROM thread_registry
                                 WHERE status = 'active' AND origin = 'name' AND anchor IS NOT NULL ORDER BY key""")).all():
        span = _anchor_span(s, {"how": row.anchor, "name": row.anchor_name})
        if span:
            s.execute(text("""UPDATE thread_registry SET span_start = :a, span_end = :b WHERE key = :k
                              AND (span_start, span_end) IS DISTINCT FROM (:a, :b)"""),
                      {"a": span[0], "b": span[1], "k": row.key})

    # 1a. Threads named without the model, judged now that their records may be read (unless you named them).
    unjudged = [r for r in _rows(s) if not r.judged and r.status == "active" and r.key not in renamed]
    if unjudged:
        found = registered(unjudged, items)
        for i, row in enumerate(unjudged, 1):
            report(count=i, total=len(unjudged), note="judging threads named without the model")
            members = list(found.get(row.key, {}).values())
            if not members:
                continue
            c = _candidate_from(row, members)
            verdict, called = _judge(s, c, stats["calls"] < JUDGE_BUDGET)
            stats["calls"] += called
            if verdict is None or not verdict["judged"]:
                continue  # still nothing a model may read, the call failed, or the budget is spent
            terms = [t for t in (verdict.get("defining_terms") or row.terms) if len(t) >= 3] or list(row.terms)
            keep = (bool(verdict.get("keep")) and len(_belongs(c, verdict)) >= 3) or row.key in kept
            s.execute(text("""
                UPDATE thread_registry SET name = :n, kind = :kind, terms = :t, confidence = :c, judged = true,
                    status = :st, updated_at = now() WHERE key = :k
            """), {"k": row.key, "n": (verdict.get("name") or row.name)[:80], "kind": verdict.get("kind"), "t": terms,
                   "c": verdict.get("confidence"), "st": "active" if keep else "rejected"})
            if not keep:  # what was merged into it stands on its own again
                s.execute(text("""UPDATE thread_registry SET status = 'active', merged_into = NULL, updated_at = now()
                                  WHERE status = 'merged' AND merged_into = :k"""), {"k": row.key})
            stats["rejudged"] += 1

    # 1b. Every row's records; a merged row's go to the thread it ends up in.
    rows = _rows(s)
    by_key = {r.key: r for r in rows}
    found = registered([r for r in rows if r.status in ("active", "merged")], items)
    built: list[str] = []
    claimed: set[int] = set()
    for row in sorted((r for r in rows if r.status == "active"), key=lambda r: (not r.pinned, r.key)):
        members = _build(w, row, rows, found)
        if members:
            built.append(row.key)
            claimed |= members
    stats["threads"] = len(built)

    # 2. New candidates: name seeds first (long-running), then bursts; only what known threads don't cover. A kept
    # one is built from its registry row, exactly as every later run builds it. One that is part of a class (its
    # project, its demo day: `_course_part`) is a facet of the class from the start, whose term finds its records for it.
    step("finding new threads")
    courses = [c for key in built if (c := _course(by_key[key]))]
    cands = [c for c in (_seed_candidate(s, sd, items_by_id) for sd in name_seeds(s)) if c]
    cands += candidates(s)
    for i, c in enumerate(cands, 1):
        report(count=i, total=len(cands), note="judging new threads")
        if part := _course_part(c, courses):
            key = thread_key(c)
            if key not in by_key:
                span = c.get("anchor") or (c["start"], c["end"])
                row = SimpleNamespace(key=key, name=_fallback_name(c), kind=None, terms=[c["terms"][0]],
                                      span_start=span[0], span_end=span[1], status="merged", merged_into=part,
                                      origin="name" if c.get("seed") else "burst", confidence=None,
                                      anchor=c.get("seed"), anchor_name=c.get("seed_name"), judged=False,
                                      pinned=False, merged_by=None)
                s.execute(text("""
                    INSERT INTO thread_registry (key, name, kind, terms, span_start, span_end, status, merged_into,
                                                 origin, confidence, anchor, anchor_name, judged)
                    VALUES (:key, :name, :kind, :terms, :span_start, :span_end, :status, :merged_into, :origin,
                            :confidence, :anchor, :anchor_name, :judged)
                """), {k: getattr(row, k) for k in ("key", "name", "kind", "terms", "span_start", "span_end", "status",
                                                    "merged_into", "origin", "confidence", "anchor", "anchor_name",
                                                    "judged")})
                by_key[key] = row
                rows.append(row)
                found[key] = registered([row], items).get(key, {})
                # The class again, with its new part.
                s.execute(text("""DELETE FROM edges e USING nodes t WHERE t.id = e.dst AND t.type = 'Project'
                                  AND t.key = :k AND e.extractor = :x"""), {"k": part, "x": EXTRACTOR})
                claimed |= _build(w, by_key[part], rows, found)
                stats["parts"] += 1
            continue
        fresh = {k: v for k, v in c["items"].items() if k not in claimed}
        if len(fresh) < MIN_ITEMS:
            continue  # already covered by a thread we know
        c = {**c, "items": fresh, "sources": Counter(r.source for r in fresh.values())}
        key = thread_key(c)
        if key in by_key:
            continue  # judged before (kept: built above; rejected: stays rejected)
        verdict, called = _judge(s, c, stats["calls"] < JUDGE_BUDGET)
        stats["calls"] += called
        if verdict is None:
            stats["waiting"] += 1  # the call failed or the budget is spent: judged on a later run
            continue
        terms = [t for t in (verdict.get("defining_terms") or c["terms"]) if len(t) >= 3] or c["terms"]
        span = c.get("anchor") or (c["start"], c["end"])  # a seed's span is its anchor's, as every later run sets it
        row = SimpleNamespace(key=key, name=(verdict.get("name") or c["terms"][0])[:80], kind=verdict.get("kind"),
                              terms=terms, span_start=span[0], span_end=span[1], status="active",
                              origin="name" if c.get("seed") else "burst", confidence=verdict.get("confidence"),
                              anchor=c.get("seed"), anchor_name=c.get("seed_name"), judged=verdict["judged"],
                              pinned=False, merged_into=None, merged_by=None)
        found[key] = registered([row], items).get(key, {})
        keep = bool(verdict.get("keep")) and len(_belongs(c, verdict)) >= 3 and len(found[key]) >= 3
        if not keep and not verdict["judged"]:
            continue  # too small without a model's say: no row, so a model judges it once it may read it
        row.status = "active" if keep else "rejected"
        s.execute(text("""
            INSERT INTO thread_registry (key, name, kind, terms, span_start, span_end, status, origin, confidence,
                                         anchor, anchor_name, judged)
            VALUES (:key, :name, :kind, :terms, :span_start, :span_end, :status, :origin, :confidence, :anchor,
                    :anchor_name, :judged)
        """), {k: getattr(row, k) for k in ("key", "name", "kind", "terms", "span_start", "span_end", "status",
                                            "origin", "confidence", "anchor", "anchor_name", "judged")})
        by_key[key] = row
        rows.append(row)
        if not keep:
            stats["rejected"] += 1
            continue
        claimed |= _build(w, row, rows, found)
        built.append(key)
        courses += [course] if (course := _course(row)) else []  # a class its project and events may be part of
        stats["new"] += 1

    # 3. Threads no row builds any more go (whatever they were); then what sits around the threads.
    s.execute(text("""DELETE FROM nodes WHERE type = 'Project' AND props->>'derived' = 'threads'
                      AND NOT COALESCE((props->>'umbrella')::bool, false) AND NOT (key = ANY(:keep))"""),
              {"keep": built})
    step("resolving duplicate threads")
    stats["resolution"] = resolve_threads.run(s, log=None, writer=w)
    survivors = stats["resolution"].get("survivors") or []
    if survivors:  # built again with their new facets, as every later run builds them
        rows = _rows(s)
        for row in (r for r in rows if r.key in survivors):
            s.execute(text("""DELETE FROM edges e USING nodes t WHERE t.id = e.dst AND t.type = 'Project'
                              AND t.key = :k AND e.extractor = :x"""), {"k": row.key, "x": EXTRACTOR})
            _build(w, row, rows, found)
    w.refresh()  # threads folded into others: a rejection of either holds for the survivor
    w.prune()
    stats["umbrellas"] = umbrellas(s, w)
    stats["stated"] = stated_hierarchy(s)
    stats["groups"] = attach_groups(s, w) + _short_named(s, w)
    stats["skipped_rejected"] = w.skipped
    if log:
        log(json.dumps(stats))
    return stats


def umbrellas(s: Session, w: Writer) -> int:
    """Long-running things several threads sit under: an org, lab, program or club ("the Lab" over Acme Client,
    Project Atlas and the kiosk prototype). The opposite of a thread: persistent (many months, no single burst) and
    concentrated in a group of threads. Proposes thread PART_OF umbrella edges, with the records that name it as
    evidence; the verifier checks each. Umbrellas are upserted by key (a link you drew to one survives the next
    run), and one no longer found goes."""
    from igneous.llm import allowed_sources, nameable

    threads_ = s.execute(text("""
        SELECT t.id FROM nodes t WHERE t.type = 'Project' AND t.props->>'derived' = 'threads'
          AND NOT COALESCE((t.props->>'umbrella')::bool, false) ORDER BY t.id""")).scalars().all()
    found: list[str] = []
    # Terms per thread, weighted by share of the thread's items that carry them.
    share: dict[str, dict[int, float]] = defaultdict(dict)
    for tid in threads_ if len(threads_) >= UMBRELLA_THREADS else []:
        rows = s.execute(text("""
            SELECT coalesce(si.title, '') || ' ' || left(coalesce(si.body_text, ''), 1500)
            FROM edges e JOIN nodes n ON n.id = e.src JOIN source_items si ON si.id = n.source_item_id
            WHERE e.dst = :t AND e.type = 'PART_OF' AND e.extractor = :x ORDER BY si.occurred_at DESC, si.id LIMIT 400
        """), {"t": tid, "x": EXTRACTOR}).scalars().all()
        if not rows:
            continue
        counts = Counter(t for r in rows for t in _terms(r))
        for term, n in counts.items():
            if n / len(rows) >= UMBRELLA_SHARE:
                share[term][tid] = n / len(rows)
    thread_names = {norm(n) for n in s.execute(text("""
        SELECT COALESCE(props->>'original_name', name) FROM nodes WHERE id = ANY(:ids)"""),
        {"ids": list(threads_)}).scalars()}
    # Your orgs (organizations.YOUR_ORGS) already hold their threads (graph/backbone.py): a term that is part of one's
    # name ("Reserve University" in Western Reserve University) is that org, not a new umbrella.
    from igneous.graph.organizations import YOUR_ORGS

    org_names = [f" {norm(n)} " for n in s.execute(text("""
        SELECT o.name FROM edges e JOIN nodes o ON o.id = e.dst
        JOIN nodes me ON me.id = e.src AND COALESCE((me.props->>'self')::bool, false)
        WHERE e.type = 'WORKED_AT' AND e.extractor = ANY(:yours) ORDER BY o.name"""),
        {"yours": list(YOUR_ORGS)}).scalars()]
    sources = allowed_sources()
    names = nameable(s, {t for by_thread in share.values() for t in by_thread}, sources)  # what a prompt may name
    for term in sorted(share):
        by_thread = share[term]
        if len(by_thread) < UMBRELLA_THREADS:
            continue
        # A course is never over other courses (two classes share a prereq's name in their mail), and a term that
        # already names a thread is that thread, not a second thing above its neighbours.
        n = norm(term)
        if course_code(term) or n in thread_names or (len(n.split()) >= 2 and any(f" {n} " in o for o in org_names)):
            continue
        months, shareable = s.execute(text("""
            SELECT count(DISTINCT date_trunc('month', occurred_at)), COALESCE(bool_or(source = ANY(CAST(:src AS text[]))), false)
            FROM source_items WHERE occurred_at IS NOT NULL AND (title ILIKE :q OR body_text ILIKE :q)
        """), {"q": f"%{term}%", "src": sorted(sources)}).one()
        if (months or 0) < UMBRELLA_MONTHS:
            continue
        derived = dict(s.execute(text("SELECT id, COALESCE(props->>'original_name', name) FROM nodes WHERE id = ANY(:i)"),
                                 {"i": list(by_thread)}).all())
        shown = [names[t] for t in sorted(by_thread) if t in names] if shareable else []
        if not _is_umbrella(term, [derived[t] for t in sorted(by_thread) if t in derived], months, shown):
            continue
        key = "umbrella:" + term.lower()
        found.append(key)
        uid = w.project(key, term, {"derived": "threads", "kind": "organization", "umbrella": True, "months": months,
                                    "named_from": "records"})
        for tid, frac in sorted(by_thread.items()):
            # Evidence: the thread's items that name the umbrella (the verifier reads exactly those).
            evidence = s.execute(text("""
                SELECT si.id FROM edges e JOIN nodes n ON n.id = e.src JOIN source_items si ON si.id = n.source_item_id
                WHERE e.dst = :t AND e.type = 'PART_OF' AND e.extractor = :x
                  AND (si.title ILIKE :q OR si.body_text ILIKE :q) ORDER BY si.occurred_at DESC, si.id DESC LIMIT 6
            """), {"t": tid, "x": EXTRACTOR, "q": f"%{term}%"}).scalars().all()
            w.edge(tid, uid, "PART_OF", {"why": f"{round(frac * 100)}% of its records mention {term}"},
                   confidence=round(frac, 3), extractor="hierarchy",
                   evidence=[(sid, "umbrella", {}) for sid in evidence])
    s.execute(text("""DELETE FROM nodes WHERE type = 'Project' AND props->>'derived' = 'threads'
                      AND COALESCE((props->>'umbrella')::bool, false) AND NOT (key = ANY(:keep))"""), {"keep": found})
    return len(found)


def _is_umbrella(term: str, thread_names: list[str], months: int, shown: list[str]) -> bool:
    """Is `term` the name of an organization, lab, program, club or team these threads belong to? Boilerplate
    shared across threads ("Zoom", "Best", "Eastern Time") isn't. Cached by the question; without the model, not
    accepted. `shown` is what the model may read (llm.nameable): the threads among them a prompt may name, and nothing
    at all when no opted-in record mentions the term."""
    from igneous.llm import LLMDisabled, cache_lookup, cache_store, structured

    def prompt_with(names: list[str]) -> str:
        return f"Term: {term}\nPresent across {months} months.\nThreads whose records often mention it:\n" + \
            "\n".join(f"- {n}" for n in sorted(names)[:20])
    key = "threads-umbrella:v1:" + hashlib.sha256(prompt_with(thread_names).encode()).hexdigest()
    cached = cache_lookup(key)
    if cached is None:
        if not settings.llm_enabled or not shown:
            return False
        try:
            cached, _ = structured(
                "Decide whether a term is the name of an organization, lab, program, club, team or company that "
                "the listed projects/threads belong to (e.g. a university lab running several client projects). "
                "Generic words, tools used everywhere (Zoom, Canvas), places, times, people's names and email "
                "boilerplate are NOT. Answer only from the term and the thread names.",
                prompt_with(shown),
                {"type": "object", "additionalProperties": False, "required": ["umbrella", "confidence"],
                 "properties": {"umbrella": {"type": "boolean"}, "confidence": {"type": "number"}}},
                max_tokens=120, model=settings.llm_cheap_model)
        except LLMDisabled:
            return False
        except Exception:  # not answered: asked again next run
            return False
        cache_store(key, cached)
    return bool(cached.get("umbrella")) and (cached.get("confidence") or 0) >= 0.7


def stated_hierarchy(s: Session) -> int:
    """The Project hierarchy someone stated (thread_parents: you through POST /graph/link, or a gold set you
    pinned), as PART_OF links with extractor 'user', on the Projects as they are now (one you folded away: the one it
    went into). Written as the threads are built, so the stages after this one see it; the last step writes it
    again."""
    from igneous.graph.derived import merges
    from igneous.graph.overrides import stated_parents as rows

    folded = merges(s)
    pairs = [(follow(folded, c), follow(folded, p)) for c, p in rows(s)]
    ids = dict(s.execute(text("SELECT type || ':' || key, id FROM nodes WHERE type || ':' || key = ANY(:k)"),
                         {"k": sorted({k for pair in pairs for k in pair})}).all())
    n = 0
    for c, p in pairs:
        if c in ids and p in ids and ids[c] != ids[p]:
            s.execute(text("""
                INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
                VALUES (:c, :p, 'PART_OF', '{"why": "you said so"}', 'user', :v, 1.0)
                ON CONFLICT (src, dst, type, extractor) DO NOTHING"""), {"c": ids[c], "p": ids[p], "v": VERSION})
            n += 1
    return n


def _group_evidence(s: Session, group: int, term: str | None, start, end) -> list[int]:
    """The records of a group chat or a calendar series that show it is about a thread: those naming `term` (when
    given) inside the thread's span, the newest first."""
    return list(s.execute(text("""
        SELECT si.id FROM nodes g
        JOIN source_items si ON si.id IN (
            SELECT m.source_item_id FROM edges it JOIN nodes m ON m.id = it.src WHERE it.dst = g.id AND it.type = 'IN_THREAD'
            UNION SELECT g.source_item_id
            UNION SELECT o.id FROM source_items o WHERE g.type = 'Event' AND o.source = 'gcal'
                                                    AND o.raw_meta->>'event_key' = g.key)
        WHERE g.id = :g AND (CAST(:term AS text) IS NULL OR si.title ILIKE :q OR si.body_text ILIKE :q)
          AND (CAST(:a AS timestamptz) IS NULL OR si.occurred_at BETWEEN CAST(:a AS timestamptz) - interval '30 days'
                                                                     AND CAST(:b AS timestamptz) + interval '30 days')
        ORDER BY si.occurred_at DESC NULLS LAST, si.id DESC LIMIT 6
    """), {"g": group, "term": term, "q": f"%{term}%", "a": start, "b": end}).scalars())


def _short_named(s: Session, w: Writer) -> int:
    """A chat named by a short or ambiguous token ("395": the class group chat) can't be matched by name alone, since
    "401" is CS 401 and EE 401 alike. Among the threads whose terms contain that token, the chat goes to the one
    it fits best (the share of its active days inside the thread's span, plus people they share), and only when
    that one clearly fits better than the others. The verifier then checks the link against the chat's days in it."""
    chats = s.execute(text("""
        SELECT g.id, lower(regexp_replace(g.name, '[^A-Za-z0-9]+', '', 'g')) AS tok,
               array_agg(DISTINCT si.occurred_at::date) AS days,
               array_remove(array_agg(DISTINCT COALESCE(c.canonical_id, x.dst)), NULL) AS people
        FROM nodes g JOIN edges it ON it.dst = g.id AND it.type = 'IN_THREAD'
        JOIN nodes m ON m.id = it.src JOIN source_items si ON si.id = m.source_item_id
        LEFT JOIN edges x ON x.src = m.id AND x.type = 'RECEIVED' LEFT JOIN canonical c ON c.node_id = x.dst
        WHERE g.type = 'Thread' AND COALESCE((g.props->>'group')::bool, false)
          AND NOT COALESCE((g.props->>'hidden')::bool, false)
          AND length(regexp_replace(g.name, '[^A-Za-z0-9]+', '', 'g')) BETWEEN 2 AND 5
        GROUP BY g.id ORDER BY g.id""")).mappings().all()
    if not chats:
        return 0
    threads_ = s.execute(text("""
        SELECT p.id, r.terms, (p.props->>'start')::date AS a, (p.props->>'end')::date AS b,
               array_remove(array_agg(DISTINCT COALESCE(c.canonical_id, e.src)), NULL) AS people
        FROM nodes p JOIN thread_registry r ON r.key = p.key
        LEFT JOIN edges e ON e.dst = p.id AND e.type IN ('INVOLVED_IN', 'RELATED_TO')
        LEFT JOIN canonical c ON c.node_id = e.src
        WHERE p.type = 'Project' AND r.status = 'active' AND p.props ? 'start' GROUP BY p.id, r.terms
        ORDER BY p.id""")).mappings().all()
    n = 0
    for ch in chats:
        fits = []
        for t in threads_:
            tokens = {w_ for term in t["terms"] or [] for w_ in re.findall(r"[a-z0-9]+", term.lower())}
            if ch["tok"] not in tokens:
                continue
            inside = sum(1 for d in ch["days"] if t["a"] <= d <= t["b"]) / max(1, len(ch["days"]))
            shared = len(set(ch["people"]) & set(t["people"] or []))
            fits.append((inside + 0.25 * min(shared, 4), shared, inside, t["id"], t["a"], t["b"]))
        fits.sort(reverse=True)
        if not fits or fits[0][2] < 0.5 or (len(fits) > 1 and fits[0][0] - fits[1][0] < 0.25):
            continue  # it didn't happen during any of them, or two fit about as well: no guess
        score, shared, inside, tid, a, b = fits[0]
        evidence = _group_evidence(s, ch["id"], None, a, b)
        if w.edge(ch["id"], tid, "PART_OF",
                  {"why": f"named like it; {round(inside * 100)}% of its activity was during it"
                          + (f", {shared} of its people are in it" if shared else "")},
                  confidence=round(min(0.9, 0.4 + score / 3), 2), evidence=[(sid, "group", {}) for sid in evidence]):
            n += 1
    return n


def attach_groups(s: Session, w: Writer) -> int:
    """A named group chat or a recurring event is often *about* a thread: three chats called "Maple St" and
    "Maple St party planning" belong to the shared house. Propose chat/event PART_OF thread when its name contains
    one of the thread's defining terms, with its records that name the term during the thread as evidence; the
    verifier then checks each link."""
    threads_ = s.execute(text("""
        SELECT p.id, r.terms, (p.props->>'start')::timestamptz AS a, (p.props->>'end')::timestamptz AS b
        FROM nodes p JOIN thread_registry r ON r.key = p.key
        WHERE p.type = 'Project' AND r.status = 'active' ORDER BY p.id""")).all()
    n = 0
    for tid, terms, a, b in threads_:
        for term in sorted({t for t in (terms or []) if len(t) >= 4}):
            rows = s.execute(text("""
                SELECT g.id FROM nodes g
                WHERE ((g.type = 'Thread' AND COALESCE((g.props->>'group')::bool, false)
                        AND NOT COALESCE((g.props->>'hidden')::bool, false))
                       OR g.type = 'Event')
                  AND g.name ILIKE :q
                  AND NOT EXISTS (SELECT 1 FROM edges x WHERE x.src = g.id AND x.dst = :t AND x.type = 'PART_OF')
                ORDER BY g.id LIMIT 20"""), {"q": f"%{term}%", "t": tid}).scalars().all()
            for gid in rows:
                evidence = _group_evidence(s, gid, term, a, b)
                if w.edge(gid, tid, "PART_OF", {"why": f"named after {term}"}, confidence=0.7,
                          evidence=[(sid, "group", {}) for sid in evidence]):
                    n += 1
    return n
