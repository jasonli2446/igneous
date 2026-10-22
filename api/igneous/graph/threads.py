"""Life threads: the trips, classes, jobs, hobbies and projects that no single record names.

A Seattle trip is a flight email, an itinerary doc, browsing, a calendar block and a group chat, and nothing
labels all of them. What they share is a distinctive term that suddenly shows up in several kinds of source at
once. So a thread is found, not configured:

  1. Terms: capitalized phrases from item titles and text ("Seattle", "Pike Place", "CSDS 395", "Draw Steel").
  2. Burst: a term that appears in 3+ distinct sources within a <= BURST_DAYS window, and is not everywhere all
     the time (document-frequency cap), is a candidate. The densest window is its span.
  3. Members: items in the window that mention the term; people involved in 2+ of them.
  4. Overlapping candidates (same window, mostly the same items) merge into one.
  5. Judge: with the model enabled and sources opted in (IGNEOUS_LLM_SOURCES), one cached structured call per
     candidate decides keep / kind (trip, class, job, hobby, project, ... free text) / the name the user would
     use, from titles and short snippets of opted-in sources only (counts for the rest). Without it, a thread
     is named by its term and month ("Seattle · Aug 2026"), kind "thread". Low confidence is kept as a
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

# Sources that describe the user's life (not contact cards or raw metadata).
SOURCES = ("gmail", "email", "imessage", "gcal", "drive", "fs", "notes", "ai_chats", "github",
           "takeout_activity", "takeout_chrome", "takeout_tasks", "takeout_meet", "takeout_chat")

# Capitalized phrases (1-3 words), or course-style codes ("CSDS 395").
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
    "Searched", "Watched", "Used", "Viewed", "Assistant", "User", "Jason", "Igneous",  # the user + this tool
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
               left(coalesce(si.body_text, ''), 1500) AS body, n.id AS node_id
        FROM source_items si
        LEFT JOIN nodes n ON n.source_item_id = si.id AND n.type IN ('Message', 'File', 'Event')
        WHERE si.source = ANY(:src) AND si.occurred_at IS NOT NULL
        ORDER BY si.id, n.id
    """), {"src": list(SOURCES)}).all()


def _bursts(its: list) -> list[tuple[int, int]]:
    """Windows where a term is far denser than its own background: (lo, hi) index ranges into the time-sorted
    items. A place mentioned for years (Seattle) can still have a trip-shaped spike; each spike is a candidate."""
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
    return _merge(out)


def _merge(cands: list[dict]) -> list[dict]:
    """Candidates over mostly the same items in overlapping windows are one thread ("Seattle" + "Pike Place")."""
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


def _people(s: Session, item_ids: list[int]) -> list[tuple[int, int]]:
    """(canonical person, items they're in) for people in 2+ of the thread's items, excluding services."""
    return s.execute(text("""
        WITH msg AS (SELECT n.id FROM nodes n WHERE n.source_item_id = ANY(:ids)),
        ppl AS (
            SELECT e.src AS p, e.dst AS m FROM edges e JOIN msg ON msg.id = e.dst WHERE e.type IN ('SENT', 'ATTENDED')
            UNION SELECT e.dst, e.src FROM edges e JOIN msg ON msg.id = e.src WHERE e.type = 'RECEIVED')
        SELECT COALESCE(c.canonical_id, ppl.p) AS p, count(DISTINCT ppl.m) AS n
        FROM ppl LEFT JOIN canonical c ON c.node_id = ppl.p JOIN nodes pn ON pn.id = ppl.p
        WHERE pn.type = 'Person' AND COALESCE(pn.props->>'kind', '') <> 'service'
          AND NOT COALESCE((pn.props->>'self')::bool, false)
        GROUP BY 1 HAVING count(DISTINCT ppl.m) >= 2 ORDER BY 2 DESC LIMIT 30
    """), {"ids": item_ids}).all()


def _judge(s: Session, c: dict) -> dict:
    """Name and classify a candidate. Model only with opted-in sources; cached per candidate content."""
    from igneous.graph.extract import allowed_sources
    from igneous.llm import LLMDisabled, structured

    allowed = allowed_sources()
    month = c["start"].strftime("%b %Y") if c["start"].strftime("%b %Y") == c["end"].strftime("%b %Y") \
        else f"{c['start']:%b}-{c['end']:%b %Y}"
    fallback = {"keep": True, "kind": "thread", "name": f"{c['terms'][0]} · {month}", "confidence": 0.6,
                "judged": False}
    shown = [r for r in c["items"].values() if r.source in allowed][:40]
    if not settings.llm_enabled or not shown:
        return fallback
    lines = [f"- [{r.source}, {r.occurred_at:%Y-%m-%d}] {r.title[:120]} :: {re.sub(r'\\s+', ' ', r.body)[:240]}"
             for r in shown]
    others = {src: n for src, n in c["sources"].items() if src not in allowed}
    prompt = (f"Terms: {', '.join(c['terms'][:6])}\nWindow: {c['start']:%Y-%m-%d} to {c['end']:%Y-%m-%d}\n"
              f"Items (opted-in sources):\n" + "\n".join(lines) +
              (f"\nAlso, items not shown (counts only): {json.dumps(others)}" if others else ""))
    key = "threads-judge:v1:" + hashlib.sha256(prompt.encode()).hexdigest()
    cached = s.execute(text("SELECT response FROM llm_cache WHERE cache_key = :k"), {"k": key}).scalar()
    if cached is not None:
        return {**cached, "judged": True}
    try:
        out, _ = structured(
            "You find the threads of someone's life in their records: a trip, a class, a job or internship, a "
            "hobby, a project, a club, an event they planned. Given records that share some terms within a time "
            "window, decide whether they are one such thread. If yes, name it the way the person would refer to "
            "it in conversation (short: 'Seattle trip', 'CSDS 395', 'KKR internship', 'Draw Steel'), and say "
            "what kind of thing it is in one or two words. If the records are unrelated or just share a common "
            "word (a newsletter topic, a place mentioned in passing), keep=false. Use only what the records show.",
            prompt,
            {"type": "object", "additionalProperties": False, "required": ["keep", "kind", "name", "confidence"],
             "properties": {"keep": {"type": "boolean"}, "kind": {"type": "string"}, "name": {"type": "string"},
                            "confidence": {"type": "number"}}},
            max_tokens=400)
    except LLMDisabled:
        return fallback
    s.execute(text("INSERT INTO llm_cache (cache_key, response) VALUES (:k, CAST(:r AS jsonb)) "
                   "ON CONFLICT (cache_key) DO NOTHING"), {"k": key, "r": json.dumps(out)})
    return {**out, "judged": True}


def _edge(s: Session, src: int, dst: int, type_: str, props: dict, sid: int | None, conf: float) -> None:
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


def run(s: Session, log=print) -> int:
    from igneous.graph import overrides

    cands = candidates(s)
    s.execute(text("DELETE FROM edges WHERE extractor = :x"), {"x": EXTRACTOR})
    keep_keys = []
    built = 0
    for c in cands:
        verdict = _judge(s, c)
        if not verdict.get("keep"):
            continue
        key = "thread:" + hashlib.sha256((c["terms"][0].lower() + c["start"].strftime("%Y-%m")).encode()).hexdigest()[:16]
        keep_keys.append(key)
        props = {"derived": "threads", "kind": verdict.get("kind") or "thread", "terms": c["terms"][:6],
                 "start": c["start"].isoformat(), "end": c["end"].isoformat(), "sources": dict(c["sources"]),
                 "judged": verdict.get("judged", False), "confidence": verdict.get("confidence"),
                 "suggested": (verdict.get("confidence") or 0) < MIN_CONFIDENCE}
        tid = s.execute(text("""
            INSERT INTO nodes (type, key, name, props) VALUES ('Project', :k, :n, CAST(:p AS jsonb))
            ON CONFLICT (type, key) DO UPDATE SET
                name = CASE WHEN COALESCE((nodes.props->>'renamed')::bool, false) THEN nodes.name ELSE EXCLUDED.name END,
                props = nodes.props || EXCLUDED.props
            RETURNING id
        """), {"k": key, "n": verdict["name"][:80], "p": json.dumps(props)}).scalar_one()
        for it in c["items"].values():
            if it.node_id:
                _edge(s, it.node_id, tid, "PART_OF", {"why": "mentions " + c["terms"][0]}, it.id, 0.6)
        for p, n in _people(s, list(c["items"].keys())):
            _edge(s, p, tid, "INVOLVED_IN", {"why": "in thread items", "items": n}, None, 0.6)
        built += 1
        log(f"  thread {props['kind']:10s} sources={len(c['sources'])} items={len(c['items'])}") if log else None
    # Threads that no longer qualify go away (corrections in overrides survive: they key on node keys).
    s.execute(text("""DELETE FROM nodes WHERE type = 'Project' AND props->>'derived' = 'threads'
                      AND NOT (key = ANY(:keep))"""), {"keep": keep_keys})
    overrides.apply(s)
    s.commit()
    return built
