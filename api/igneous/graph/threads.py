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
UMBRELLA_MONTHS = 6      # an umbrella (org, program, lab) is present across at least this many months...
UMBRELLA_SHARE = 0.2     # ...and makes up at least this share of the items of...
UMBRELLA_THREADS = 2     # ...at least this many threads

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
    # Repeating terms are kept (a weekly game repeats); whether a candidate names a real thing is the judge's
    # call, measured against the gold set in eval/threads_gold.json rather than tuned by hand.
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
                "there a real thread here? If yes: name it the way the person would say it ('Seattle trip', "
                "'CSDS 395', 'Draw Steel'), say what kind of thing it is, list the words that actually identify "
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


def run(s: Session, log=print) -> int:
    from igneous.graph import overrides

    cands = candidates(s)
    s.execute(text("DELETE FROM edges WHERE extractor IN (:x, 'hierarchy', 'thread-resolution')"), {"x": EXTRACTOR})
    keep_keys = []
    built = 0
    for c in cands:
        verdict = _judge(s, c)
        if not verdict.get("keep"):
            continue
        members = _belongs(c, verdict)
        if len(members) < 3:
            continue  # once unrelated records are removed, too little is left to be a thread
        key = "thread:" + hashlib.sha256((c["terms"][0].lower() + c["start"].strftime("%Y-%m")).encode()).hexdigest()[:16]
        keep_keys.append(key)
        props = {"derived": "threads", "kind": verdict.get("kind") or "thread",
                 "terms": (verdict.get("defining_terms") or c["terms"])[:6],
                 "start": c["start"].isoformat(), "end": c["end"].isoformat(),
                 "sources": dict(Counter(r.source for r in members)), "records": len(members),
                 "judged": verdict.get("judged", False), "confidence": verdict.get("confidence"),
                 "suggested": (verdict.get("confidence") or 0) < MIN_CONFIDENCE}
        tid = s.execute(text("""
            INSERT INTO nodes (type, key, name, props) VALUES ('Project', :k, :n, CAST(:p AS jsonb))
            ON CONFLICT (type, key) DO UPDATE SET
                name = CASE WHEN COALESCE((nodes.props->>'renamed')::bool, false) THEN nodes.name ELSE EXCLUDED.name END,
                props = nodes.props || EXCLUDED.props
            RETURNING id
        """), {"k": key, "n": verdict["name"][:80], "p": json.dumps(props)}).scalar_one()
        defining = (verdict.get("defining_terms") or c["terms"][:1])[0]
        for it in members:
            if it.node_id:
                why = "picked by the judge" if it.id in set(verdict.get("members") or []) else f"mentions {defining}"
                _edge(s, it.node_id, tid, "PART_OF", {"why": why}, it.id, 0.6)
        for p, n, ev in _people(s, [r.id for r in members]):
            # Record the very items that make them a member, so the verifier and the "why" view read those.
            eid = _edge(s, p, tid, "INVOLVED_IN", {"why": f"in {n} of its records", "items": n}, None, 0.6)
            for sid in ev or []:
                if sid:
                    s.execute(text("INSERT INTO edge_evidence (edge_id, source_item_id, kind, detail) "
                                   "VALUES (:e, :sid, 'member', '{}')"), {"e": eid, "sid": sid})
        built += 1
        log(f"  thread {props['kind']:10s} sources={len(c['sources'])} items={len(c['items'])}") if log else None
    # Before deleting stale threads and finding umbrellas: fold duplicates (one ETA, not eighteen) and link
    # related threads, so umbrellas see one thread per real thing.
    s.execute(text("""DELETE FROM nodes WHERE type = 'Project' AND props->>'derived' = 'threads'
                      AND NOT COALESCE((props->>'umbrella')::bool, false) AND NOT (key = ANY(:keep))"""),
              {"keep": keep_keys})
    s.commit()
    from igneous.graph import resolve_threads
    resolve_threads.run(s, log=None)
    keep_keys[:] = s.execute(text("SELECT key FROM nodes WHERE type = 'Project' AND props->>'derived' = 'threads'")).scalars().all()
    built += umbrellas(s, keep_keys)
    # Threads that no longer qualify go away (corrections in overrides survive: they key on node keys).
    s.execute(text("""DELETE FROM nodes WHERE type = 'Project' AND props->>'derived' = 'threads'
                      AND NOT (key = ANY(:keep))"""), {"keep": keep_keys})
    overrides.apply(s)
    s.commit()
    return built


def umbrellas(s: Session, keep_keys: list[str]) -> int:
    """Long-running things several threads sit under: an org, lab, program or club ("xLab" over Dealer Tire,
    ETA and the avatar kiosk). The opposite of a thread: persistent (many months, no single burst) and
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
    for term, by_thread in share.items():
        if len(by_thread) < UMBRELLA_THREADS:
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
                max_tokens=120)
        except LLMDisabled:
            return False
        s.execute(text("INSERT INTO llm_cache (cache_key, response) VALUES (:k, CAST(:r AS jsonb)) "
                       "ON CONFLICT (cache_key) DO NOTHING"), {"k": key, "r": json.dumps(cached)})
    return bool(cached.get("umbrella")) and (cached.get("confidence") or 0) >= 0.7
