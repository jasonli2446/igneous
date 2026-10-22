"""Life threads: the trips, classes, jobs, hobbies and projects that no single record names.

A weekend trip is a flight email, an itinerary doc, browsing, a calendar block and a group chat, and nothing
labels all of them. What they share is a distinctive term that suddenly shows up in several kinds of source at
once. So a thread is found, not configured:

  1. Terms: capitalized phrases from item titles and text ("Portland", "Powell's", "CS 401", "Gloomhaven Night").
  2. Burst: a term that appears in 3+ distinct sources within a <= BURST_DAYS window, and is not everywhere all
     the time (document-frequency cap), is a candidate. The densest window is its span.
  3. Members: items in the window that mention the term; people involved in 2+ of them.
  4. Overlapping candidates (same window, mostly the same items) merge into one.
  5. Judge: with the model enabled and sources opted in (IGNEOUS_LLM_SOURCES), one cached structured call per
     candidate decides keep / kind (trip, class, job, hobby, project, ... free text) / the name the user would
     use, from titles and short snippets of opted-in sources only (counts for the rest). Without it, a thread
     is named by its term and month ("Portland · Aug 2026"), kind "thread". Low confidence is kept as a
     suggestion (props.suggested), never silently promoted.

Writes `Project` nodes with props.kind and props.derived='threads', PART_OF (item -> thread) and INVOLVED_IN
(person -> thread) with edge_evidence. Rebuilt from scratch each run, so new mail joins old threads.
"""
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings

EXTRACTOR = "threads"
VERSION = "1"
BURST_DAYS = 60          # a thread's mentions cluster inside this many days
MIN_SOURCES = 3          # ...across at least this many kinds of source (mail, chat, calendar, files, ...)
MIN_ITEMS = 6
MAX_DF = 0.02            # a term in more than 2% of all items is background (a hometown, a school), not a thread
MAX_CANDIDATES = 80      # judged per run (model budget)
MIN_CONFIDENCE = 0.55    # below this, a judged thread is a suggestion, not a node
UMBRELLA_MONTHS = 6      # an umbrella (org, program, lab) is present across at least this many months...
UMBRELLA_SHARE = 0.2     # ...and makes up at least this share of the items of...
UMBRELLA_THREADS = 2     # ...at least this many threads

# Sources that describe the user's life (not contact cards or raw metadata).
SOURCES = ("gmail", "email", "imessage", "gcal", "drive", "fs", "notes", "ai_chats", "github",
           "takeout_activity", "takeout_chrome", "takeout_tasks", "takeout_meet", "takeout_chat")

COURSE_CODE = re.compile(r"[A-Za-z]{2,5}\s?\d{3}[A-Za-z]?")
# Capitalized phrases (1-3 words), or course-style codes ("CS 401").
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


def _terms(text_: str) -> set[str]:
    out = set()
    for m in TERM.findall(text_ or ""):
        words = m.split()
        while words and words[0] in STOP:
            words = words[1:]
        while words and words[-1] in STOP:
            words = words[:-1]
        if words and not (len(words) == 1 and len(words[0]) < 4):
            out.add(" ".join(words))
    return out


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


def candidates(s: Session) -> list[dict]:
    """Terms that spike across several kinds of source in one window, with their member items."""
    items = _items(s)
    if not items:
        return []
    by_term: dict[str, list] = defaultdict(list)
    for it in items:
        # Names of trips, classes and projects show up in titles and opening lines; the first 1,500 characters
        # of a body are enough and keep this fast on a large mailbox.
        for t in _terms(it.title) | _terms(it.body[:1500]):
            by_term[t].append(it)
    people = {r[0] for r in s.execute(text(
        "SELECT lower(name) FROM nodes WHERE type = 'Person' AND name ~ ' '"))}
    # The owner's own name words ("Sam", "Lee") are on everything they write: never a thread on their own.
    people |= {w.lower() for (n,) in s.execute(text("""
        SELECT n.name FROM nodes n WHERE n.type = 'Person' AND (COALESCE((n.props->>'self')::bool, false)
           OR n.id IN (SELECT c.node_id FROM canonical c JOIN nodes h ON h.id = c.canonical_id
                       WHERE COALESCE((h.props->>'self')::bool, false)))""")) for w in (n or "").split() if "@" not in w}
    n_items = len(items)
    out = []
    for term, its in by_term.items():
        if len(its) < MIN_ITEMS or len(its) > MAX_DF * n_items or term.lower() in people:
            continue
        its.sort(key=lambda r: r.occurred_at)
        for lo, hi in _bursts(its):
            win = its[lo:hi + 1]
            out.append({"term": term, "start": win[0].occurred_at, "end": win[-1].occurred_at,
                        "items": {r.id: r for r in win}, "sources": Counter(r.source for r in win)})
    # Repeating terms are kept (a weekly game repeats); whether a candidate names a real thing is the judge's
    # call, measured against the gold set in eval/threads_gold.json rather than tuned by hand.
    return _merge(out)


def _merge(cands: list[dict]) -> list[dict]:
    """Candidates over mostly the same items in overlapping windows are one thread ("Portland" + "Powell's")."""
    cands.sort(key=lambda c: (-len(c["sources"]), -len(c["items"])))
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
            merged.append({**c, "terms": [c["term"]]})
    return merged[:MAX_CANDIDATES]


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
        GROUP BY 1 HAVING count(DISTINCT ok.m) >= 2 ORDER BY 2 DESC LIMIT 30
    """), {"ids": item_ids}).all()


def _sample(items: list, n: int = 40) -> list:
    """Spread across the window and the sources, so the judge sees the whole candidate, not its first days."""
    items = sorted(items, key=lambda r: r.occurred_at)
    if len(items) <= n:
        return items
    by_src: dict[str, list] = defaultdict(list)
    for r in items:
        by_src[r.source].append(r)
    out: list = []
    while len(out) < n and any(by_src.values()):
        for src in list(by_src):
            lst = by_src[src]
            if lst:
                out.append(lst.pop(len(lst) // 2 if len(out) % 2 else 0))
    return sorted(out[:n], key=lambda r: r.occurred_at)


def _judge(s: Session, c: dict) -> dict:
    """Decide what the candidate is, which of its records belong, and which words define it. Model only with
    opted-in sources; cached per candidate content. Fallback (no model): the seed term defines it."""
    from igneous.graph.extract import allowed_sources
    from igneous.llm import LLMDisabled, structured

    allowed = allowed_sources()
    month = c["start"].strftime("%b %Y") if c["start"].strftime("%b %Y") == c["end"].strftime("%b %Y") \
        else f"{c['start']:%b}-{c['end']:%b %Y}"
    fallback = {"keep": True, "kind": "thread", "name": f"{c['terms'][0]} · {month}", "confidence": 0.6,
                "judged": False, "defining_terms": [c["terms"][0]], "members": None}
    shown = _sample([r for r in c["items"].values() if r.source in allowed])
    if not settings.llm_enabled or not shown:
        return fallback
    lines = [f"{i}. [{r.source}, {r.occurred_at:%Y-%m-%d}] {r.title[:120]} :: {re.sub(r'\\s+', ' ', r.body)[:240]}"
             for i, r in enumerate(shown)]
    others = {src: n for src, n in c["sources"].items() if src not in allowed}
    prompt = (f"Shared terms: {', '.join(c['terms'][:8])}\nWindow: {c['start']:%Y-%m-%d} to {c['end']:%Y-%m-%d}"
              f" ({len(c['items'])} records; a sample is listed)\nRecords:\n" + "\n".join(lines) +
              (f"\nAlso, records not shown (counts only): {json.dumps(others)}" if others else ""))
    key = "threads-judge:v2:" + hashlib.sha256(prompt.encode()).hexdigest()
    cached = s.execute(text("SELECT response FROM llm_cache WHERE cache_key = :k"), {"k": key}).scalar()
    if cached is None:
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
            return fallback
        except Exception:  # a failed judgment skips this candidate rather than stopping the run
            return {**fallback, "keep": False}
        s.execute(text("INSERT INTO llm_cache (cache_key, response) VALUES (:k, CAST(:r AS jsonb)) "
                       "ON CONFLICT (cache_key) DO NOTHING"), {"k": key, "r": json.dumps(cached)})
    judged_ids = [shown[i].id for i in cached.get("members", []) if 0 <= i < len(shown)]
    return {**cached, "judged": True, "members": judged_ids, "shown": [r.id for r in shown]}


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


def _edge(s: Session, src: int, dst: int, type_: str, props: dict, sid: int | None, conf: float) -> int:
    eid = s.execute(text("""
        INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence, source_item_id)
        VALUES (:src, :dst, :t, CAST(:p AS jsonb), :x, :v, :c, :sid)
        ON CONFLICT (src, dst, type, extractor) DO UPDATE SET props = EXCLUDED.props RETURNING id
    """), {"src": src, "dst": dst, "t": type_, "p": json.dumps(props), "x": EXTRACTOR, "v": VERSION, "c": conf,
           "sid": sid}).scalar_one()
    if sid is not None:
        s.execute(text("INSERT INTO edge_evidence (edge_id, source_item_id, kind, detail) "
                       "VALUES (:e, :sid, :k, CAST(:d AS jsonb))"),
                  {"e": eid, "sid": sid, "k": props.get("why", "thread"), "d": json.dumps(props)})
    return eid


def ensure_registry(s: Session) -> None:
    """Threads that have been found are kept as identities, so later runs attach new records to them instead of
    re-deriving (and re-rolling) everything from scratch."""
    s.execute(text("""
        CREATE TABLE IF NOT EXISTS thread_registry (
            key        text PRIMARY KEY,
            name       text NOT NULL,
            kind       text,
            terms      text[] NOT NULL,          -- the words that define it (match = membership)
            span_start timestamptz,
            span_end   timestamptz,
            status     text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'rejected', 'merged')),
            merged_into text,                         -- for status 'merged': the surviving thread's key
            pinned     boolean NOT NULL DEFAULT false,   -- gold set or a user correction: never dropped
            origin     text NOT NULL DEFAULT 'burst',    -- burst | name | gold | user
            confidence real,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )"""))


def _migrate_registry(s: Session) -> None:
    """Older registries: allow status 'merged' and the merged_into column."""
    s.execute(text("ALTER TABLE thread_registry ADD COLUMN IF NOT EXISTS merged_into text"))
    s.execute(text("ALTER TABLE thread_registry ADD COLUMN IF NOT EXISTS anchor text"))       # series|repo|chat|course
    s.execute(text("ALTER TABLE thread_registry ADD COLUMN IF NOT EXISTS anchor_name text"))  # the seed's own name
    s.execute(text("ALTER TABLE thread_registry DROP CONSTRAINT IF EXISTS thread_registry_status_check"))
    s.execute(text("ALTER TABLE thread_registry ADD CONSTRAINT thread_registry_status_check "
                   "CHECK (status IN ('active', 'rejected', 'merged'))"))


def name_seeds(s: Session) -> list[dict]:
    """Long-running things named outright: repositories, course codes, recurring calendar series, named group
    chats. A burst finds a trip; a name finds a semester-long course or a year-long project."""
    seeds: list[dict] = []
    rows = s.execute(text(r"""
        SELECT 'repo' AS how, lower(raw_meta->>'repo') AS name FROM source_items
        WHERE source IN ('github', 'ai_chats', 'fs') AND coalesce(raw_meta->>'repo', '') <> ''
        GROUP BY 2 HAVING count(*) >= 5
        UNION ALL
        SELECT 'course', upper(regexp_replace(substring(title from '([A-Za-z]{2,5} ?[0-9]{3})'), '\s', ' ', 'g'))
        FROM source_items WHERE source IN ('gcal', 'gmail') AND title ~ '[A-Za-z]{2,5} ?[0-9]{3}'
        GROUP BY 2 HAVING count(*) >= 5
        UNION ALL
        SELECT 'series', min(title) FROM source_items WHERE source = 'gcal' AND coalesce(title, '') <> ''
        GROUP BY raw_meta->>'event_key' HAVING count(*) >= 4
        UNION ALL
        SELECT 'chat', name FROM nodes WHERE type = 'Thread' AND COALESCE((props->>'group')::bool, false)
          AND name !~ '[0-9]{4}' AND name !~ ',' AND length(name) >= 3
    """)).all()
    seen = set()
    for how, name in rows:
        n = (name or "").strip()
        if len(n) < 3 or n.lower() in seen:
            continue
        seen.add(n.lower())
        seeds.append({"how": how, "name": n, "term": n.replace("-", " ") if how == "repo" else n})
    return seeds


_PAD = timedelta(days=30)  # around an anchor's own dates: planning before, photos and follow-ups after


def _anchor_span(s: Session, seed: dict):
    """When the seed's own anchor was active: (first, last), or None for anchors without dates of their own."""
    name = seed["name"]
    if seed["how"] == "series":
        q = "SELECT min(occurred_at), max(occurred_at) FROM source_items WHERE source = 'gcal' AND title = :n"
    elif seed["how"] == "repo":
        q = ("SELECT min(occurred_at), max(occurred_at) FROM source_items WHERE source IN ('github', 'ai_chats', 'fs') "
             "AND lower(raw_meta->>'repo') = lower(:n)")
    elif seed["how"] == "chat":
        q = ("SELECT min(si.occurred_at), max(si.occurred_at) FROM nodes t JOIN edges it ON it.dst = t.id AND it.type = 'IN_THREAD' "
             "JOIN nodes m ON m.id = it.src JOIN source_items si ON si.id = m.source_item_id WHERE t.type = 'Thread' AND t.name = :n")
    else:
        return None  # course codes and the like: their mentions define them
    row = s.execute(text(q), {"n": name}).first()
    return (row[0], row[1]) if row and row[0] else None


def _seed_candidate(s: Session, seed: dict, items_by_id: dict) -> dict | None:
    """Records naming a seed (in title or text), across all time: a long-running candidate."""
    term = seed["term"]
    variants = {term, term.replace(" ", "-"), term.replace(" ", "")}
    q = " OR ".join(f"(si.title ILIKE :v{i} OR left(si.body_text, 4000) ILIKE :v{i})" for i in range(len(variants)))
    ids = s.execute(text(f"SELECT si.id FROM source_items si WHERE si.source = ANY(:src) AND ({q}) LIMIT 3000"),
                    {"src": list(SOURCES), **{f"v{i}": f"%{v}%" for i, v in enumerate(variants)}}).scalars().all()
    items = {i: items_by_id[i] for i in ids if i in items_by_id}
    # The thread lives when its anchor does: a calendar series' occurrences, a repo's activity, a chat's days.
    # A one-time hangout is its evening, not every later text naming the neighborhood.
    anchor = _anchor_span(s, seed)
    if anchor:
        lo, hi = anchor
        items = {k: v for k, v in items.items() if lo - _PAD <= v.occurred_at <= hi + _PAD}
    if len(items) < MIN_ITEMS or len({r.source for r in items.values()}) < 2:
        return None
    ts = sorted(r.occurred_at for r in items.values())
    return {"term": term, "terms": [term], "start": ts[0], "end": ts[-1], "items": items,
            "sources": Counter(r.source for r in items.values()), "seed": seed["how"], "seed_name": seed["name"],
            "anchor": anchor}


def _registered_all(rows, items: list) -> dict[str, dict]:
    """Every known thread's records now, in one pass over the items: those naming one of its defining terms,
    inside its span (+/- 30 days) for burst threads. Deterministic, which is what keeps a found thread found."""
    from datetime import timedelta as _td

    specs = []
    for row in rows:
        # A bare number ("395") is shared by different courses, rooms and years: never a defining term alone.
        terms = [t.lower() for t in (row.terms or []) if len(t) >= 3 and not t.strip().isdigit()]
        if not terms:
            continue
        lo = hi = None
        if row.span_start and row.span_end and row.origin in ("burst", "name"):
            lo, hi = row.span_start - _td(days=30), row.span_end + _td(days=30)
        specs.append((row.key, terms, lo, hi))
    # Whole-word matching: a short defining term ("eta") must not match inside other words ("beta", "theta").
    # A course code matches with or without its space ("CS 401" / "cs401"), and only with its department.
    def pat(t: str) -> str:
        m = re.fullmatch(r"([a-z]{2,5})\s?(\d{3})", t)
        return rf"{m.group(1)}\s?{m.group(2)}" if m else re.escape(t)
    patterns = {key: re.compile(r"(?<![a-z0-9])(" + "|".join(pat(t) for t in terms) + r")(?![a-z0-9])")
                for key, terms, _lo, _hi in specs}
    out: dict[str, dict] = {k: {} for k, *_ in specs}
    for it in items:
        hay = (it.title + " " + it.body).lower()
        for key, _terms, lo, hi in specs:
            if lo is not None and not (lo <= it.occurred_at <= hi):
                continue
            if patterns[key].search(hay):
                out[key][it.id] = it
    return out


def _write_thread(s: Session, key: str, name: str, kind: str | None, terms: list[str], members: list,
                  extra: dict) -> int:
    span = sorted(r.occurred_at for r in members)
    props = {"derived": "threads", "kind": kind or "thread", "terms": terms[:6],
             "start": span[0].isoformat(), "end": span[-1].isoformat(),
             "sources": dict(Counter(r.source for r in members)), "records": len(members), **extra}
    tid = s.execute(text("""
        INSERT INTO nodes (type, key, name, props) VALUES ('Project', :k, :n, CAST(:p AS jsonb))
        ON CONFLICT (type, key) DO UPDATE SET
            name = CASE WHEN COALESCE((nodes.props->>'renamed')::bool, false) THEN nodes.name ELSE EXCLUDED.name END,
            props = nodes.props || EXCLUDED.props
        RETURNING id
    """), {"k": key, "n": name[:80], "p": json.dumps(props)}).scalar_one()
    for it in members:
        if it.node_id:
            _edge(s, it.node_id, tid, "PART_OF", {"why": f"mentions {terms[0]}"}, it.id, 0.6)
    # Who took part comes from the strongest evidence: the thread's own group chat (the chat that most of its
    # conversation happened in) and the attendees of its events. Someone who only talked about it with you ("how
    # was the summer trip?") is related to it, not part of it.
    participants = _participants(s, tid, [r.id for r in members])
    # No group chat or events to say who took part (a trip planned over email and one-on-one texts): then the
    # people in its small conversations are the best evidence we have; the verifier checks each one.
    fallback = not participants
    people = _people(s, [r.id for r in members])
    real = relative_members([(p, n) for p, n, _ev in people], set(participants))
    for p, n, ev in people:
        if p not in real:
            continue  # a passing mention next to people who talk about it all the time
        is_part = p in participants or fallback
        eid = _edge(s, p, tid, "INVOLVED_IN" if is_part else "RELATED_TO",
                    {"why": participants.get(p) or f"talked about it in {n} of its records", "items": n,
                     "role": "participant" if is_part else "mentioned"}, None, 0.8 if is_part else 0.5)
        for sid in ev or []:
            if sid:
                s.execute(text("INSERT INTO edge_evidence (edge_id, source_item_id, kind, detail) "
                               "VALUES (:e, :sid, 'member', '{}')"), {"e": eid, "sid": sid})
    for p, why in participants.items():  # participants the mention rule didn't find (e.g. a quiet chat member)
        if not s.execute(text("SELECT 1 FROM edges WHERE src = :p AND dst = :t AND type = 'INVOLVED_IN'"),
                         {"p": p, "t": tid}).first():
            _edge(s, p, tid, "INVOLVED_IN", {"why": why, "role": "participant"}, None, 0.8)
    return tid


def _participants(s: Session, tid: int, item_ids: list[int]) -> dict[int, str]:
    """{person: why} for the people who took part: members of the thread's main group chat (the group
    conversation with the most of the thread's days, if it holds a real share of them), and attendees of the
    thread's events. Canonical identities; you and services excluded."""
    chat = s.execute(text("""
        SELECT it.dst, count(*) AS days FROM nodes m JOIN edges it ON it.src = m.id AND it.type = 'IN_THREAD'
        JOIN nodes c ON c.id = it.dst AND COALESCE((c.props->>'group')::bool, false)
        WHERE m.source_item_id = ANY(:ids) AND m.type = 'Message'
        GROUP BY it.dst ORDER BY count(*) DESC LIMIT 1"""), {"ids": item_ids}).first()
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


def run(s: Session, log=print) -> int:
    """1. Known threads (registry): members recomputed from their defining terms, no model call.
    2. New candidates (name seeds for long-running things, bursts for the rest), skipping records known threads
       already cover: judged once; kept ones join the registry, rejected ones are remembered.
    3. Resolve duplicates / link related threads; umbrellas over them. Pinned threads are never dropped."""
    from igneous.graph import overrides

    ensure_registry(s)
    _migrate_registry(s)
    items = _items(s)
    items_by_id = {r.id: r for r in items}
    # Your stated links ('user') are never deleted: viewers mid-rebuild must not see the Lab lose its projects.
    s.execute(text("DELETE FROM edges WHERE extractor IN (:x, 'hierarchy', 'thread-resolution')"), {"x": EXTRACTOR})
    keep_keys: list[str] = []
    claimed: set[int] = set()
    built = 0

    # 1. Known threads.
    # A name thread's window follows its anchor (a series' occurrences, a repo's activity) as the data grows.
    for row in s.execute(text("""SELECT key, anchor, anchor_name FROM thread_registry
                                 WHERE status = 'active' AND origin = 'name' AND anchor IS NOT NULL""")).all():
        span = _anchor_span(s, {"how": row.anchor, "name": row.anchor_name})
        if span:
            s.execute(text("UPDATE thread_registry SET span_start = :a, span_end = :b WHERE key = :k"),
                      {"a": span[0], "b": span[1], "k": row.key})
    s.commit()
    registry = s.execute(text("SELECT * FROM thread_registry WHERE status = 'active' ORDER BY pinned DESC, key")).all()
    members_by_key = _registered_all(registry, items)
    for row in registry:
        members = list(members_by_key.get(row.key, {}).values())
        if len(members) < 3 and not row.pinned:
            continue
        if not members:
            continue
        _write_thread(s, row.key, row.name, row.kind, list(row.terms), members, {"origin": row.origin,
                      "pinned": row.pinned, "confidence": row.confidence})
        keep_keys.append(row.key)
        claimed |= {r.id for r in members}
        built += 1
    known = {r[0] for r in s.execute(text("SELECT key FROM thread_registry"))}

    # 2. New candidates: name seeds first (long-running), then bursts; only what known threads don't cover.
    cands = [c for c in (_seed_candidate(s, sd, items_by_id) for sd in name_seeds(s)) if c]
    cands += candidates(s)
    for c in cands:
        fresh = {k: v for k, v in c["items"].items() if k not in claimed}
        if len(fresh) < MIN_ITEMS:
            continue  # already covered by a thread we know
        c = {**c, "items": fresh, "sources": Counter(r.source for r in fresh.values())}
        key = "thread:" + hashlib.sha256((c["terms"][0].lower() + ("" if c.get("seed") else c["start"].strftime("%Y-%m"))).encode()).hexdigest()[:16]
        if key in known:
            continue  # judged before (kept -> handled above; rejected -> stays rejected)
        verdict = _judge(s, c)
        terms = [t for t in (verdict.get("defining_terms") or c["terms"]) if len(t) >= 3] or c["terms"]
        s.execute(text("""
            INSERT INTO thread_registry (key, name, kind, terms, span_start, span_end, status, origin, confidence,
                                         anchor, anchor_name)
            VALUES (:k, :n, :kind, :t, :a, :b, :st, :o, :c, :anc, :an) ON CONFLICT (key) DO NOTHING
        """), {"k": key, "n": (verdict.get("name") or c["terms"][0])[:80], "kind": verdict.get("kind"), "t": terms,
               "a": c["start"], "b": c["end"], "st": "active" if verdict.get("keep") else "rejected",
               "o": "name" if c.get("seed") else "burst", "c": verdict.get("confidence"),
               "anc": c.get("seed"), "an": c.get("seed_name")})
        known.add(key)
        if not verdict.get("keep"):
            continue
        members = _belongs(c, verdict)
        if len(members) < 3:
            s.execute(text("UPDATE thread_registry SET status = 'rejected' WHERE key = :k"), {"k": key})
            continue
        _write_thread(s, key, verdict["name"], verdict.get("kind"), terms, members,
                      {"origin": "name" if c.get("seed") else "burst", "judged": verdict.get("judged", False),
                       "confidence": verdict.get("confidence"),
                       "suggested": (verdict.get("confidence") or 0) < MIN_CONFIDENCE})
        keep_keys.append(key)
        claimed |= {r.id for r in members}
        built += 1
        s.commit()

    # 3. Clean up, resolve duplicates / related, umbrellas. Pinned threads are never dropped.
    s.execute(text("""DELETE FROM nodes WHERE type = 'Project' AND props->>'derived' = 'threads'
                      AND NOT COALESCE((props->>'umbrella')::bool, false) AND NOT (key = ANY(:keep))
                      AND NOT COALESCE((props->>'pinned')::bool, false)"""), {"keep": keep_keys})
    s.commit()
    from igneous.graph import resolve_threads
    resolve_threads.run(s, log=None)
    keep_keys[:] = s.execute(text("SELECT key FROM nodes WHERE type = 'Project' AND props->>'derived' = 'threads'")).scalars().all()
    s.execute(text("""DELETE FROM nodes WHERE type = 'Project' AND COALESCE((props->>'umbrella')::bool, false)
                      AND NOT COALESCE((props->>'pinned')::bool, false)"""))
    keep_keys[:] = [k for k in keep_keys if not k.startswith("umbrella:")]
    built += umbrellas(s, keep_keys)
    pinned_parents(s)  # stated structure (the Lab over its projects) always holds
    _short_named(s)    # chats named by an ambiguous token ("395") go to the thread they fit, if one clearly does
    attach_groups(s)   # named group chats / event series that belong to a thread (the Maple St chats -> the shared house)
    s.execute(text("""DELETE FROM nodes WHERE type = 'Project' AND props->>'derived' = 'threads'
                      AND NOT (key = ANY(:keep)) AND NOT COALESCE((props->>'pinned')::bool, false)"""),
              {"keep": keep_keys})
    overrides.apply(s)
    s.commit()
    return built


def umbrellas(s: Session, keep_keys: list[str]) -> int:
    """Long-running things several threads sit under: an org, lab, program or club ("the Lab" over Acme Client,
    Project Atlas and the kiosk prototype). The opposite of a thread: persistent (many months, no single burst) and
    concentrated in a group of threads. Proposes thread PART_OF umbrella edges; the verifier checks each."""
    threads_ = s.execute(text("""
        SELECT t.id, t.key FROM nodes t WHERE t.type = 'Project' AND t.props->>'derived' = 'threads'
          AND t.key = ANY(:keep)"""), {"keep": keep_keys}).all()
    if len(threads_) < UMBRELLA_THREADS:
        return 0
    # Terms per thread, weighted by share of the thread's items that carry them.
    share: dict[str, dict[int, float]] = defaultdict(dict)
    for tid, _key in threads_:
        rows = s.execute(text("""
            SELECT coalesce(si.title, '') || ' ' || left(coalesce(si.body_text, ''), 1500)
            FROM edges e JOIN nodes n ON n.id = e.src JOIN source_items si ON si.id = n.source_item_id
            WHERE e.dst = :t AND e.type = 'PART_OF' AND e.extractor = :x LIMIT 400
        """), {"t": tid, "x": EXTRACTOR}).scalars().all()
        if not rows:
            continue
        counts = Counter(t for r in rows for t in _terms(r))
        for term, n in counts.items():
            if n / len(rows) >= UMBRELLA_SHARE:
                share[term][tid] = n / len(rows)
    built = 0
    thread_names = {re.sub(r"\s+", " ", n.lower()).strip() for n in s.execute(text(
        "SELECT name FROM nodes WHERE type = 'Project' AND key = ANY(:keep)"), {"keep": keep_keys}).scalars()}
    # Orgs from your resume already hold their threads (graph/backbone.py): a term that is part of one's name
    # ("Reserve University" in Western Reserve University) is that org, not a new umbrella.
    org_names = [f" {re.sub(r'[^a-z0-9]+', ' ', n.lower()).strip()} " for n in s.execute(text("""
        SELECT o.name FROM edges e JOIN nodes o ON o.id = e.dst
        WHERE e.type = 'WORKED_AT' AND e.extractor IN ('profile', 'user')""")).scalars()]
    for term, by_thread in share.items():
        if len(by_thread) < UMBRELLA_THREADS:
            continue
        # A course is never over other courses (two classes share a prereq's name in their mail), and a term that
        # already names a thread is that thread, not a second thing above its neighbours.
        norm = re.sub(r"[^a-z0-9]+", " ", term.lower()).strip()
        if (COURSE_CODE.fullmatch(term.strip()) or norm in thread_names
                or (len(norm.split()) >= 2 and any(f" {norm} " in o for o in org_names))):
            continue
        months = s.execute(text("""
            SELECT count(DISTINCT date_trunc('month', occurred_at)) FROM source_items
            WHERE occurred_at IS NOT NULL AND (title ILIKE :q OR body_text ILIKE :q)
        """), {"q": f"%{term}%"}).scalar()
        if (months or 0) < UMBRELLA_MONTHS:
            continue
        names = dict(s.execute(text("SELECT id, name FROM nodes WHERE id = ANY(:i)"), {"i": list(by_thread)}).all())
        if not _is_umbrella(s, term, [names[t] for t in by_thread if t in names], months):
            continue
        key = "umbrella:" + term.lower()
        keep_keys.append(key)
        uid = s.execute(text("""
            INSERT INTO nodes (type, key, name, props) VALUES ('Project', :k, :n, CAST(:p AS jsonb))
            ON CONFLICT (type, key) DO UPDATE SET
                name = CASE WHEN COALESCE((nodes.props->>'renamed')::bool, false) THEN nodes.name ELSE EXCLUDED.name END,
                props = nodes.props || EXCLUDED.props
            RETURNING id
        """), {"k": key, "n": term, "p": json.dumps({"derived": "threads", "kind": "organization",
                                                     "umbrella": True, "months": months})}).scalar_one()
        for tid, frac in by_thread.items():
            # Evidence: the thread's items that name the umbrella (the verifier reads exactly those).
            eid = s.execute(text("""
                INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
                VALUES (:a, :b, 'PART_OF', CAST(:p AS jsonb), 'hierarchy', :v, :c)
                ON CONFLICT (src, dst, type, extractor) DO UPDATE SET props = EXCLUDED.props RETURNING id
            """), {"a": tid, "b": uid, "v": VERSION, "c": round(frac, 3),
                   "p": json.dumps({"why": f"{round(frac * 100)}% of its records mention {term}"})}).scalar_one()
            for sid in s.execute(text("""
                SELECT si.id FROM edges e JOIN nodes n ON n.id = e.src JOIN source_items si ON si.id = n.source_item_id
                WHERE e.dst = :t AND e.type = 'PART_OF' AND e.extractor = :x
                  AND (si.title ILIKE :q OR si.body_text ILIKE :q) ORDER BY si.occurred_at DESC LIMIT 6
            """), {"t": tid, "x": EXTRACTOR, "q": f"%{term}%"}).scalars():
                s.execute(text("INSERT INTO edge_evidence (edge_id, source_item_id, kind, detail) "
                               "VALUES (:e, :sid, 'umbrella', '{}')"), {"e": eid, "sid": sid})
        built += 1
    return built


def _is_umbrella(s: Session, term: str, thread_names: list[str], months: int) -> bool:
    """Is `term` the name of an organization, lab, program, club or team these threads belong to? Boilerplate
    shared across threads ("Zoom", "Best", "Eastern Time") isn't. Cached; without the model, not accepted."""
    from igneous.llm import LLMDisabled, structured

    if not settings.llm_enabled:
        return False
    prompt = f"Term: {term}\nPresent across {months} months.\nThreads whose records often mention it:\n" + \
        "\n".join(f"- {n}" for n in sorted(thread_names)[:20])
    key = "threads-umbrella:v1:" + hashlib.sha256(prompt.encode()).hexdigest()
    cached = s.execute(text("SELECT response FROM llm_cache WHERE cache_key = :k"), {"k": key}).scalar()
    if cached is None:
        try:
            cached, _ = structured(
                "Decide whether a term is the name of an organization, lab, program, club, team or company that "
                "the listed projects/threads belong to (e.g. a university lab running several client projects). "
                "Generic words, tools used everywhere (Zoom, Canvas), places, times, people's names and email "
                "boilerplate are NOT. Answer only from the term and the thread names.",
                prompt,
                {"type": "object", "additionalProperties": False, "required": ["umbrella", "confidence"],
                 "properties": {"umbrella": {"type": "boolean"}, "confidence": {"type": "number"}}},
                max_tokens=120, model=settings.llm_cheap_model)
        except LLMDisabled:
            return False
        s.execute(text("INSERT INTO llm_cache (cache_key, response) VALUES (:k, CAST(:r AS jsonb)) "
                       "ON CONFLICT (cache_key) DO NOTHING"), {"k": key, "r": json.dumps(cached)})
    return bool(cached.get("umbrella")) and (cached.get("confidence") or 0) >= 0.7


def ensure_hierarchy(s: Session) -> None:
    s.execute(text("""CREATE TABLE IF NOT EXISTS thread_parents (
        child_key text NOT NULL, parent_key text NOT NULL, origin text NOT NULL DEFAULT 'gold',
        PRIMARY KEY (child_key, parent_key))"""))


def pinned_parents(s: Session) -> int:
    """Write PART_OF edges for parent links someone stated (gold set / corrections), by registry key."""
    ensure_hierarchy(s)
    rows = s.execute(text("""
        SELECT c.id, p.id FROM thread_parents tp
        JOIN nodes c ON c.type = 'Project' AND c.key = tp.child_key
        JOIN nodes p ON p.type = 'Project' AND p.key = tp.parent_key""")).all()
    for c, p in rows:
        s.execute(text("""
            INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
            VALUES (:c, :p, 'PART_OF', '{"why": "you said so"}', 'user', :v, 1.0)
            ON CONFLICT (src, dst, type, extractor) DO NOTHING"""), {"c": c, "p": p, "v": VERSION})
    return len(rows)


def pin_gold(s: Session, gold: dict) -> int:
    """Seed the registry from a gold set: each gold thread is a pinned identity defined by its terms."""
    ensure_registry(s)
    n = 0
    for g in gold.get("threads", []):
        terms = [t.strip() for t in g.get("terms", []) if len(t.strip()) >= 3]
        if not terms:
            continue
        key = "gold:" + hashlib.sha256(g["name"].lower().encode()).hexdigest()[:16]
        s.execute(text("""
            INSERT INTO thread_registry (key, name, kind, terms, status, pinned, origin, confidence)
            VALUES (:k, :n, :kind, :t, 'active', true, 'gold', 1.0)
            ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, kind = EXCLUDED.kind, terms = EXCLUDED.terms,
                status = 'active', pinned = true, updated_at = now()
        """), {"k": key, "n": g["name"], "kind": g.get("kind"), "t": terms})
        n += 1
    ensure_hierarchy(s)
    gkey = lambda name: "gold:" + hashlib.sha256(name.lower().encode()).hexdigest()[:16]  # noqa: E731
    for g in gold.get("threads", []):
        if g.get("parent"):
            s.execute(text("INSERT INTO thread_parents (child_key, parent_key) VALUES (:c, :p) ON CONFLICT DO NOTHING"),
                      {"c": gkey(g["name"]), "p": gkey(g["parent"])})
    s.commit()
    return n


def _short_named(s: Session) -> int:
    """A chat named by a short or ambiguous token ("395": the class group chat) can't be matched by name alone, since
    "401" is CS 401 and EE 401 alike. Among the threads whose terms contain that token, the chat goes to the one
    it fits best (the share of its active days inside the thread's span, plus people they share), and only when
    that one clearly fits better than the others. The verifier then checks the link."""
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
        GROUP BY g.id""")).mappings().all()
    if not chats:
        return 0
    threads_ = s.execute(text("""
        SELECT p.id, r.terms, (p.props->>'start')::date AS a, (p.props->>'end')::date AS b,
               array_remove(array_agg(DISTINCT COALESCE(c.canonical_id, e.src)), NULL) AS people
        FROM nodes p JOIN thread_registry r ON r.key = p.key
        LEFT JOIN edges e ON e.dst = p.id AND e.type IN ('INVOLVED_IN', 'RELATED_TO')
        LEFT JOIN canonical c ON c.node_id = e.src
        WHERE p.type = 'Project' AND r.status = 'active' AND p.props ? 'start' GROUP BY p.id, r.terms""")).mappings().all()
    n = 0
    for ch in chats:
        fits = []
        for t in threads_:
            tokens = {w for term in t["terms"] or [] for w in re.findall(r"[a-z0-9]+", term.lower())}
            if ch["tok"] not in tokens:
                continue
            inside = sum(1 for d in ch["days"] if t["a"] <= d <= t["b"]) / max(1, len(ch["days"]))
            shared = len(set(ch["people"]) & set(t["people"] or []))
            fits.append((inside + 0.25 * min(shared, 4), shared, inside, t["id"]))
        fits.sort(reverse=True)
        if not fits or fits[0][2] < 0.5 or (len(fits) > 1 and fits[0][0] - fits[1][0] < 0.25):
            continue  # it didn't happen during any of them, or two fit about as well: no guess
        score, shared, inside, tid = fits[0]
        s.execute(text("""
            INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
            VALUES (:g, :t, 'PART_OF', CAST(:p AS jsonb), 'hierarchy', :v, :c)
            ON CONFLICT (src, dst, type, extractor) DO NOTHING
        """), {"g": ch["id"], "t": tid, "v": VERSION, "c": round(min(0.9, 0.4 + score / 3), 2),
               "p": json.dumps({"why": f"named like it; {round(inside * 100)}% of its activity was during it"
                                       + (f", {shared} of its people are in it" if shared else "")})})
        n += 1
    return n


def attach_groups(s: Session) -> int:
    """A named group chat or a recurring event is often *about* a thread: three chats called "Maple St" and
    "Maple St party planning" belong to the shared house. Propose chat/event PART_OF thread when the chat's
    name contains one of the thread's defining terms; the verifier then checks each link."""
    threads_ = s.execute(text("""
        SELECT p.id, r.terms FROM nodes p JOIN thread_registry r ON r.key = p.key
        WHERE p.type = 'Project' AND r.status = 'active'""")).all()
    n = 0
    for tid, terms in threads_:
        for term in [t for t in (terms or []) if len(t) >= 4]:
            rows = s.execute(text("""
                SELECT g.id FROM nodes g
                WHERE ((g.type = 'Thread' AND COALESCE((g.props->>'group')::bool, false)
                        AND NOT COALESCE((g.props->>'hidden')::bool, false))
                       OR g.type = 'Event')
                  AND g.name ILIKE :q
                  AND NOT EXISTS (SELECT 1 FROM edges x WHERE x.src = g.id AND x.dst = :t AND x.type = 'PART_OF')
                LIMIT 20"""), {"q": f"%{term}%", "t": tid}).scalars().all()
            for gid in rows:
                s.execute(text("""
                    INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
                    VALUES (:g, :t, 'PART_OF', CAST(:p AS jsonb), 'hierarchy', :v, 0.7)
                    ON CONFLICT (src, dst, type, extractor) DO NOTHING
                """), {"g": gid, "t": tid, "v": VERSION, "p": json.dumps({"why": f"named after {term}"})})
                n += 1
    return n
