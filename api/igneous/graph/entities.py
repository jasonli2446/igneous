"""Derived entities: the folders, calendar series, group chats and repos you return to ("Gloomhaven Night", a book
club, "Igneous").

Which stage owns which Project: graph/threads.py owns life threads, the trips, classes, jobs and projects it finds from
bursts, name seeds (repositories, course codes, calendar series, named group chats) and your profile, each a lasting
identity in the thread registry with its kind and its people. This stage owns anchors, what your records already
group under a name, and runs after the threads stage: an anchor a thread is about joins that thread, and only an
anchor no thread is about becomes a Project of its own. One real thing is one Project.

Nothing is configured. A name becomes a candidate when the data itself treats it as a thing you return to:
  - a recurring calendar series (3+ occurrences), or a title you put on 3+ events
  - a named group chat active on 5+ days
  - a Drive / local folder holding 2+ files or folders, unless it is a filesystem root, or its name says how files
    are kept ("Downloads", "src") or when ("Fall 2024")
  - the repo an AI coding session ran in
A name keeps its numbers, since a course code, a year or a version is part of it ("CS 362", "Lisbon 2025"), but not an
occurrence's ("session 4", "#12"). Generic names ("Lunch", "1:1", "Office hours") are dropped by a stop-list, and by a
one-time, cached model check for names that appear in opted-in sources when the model is enabled.

Anchors that share a name are one candidate, shown by the name's most common spelling, unless the people on them
(never you, never a service) show they are different things: groups of two or more people with nobody in common. An
anchor's own records are the ones the spine built it from (graph/spine.py's provenance): a folder's files, in its
subfolders too, a repo's AI sessions and activity, a group chat's days, a series' occurrences.

A candidate is about a thread when one of the thread's defining terms is in its name (a course code with or without its
space) and the thread already holds one of its anchors, or most of its anchors' records are the thread's: it holds
them, or they happened while it did (whenever, for a thread with no window: graph/threads.py `window`). Its anchors then
join that thread (PART_OF, with this stage's evidence), and so do its folders' files from while the thread happened,
each once, for its nearest folder: a file from another time in the same folder is no part of it. Who took part stays
the threads stage's call. Its key stays a row of the thread registry (origin 'entities', merged into the thread), so
what you said about its Project holds for the thread: a link you rejected, confirmed or added (graph/derived.py,
graph/verify.py) and a name you gave it (graph/overrides.py). A candidate you merged, or merged something into, stands
on its own: your merge decides.

A Project of its own holds its anchors (structurally, confidence 1.0), its folders' files, and each record that names it
(by phrase match on its chunks) when the record shows it belongs: it is one of its anchors' own records, it shares a
person with them, or it is yours (a conversation you wrote or got, an event you attended, your calendar, tasks and AI
chats). A notice sent to you (a booking, a confirmation), a search, a page you visited or a place you went can only
show it went on: one naming it within PAD of its records is its evidence (RELATED_TO, props.role 'evidence', as the
threads stage links a thread's), never a member. Marketing never counts. When one name is several projects, a mention
goes to the ones whose people are on it. People involved in 2+ of its items get INVOLVED_IN with a count; someone on its
own calendar series or in its own group chat is listed by the record itself (props.direct), which settles the link
without a model. A group chat that names it on 5+ days is linked as a whole: that's how "Gloomhaven Night" on the
calendar finds the group chat, and the chat finds who else plays.

Every edge here is written with extractor 'entities' through graph/derived.py's Writer (a link you rejected is never
written), and the stage deletes and rebuilds its own edges on every run, so new data (a new mention, a new chat day)
updates old entities. A candidate keeps the key it was first given (`project:<name>:<first anchor>`) while that anchor
is one of its anchors, so a correction outlives a change in how names are read. Projects are upserted under the
correction contract (a name you gave one stays); corrections made in the Inspector live in graph/overrides.py and are
replayed by the pipeline's last step.
"""
import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import source_catalog
from igneous.config import settings
from igneous.graph import identity
from igneous.graph.derived import Writer, follow
from igneous.graph.threads import PAD, PLACES, term_pattern, window

EXTRACTOR = "entities"
VERSION = "1"
MIN_EVENT_OCC = 3
MIN_CHAT_DAYS = 5
MIN_PEOPLE_ITEMS = 2
MIN_GROUP = 2  # people on an anchor before they can show it is a different thing from a namesake
MAX_ITEMS_PER_ENTITY = 2000
EVIDENCE = 6   # records shown as evidence of an anchor joining a thread

# Fallback when the model is off: names that recur but aren't about anything.
GENERIC = {
    "lunch", "dinner", "breakfast", "coffee", "meeting", "1:1", "1-1", "one on one", "sync", "standup",
    "stand-up", "call", "office hours", "class", "lecture", "lab", "recitation", "study", "gym", "workout",
    "busy", "focus time", "focus", "ooo", "out of office", "travel", "flight", "birthday", "holiday", "work",
    "home", "school", "free", "hold", "block", "tbd", "event", "appointment", "interview", "chat", "catch up",
    "weekly", "daily", "team meeting", "check-in", "check in", "review", "documents", "downloads", "desktop",
    "untitled", "new folder", "photos", "notes", "drive", "my drive", "shared", "misc", "archive",
}
# Folder names that say how files are kept, not what they are about.
GENERIC_FOLDERS = {
    "files", "folder", "drive folder", "src", "source", "lib", "bin", "build", "dist", "out", "output", "tmp",
    "temp", "test", "tests", "doc", "docs", "data", "assets", "images", "img", "static", "public", "scripts",
    "config", "resources", "templates", "examples", "backup", "backups", "old", "new", "attachments", "screenshots",
    "pictures", "videos", "movies", "music", "projects", "code", "repos", "personal", "other", "stuff", "inbox",
    "icloud drive", "mobile documents", "com~apple~clouddocs", "library", "applications", "users", "career",
    "careers",
}
# Words of a name that only says when ("Fall 2024", "Q3", "March 2025", "Week 3"): a time, not a thing.
TIME_WORDS = frozenset({
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
    "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "spring",
    "summer", "fall", "autumn", "winter", "q1", "q2", "q3", "q4", "h1", "h2", "quarter", "semester", "term", "year",
    "month", "week", "day",
})
# Records that are yours whoever else is in them: your calendar, your tasks and your AI chats.
YOURS = frozenset(source_catalog.values({"calendar", "tasks", "ai_chat"}))

CLEAN = re.compile(r"\s*[\(\[].*?[\)\]]\s*|\s+[-\u2013\u2014:|]\s+.*$")
# An occurrence's number, which names one meeting of a thing rather than the thing ("session 4", "week 3", "#12"): the
# number goes and its word stays. A number of four digits is a year, part of the name ("Demo Day 2026").
COUNTER = re.compile(r"#\s?\d{1,3}\b|\b(session|meeting|week|part|round|sprint|episode|ep|lecture|game|day)\.?\s?#?\d{1,3}\b")


def _norm(name: str) -> str:
    """'Gloomhaven Night (session 4)' -> 'gloomhaven night'; 'Igneous - weekly sync' -> 'igneous';
    'Sprint 23 planning' -> 'sprint planning'; 'CS 362 Database Systems' -> 'cs 362 database systems';
    'Demo Day 2026' -> 'demo day 2026'."""
    n = CLEAN.sub("", name or "").strip().lower()
    n = COUNTER.sub(lambda m: f" {m.group(1) or ''} ", n)
    n = re.sub(r"\b(weekly|biweekly|daily|monthly|session|meeting|sync|standup|call)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip(" -:")


def _usable(n: str) -> bool:
    return (3 <= len(n) <= 48 and n not in GENERIC and not re.fullmatch(r"[\d\s:/.-]+", n) and "@" not in n
            and not re.fullmatch(r"[0-9a-f]{8,}", n)  # a hash (a filesystem root's key) names nothing
            and not all(w in TIME_WORDS or w.isdigit() for w in n.split()))  # "fall 2024" says when, not what


def _spelling(name: str) -> str:
    """How an anchor spells its name, without the parts _norm drops: 'Gloomhaven Night (session 4)' ->
    'Gloomhaven Night'."""
    return CLEAN.sub("", name or "").strip() or name


def _words(name: str) -> str:
    """A name as words to find terms in: 'atlas-platform' -> 'atlas platform'."""
    return re.sub(r"[\s_\-/]+", " ", (name or "").lower()).strip()


def candidates(s: Session) -> dict[str, list[tuple[int, str, str]]]:
    """name -> its anchors, the structural members: [(node id, how it is one, how it spells the name)]."""
    out: dict[str, list[tuple[int, str, str]]] = defaultdict(list)

    def add(name: str, node_id: int, how: str):
        if _usable(n := _norm(name)):
            out[n].append((node_id, how, _spelling(name)))

    # Recurring events: one node per series already; also titles repeated across non-recurring events.
    for nid, name, occ in s.execute(text("""
        SELECT n.id, n.name, count(si.id) FROM nodes n
        JOIN source_items si ON si.source = 'gcal'
             AND n.key = COALESCE(si.raw_meta->>'event_key', 'gcal:' || COALESCE(si.raw_meta->>'series_key', si.external_id))
        WHERE n.type = 'Event' GROUP BY n.id, n.name ORDER BY n.id
    """)):
        if occ >= MIN_EVENT_OCC:
            add(name, nid, f"event series ({occ})")
    titles: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for nid, name in s.execute(text("SELECT id, name FROM nodes WHERE type = 'Event' ORDER BY id")):
        titles[_norm(name)].append((nid, name))
    for events in titles.values():
        if len(events) >= MIN_EVENT_OCC:
            for nid, name in events:
                add(name, nid, "repeated event")

    # Named group chats must be active across several days. A title alone is not enough evidence of a project.
    for nid, name in s.execute(text("""
        SELECT t.id, t.name
        FROM nodes t
        JOIN edges it ON it.dst = t.id AND it.type = 'IN_THREAD'
        JOIN source_items si ON si.id = it.source_item_id
        WHERE t.type = 'Thread' AND COALESCE((t.props->>'group')::bool, false)
          AND t.name !~ '[0-9]{7,}' AND t.name !~ '@' AND t.name NOT LIKE '%,%'
        GROUP BY t.id, t.name
        HAVING count(DISTINCT date(si.occurred_at)) >= :days
        ORDER BY t.id
    """), {"days": MIN_CHAT_DAYS}):
        add(name, nid, "group chat")

    # Folders with 2+ files, and repos AI sessions ran in. A filesystem root (keyed by a hash of its path) and a
    # folder named for how files are kept ("src", "Downloads") are about nothing in particular.
    for nid, name, how in s.execute(text("""
        SELECT f.id, f.name, CASE WHEN f.props->>'kind' = 'repo' THEN 'repo' ELSE 'folder' END
        FROM nodes f WHERE f.type = 'Folder' AND (
            f.props->>'kind' = 'repo'
            OR (SELECT count(*) FROM edges e WHERE e.src = f.id AND e.type = 'CONTAINS') >= 2)
          AND NOT (f.key LIKE 'fs:%' AND strpos(COALESCE(f.props->>'path', ''), '/') = 0)
        ORDER BY f.id
    """)):
        folder = name.rsplit("/", 1)[-1]
        if how == "repo" or _norm(folder) not in GENERIC_FOLDERS:
            add(folder, nid, how)
    return dict(out)


def _people(s: Session, nodes: list[int]) -> dict[int, set[int]]:
    """node -> the people on it, as identity heads, never you or a service: who attended an event, sent or got a
    message (or one of a thread's), or wrote in what a folder or repo holds (its messages, or its conversations')."""
    me = identity.self_id(s)
    rows = s.execute(text(f"""
        WITH held AS (  -- the messages a node is, or holds directly or through its threads
            SELECT id AS node, id AS message FROM nodes WHERE id = ANY(:ids)
            UNION SELECT it.dst, it.src FROM edges it WHERE it.type = 'IN_THREAD' AND it.dst = ANY(:ids)
            UNION SELECT c.src, c.dst FROM edges c WHERE c.type = 'CONTAINS' AND c.src = ANY(:ids)
            UNION SELECT c.src, it.src FROM edges c JOIN edges it ON it.dst = c.dst AND it.type = 'IN_THREAD'
                  WHERE c.type = 'CONTAINS' AND c.src = ANY(:ids)),
        raw AS (
            SELECT e.dst AS node, e.src AS person FROM edges e WHERE e.type = 'ATTENDED' AND e.dst = ANY(:ids)
            UNION SELECT h.node, x.src FROM held h JOIN edges x ON x.dst = h.message AND x.type = 'SENT'
            UNION SELECT h.node, x.dst FROM held h JOIN edges x ON x.src = h.message AND x.type = 'RECEIVED')
        SELECT raw.node, {identity.head_sql('raw.person')} AS person FROM raw JOIN nodes p ON p.id = raw.person
        WHERE p.type = 'Person' AND COALESCE(p.props->>'kind', '') <> 'service'
    """), {"ids": nodes}).all()
    out: dict[int, set[int]] = defaultdict(set)
    for node, person in rows:
        if person != me:
            out[node].add(person)
    return out


def _own(s: Session, anchors: list[int]) -> dict[int, list[tuple[int, int | None, object]]]:
    """anchor -> its own records, the ones the spine built it from: [(record, the node that is that record, when)]."""
    out: dict[int, list[tuple[int, int | None, object]]] = defaultdict(list)
    for anchor, record, at, node in s.execute(text("""
        SELECT sp.node_id, sp.source_item_id, si.occurred_at,
               (SELECT r.id FROM nodes r WHERE r.source_item_id = sp.source_item_id
                  AND r.type IN ('File', 'Message', 'Event') ORDER BY r.id LIMIT 1)
        FROM spine_node_support sp JOIN source_items si ON si.id = sp.source_item_id
        WHERE sp.node_id = ANY(:a) ORDER BY sp.node_id, sp.source_item_id
    """), {"a": anchors}):
        out[anchor].append((record, node, at))
    return out


def _threads(s: Session) -> list[dict]:
    """The life threads the threads stage built this run, in key order, each with the words that define it (its own and
    those of the threads folded into it), its window, its records and the anchors it already holds."""
    rows = {r.key: r for r in s.execute(text("SELECT * FROM thread_registry ORDER BY key"))}
    into = {k: r.merged_into for k, r in rows.items() if r.status == "merged" and r.merged_into}
    terms: dict[str, set[str]] = defaultdict(set)
    for key, r in rows.items():
        if r.origin != EXTRACTOR and r.status in ("active", "merged"):
            # A bare number ("395") is shared by different courses, rooms and years: never a defining term alone.
            terms[follow(into, key)] |= {_words(t) for t in r.terms or [] if len(t) >= 3 and not t.strip().isdigit()}
    out = []
    for t in s.execute(text("""
        SELECT p.id, p.key, array_remove(array_agg(DISTINCT n.source_item_id), NULL) AS records,
               array_remove(array_agg(DISTINCT n.id) FILTER (WHERE n.type IN ('Thread', 'Event', 'Folder')), NULL)
                   AS attached
        FROM nodes p LEFT JOIN edges e ON e.dst = p.id AND e.type = 'PART_OF' AND e.extractor = 'threads'
        LEFT JOIN nodes n ON n.id = e.src
        WHERE p.type = 'Project' AND p.props->>'derived' = 'threads' AND NOT COALESCE((p.props->>'umbrella')::bool, false)
        GROUP BY p.id, p.key ORDER BY p.key
    """)).mappings():
        row = rows.get(t["key"])
        if row is None or row.status != "active" or not terms.get(t["key"]):
            continue
        words = "|".join(term_pattern(w) for w in sorted(terms[t["key"]]))
        out.append({"id": t["id"], "key": t["key"], "window": window(row),
                    "pattern": re.compile(rf"(?<![a-z0-9])({words})(?![a-z0-9])"),
                    "records": set(t["records"] or []), "attached": set(t["attached"] or [])})
    return out


def _theirs(t: dict, record: int, at) -> bool:
    """The record is the thread's: it holds it, or it happened while the thread did (whenever, for a thread with no
    window: graph/threads.py `window`)."""
    lo, hi = t["window"] or (None, None)
    return record in t["records"] or lo is None or (at is not None and lo <= at <= hi)


def _about(spellings: list[str], anchors: set[int], own: list[tuple], threads: list[dict]) -> dict | None:
    """The thread a candidate is about, or None: one of its defining terms is in the candidate's name, and the thread
    holds one of the candidate's anchors (the threads stage attached it) or most of the anchors' records are the
    thread's (`_theirs`). One record the thread holds is not enough: a folder of two trips to one city is the later
    trip's only if most of its files are. Of several, the one holding the most of its anchors, then the one most of
    the records are, then the biggest (then the first by key)."""
    names = [_words(sp) for sp in spellings]
    best = None
    for t in threads:
        if not any(t["pattern"].search(n) for n in names):
            continue
        attached = len(anchors & t["attached"])
        theirs = sum(1 for record, _node, at in own if _theirs(t, record, at))
        if not (attached or 2 * theirs > len(own) or (not own and t["window"] is None)):
            continue
        rank = (attached, theirs, len(t["records"]))
        if best is None or rank > best[0]:
            best = (rank, t)
    return best[1] if best else None


def _projects(anchors: list[tuple[int, str, str]], people: dict[int, set[int]]) -> list[list[tuple[int, str, str]]]:
    """Split one name's anchors into projects: all one, unless the people on them show different things. Anchors
    with MIN_GROUP or more people that share someone are one group; groups with nobody in common are different
    projects. An anchor with too few people to tell joins the group it shares someone with, else the oldest."""
    ids = list(dict.fromkeys(nid for nid, _how, _spelling in anchors))
    known = [a for a in ids if len(people.get(a, ())) >= MIN_GROUP]
    parent = {a: a for a in known}

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, a in enumerate(known):
        for b in known[i + 1:]:
            if people[a] & people[b]:
                parent[find(a)] = find(b)
    groups: dict[int, list[int]] = defaultdict(list)
    for a in known:
        groups[find(a)].append(a)
    if len(groups) < 2:
        return [anchors]
    ordered = sorted(groups.values(), key=min)
    for a in ids:
        if a not in parent:
            sharing = [g for g in ordered if people.get(a, set()) & set().union(*(people[x] for x in g))]
            (sharing[0] if len(sharing) == 1 else ordered[0]).append(a)
    return [[anchor for anchor in anchors if anchor[0] in group] for group in ordered]


def _screen(names: list[str]) -> set[str] | None:
    """Ask the model which names are a specific thing (a project, game, club, course, org, product) rather than a
    generic activity. None when the model can't answer."""
    from igneous.llm import LLMDisabled, structured

    if not names:
        return set()
    try:
        out, _ = structured(
            "You classify short names taken from someone's calendar, group chats and folders. Keep a name if it "
            "refers to a specific thing that other records could mention by name: a project, game, campaign, "
            "club, team, course, company, product, event series with a proper name. Drop generic activities "
            "and placeholders (lunch, 1:1, gym, class, focus, office hours, team sync).",
            "Names:\n" + "\n".join(f"- {n}" for n in names),
            {"type": "object", "properties": {"keep": {"type": "array", "items": {"type": "string"}}},
             "required": ["keep"], "additionalProperties": False}, max_tokens=2000)
        return {k.strip().lower() for k in out["keep"]} & set(names)
    except LLMDisabled:
        return None


def _screened(names: list[str], shown: set[str]) -> set[str]:
    """The names that are specific things: one model verdict per name, cached in llm_cache. Only names in `shown`
    (an anchor appears in opted-in records) go to the model; the rest are kept, since the stop-list already
    dropped the generic ones. Only model verdicts are cached (`by: model`); a cached keep without that mark is
    asked again once the name may be shown."""
    from igneous.llm import cache_lookup, cache_store

    kept = set()
    for name in names:
        key = "entities-screen:v1:" + hashlib.sha256(name.encode()).hexdigest()
        cached = cache_lookup(key)
        if cached is not None and (cached.get("by") == "model" or not cached.get("keep")):
            verdict = bool(cached.get("keep"))
        else:
            answer = _screen([name]) if name in shown and settings.llm_enabled else None
            verdict = answer is None or name in answer
            if answer is not None:
                cache_store(key, {"keep": verdict, "name": name, "by": "model"}, replace=True)
        if verdict:
            kept.add(name)
    return kept


@dataclass
class Link:
    """One edge to a project, with every reason it has: (why, evidence item, detail) each, and the strongest."""
    reasons: list[tuple[str, int | None, dict]] = field(default_factory=list)
    confidence: float = 0.0


class Links(dict[tuple[int, str], Link]):
    """A project's edges by (member, edge type): adding a reason to a member already linked keeps both."""

    def add(self, src: int, type_: str, why: str, confidence: float, sid: int | None = None, **detail) -> None:
        link = self.setdefault((src, type_), Link())
        link.reasons.append((why, sid, {"why": why, **detail}))
        link.confidence = max(link.confidence, confidence)

    def write(self, w: Writer, project: int) -> None:
        for (src, type_), link in sorted(self.items()):
            detail = {k: v for _why, _sid, d in link.reasons for k, v in d.items() if k != "why"}
            props = {**detail, "why": "; ".join(dict.fromkeys(why for why, _sid, _d in link.reasons))}
            w.edge(src, project, type_, props, confidence=link.confidence,
                   source_item_id=next((sid for _w, sid, _d in link.reasons if sid is not None), None),
                   evidence=[(sid, why, d) for why, sid, d in link.reasons])


def _keys(s: Session) -> dict[int, str]:
    """first anchor -> the key a candidate was given, from its Project or from its row in the thread registry."""
    keys: dict[int, str] = {}
    for (key,) in s.execute(text("""
        SELECT key FROM nodes WHERE type = 'Project' AND props->>'derived' = 'true'
        UNION SELECT key FROM thread_registry WHERE origin = :x ORDER BY 1"""), {"x": EXTRACTOR}):
        first = key.rsplit(":", 1)[-1]
        if first.isdigit():
            keys.setdefault(int(first), key)
    return keys


def _nearest_files(cands: dict[str, dict], own: dict[int, list[tuple]]) -> dict[str, list[tuple[int, int, object, str]]]:
    """key -> the files (and repo activity) in its folders, each record once, for its nearest folder among every
    candidate's: [(node, record, when, folder name)]."""
    folders = {nid: (key, sp) for key, c in cands.items() for nid, how, sp in c["anchors"] if how in ("folder", "repo")}
    anchors = {nid for c in cands.values() for nid, _how, _sp in c["anchors"]}
    nearest: dict[int, tuple] = {}
    for folder in sorted(folders):
        for record, node, at in own.get(folder, []):
            if node is None or node in anchors:
                continue
            rank = (len(own[folder]), folder)  # the folder holding the fewest records is the nearest
            if node not in nearest or rank < nearest[node][0]:
                nearest[node] = (rank, folder, record, at)
    out: dict[str, list[tuple[int, int, object, str]]] = defaultdict(list)
    for node, (_rank, folder, record, at) in sorted(nearest.items()):
        key, spelling = folders[folder]
        out[key].append((node, record, at, spelling))
    return out


def run(s: Session, log=print) -> dict:
    """Rebuild every derived Project and this stage's edges from scratch, after the threads stage: a candidate a thread
    is about joins it, the others are Projects of their own. Returns {projects, joined}."""
    from igneous.graph import overrides
    from igneous.llm import visible_nodes

    names = candidates(s)
    anchor_ids = sorted({nid for anchors in names.values() for nid, _how, _sp in anchors})
    people = _people(s, anchor_ids)
    own = _own(s, anchor_ids)
    keys = _keys(s)
    cands: dict[str, dict] = {}
    for name, anchors in names.items():
        for project in _projects(anchors, people):
            spellings = Counter(sp for _nid, _how, sp in project)
            display = max(spellings.items(), key=lambda kv: (kv[1], sum(ch.isupper() for ch in kv[0]), kv[0]))[0]
            first = min(nid for nid, _how, _sp in project)
            cands[keys.get(first) or f"project:{name}:{first}"] = {"screen": name, "anchors": project,
                                                                   "display": display}
    # What a thread is about joins it, unless your merge says where it goes.
    threads_ = _threads(s)
    yours = overrides.plan(overrides.load(s))
    merged = set(yours.merges) | set(yours.merges.values())
    for key, c in sorted(cands.items()):
        ids = {nid for nid, _how, _sp in c["anchors"]}
        c["thread"] = None if f"Project:{key}" in merged else _about(
            sorted({sp for _nid, _how, sp in c["anchors"]}), ids,
            [r for nid in sorted(ids) for r in own.get(nid, [])], threads_)
    joined = sorted(key for key, c in cands.items() if c["thread"])
    alone = {key: c for key, c in cands.items() if not c["thread"]}
    visible = visible_nodes(s, [nid for c in alone.values() for nid, _how, _sp in c["anchors"]])
    shown = {c["screen"] for c in alone.values() if any(nid in visible for nid, _how, _sp in c["anchors"])}
    screened = _screened(sorted({c["screen"] for c in alone.values()}), shown)
    keep = sorted(key for key, c in alone.items() if c["screen"] in screened)

    # Rebuild this stage's output from scratch: cheap, and new evidence always reaches old entities. A candidate that
    # joined a thread keeps its key as a registry row merged into that thread.
    s.execute(text("DELETE FROM edges WHERE extractor = :x"), {"x": EXTRACTOR})
    s.execute(text("""DELETE FROM nodes WHERE type = 'Project' AND props->>'derived' = 'true'
                      AND NOT (key = ANY(:keep))"""), {"keep": keep})
    s.execute(text("DELETE FROM thread_registry WHERE origin = :x AND NOT (key = ANY(:k))"), {"x": EXTRACTOR, "k": joined})
    for key in joined:
        s.execute(text("""
            INSERT INTO thread_registry (key, name, terms, status, merged_into, origin, judged)
            VALUES (:k, :n, '{}', 'merged', :into, :x, false)
            ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, terms = '{}', status = 'merged',
                merged_into = EXCLUDED.merged_into, origin = EXCLUDED.origin, merged_by = NULL, updated_at = now()
            WHERE (thread_registry.name, thread_registry.terms, thread_registry.status, thread_registry.merged_into,
                   thread_registry.origin, thread_registry.merged_by)
                  IS DISTINCT FROM (EXCLUDED.name, '{}', 'merged', EXCLUDED.merged_into, EXCLUDED.origin, NULL)
        """), {"k": key, "n": cands[key]["display"][:120], "into": cands[key]["thread"]["key"], "x": EXTRACTOR})
    w = Writer(s, EXTRACTOR, VERSION)  # after the registry says where each joined candidate went
    w.prune()  # a link you rejected on a joined candidate's Project holds for its thread, whoever wrote it
    files = _nearest_files({key: cands[key] for key in [*keep, *joined]}, own)

    for key in joined:
        c, t = cands[key], cands[key]["thread"]
        links = Links()
        for nid, how, _sp in c["anchors"]:
            if nid in t["attached"]:
                continue  # the threads stage already holds it
            mine = [r for r, _node, _at in own.get(nid, []) if r in t["records"]] or \
                [r for r, _node, at in own.get(nid, []) if _theirs(t, r, at)]
            for record in mine[-EVIDENCE:] or [None]:
                links.add(nid, "PART_OF", how, 0.9, record)
        # Its folders' files from while the thread happened (the ones it holds are linked already): a file of another
        # time in the same folder is no part of it.
        for node, record, at, folder in files.get(key, []):
            if record not in t["records"] and _theirs(t, record, at):
                links.add(node, "PART_OF", "in its folder", 0.9, record, folder=folder)
        links.write(w, t["id"])
        if log:
            log(f"  {c['display'][:40]:40s} joined thread {t['key']}")

    me = identity.members(s, me_id) if (me_id := identity.self_id(s)) else []
    siblings = Counter(cands[key]["screen"] for key in [*keep, *joined])  # how many projects share each name
    for project_key in keep:
        c = cands[project_key]
        n = c["screen"]
        pid = w.project(project_key, c["display"], {"derived": True, "named_from": "records",
                                                    "anchors": sorted({how for _nid, how, _sp in c["anchors"]})})
        links = Links()
        anchors = {nid for nid, _how, _sp in c["anchors"]}
        for nid, how, _spelling in c["anchors"]:  # structural members
            links.add(nid, "PART_OF", how, 1.0)
        for node, record, _at, folder in files.get(project_key, []):
            links.add(node, "PART_OF", "in its folder", 0.9, record, folder=folder)
        # Its own calendar series and group chats list who is in it: direct evidence.
        listed = set().union(*(people.get(nid, set()) for nid, how, _sp in c["anchors"]
                               if how.startswith(("event series", "repeated event")) or how == "group chat"))
        group = set().union(*(people.get(nid, set()) for nid in anchors))
        inside = anchors | {node for nid in anchors for _r, node, _at in own.get(nid, []) if node}

        # Mentions anywhere: phrase match on chunk text, one per item (Message/File/Event node). Marketing never counts,
        # a notice sent to you, a search, a page or a place is evidence at most, and the rest must show it belongs.
        hits = s.execute(text("""
            SELECT DISTINCT ON (c.node_id) c.node_id, c.source_item_id, c.text, COALESCE(si.item_class, ''),
                   si.source, si.occurred_at
            FROM chunks c JOIN source_items si ON si.id = c.source_item_id
            WHERE c.node_id IS NOT NULL AND c.tsv @@ phraseto_tsquery('english', :q)
            ORDER BY c.node_id, c.ord LIMIT :lim
        """), {"q": n, "lim": MAX_ITEMS_PER_ENTITY}).all()
        pat = re.compile(re.escape(n).replace(r"\ ", r"[\s\-_]+"), re.I)
        found, notices = [], []
        for node_id, sid, body, cls, source, at in hits:
            if cls != "marketing" and (m := pat.search(body or "")):
                hit = (node_id, sid, body, m, source, at)
                (notices if cls == "notification" or source in PLACES else found).append(hit)
        on = _people(s, [hit[0] for hit in found])
        with_you = _with_you(s, [hit[0] for hit in found if hit[0] not in inside], me)
        found = [hit for hit in found
                 if hit[0] in inside or on.get(hit[0], set()) & group or hit[0] in with_you or hit[4] in YOURS]
        if siblings[n] > 1:
            # One name, several projects: a mention belongs to the ones whose people are on it (a notice to none).
            found, notices = [hit for hit in found if on.get(hit[0], set()) & group], []
        for node_id, sid, body, m, _source, _at in found:
            links.add(node_id, "PART_OF", "mentions", 0.7, sid, quote=body[max(0, m.start() - 80):m.end() + 80])
        # Its evidence, never a member: a notice, search, page or place naming it while it went on (within PAD of its
        # own records and the mentions that belong).
        dates = sorted([at for nid in anchors for _r, _node, at in own.get(nid, []) if at] + [h[5] for h in found if h[5]])
        for node_id, sid, body, m, _source, at in notices:
            if dates and at and dates[0] - PAD <= at <= dates[-1] + PAD:
                links.add(node_id, "RELATED_TO", "names it while it went on", 0.5, sid, role="evidence",
                          quote=body[max(0, m.start() - 80):m.end() + 80])

        # Group chats that keep talking about it join as a whole. The evidence is the matching message items.
        for thread_id, item_ids in s.execute(text("""
            SELECT it.dst, array_agg(DISTINCT m.source_item_id ORDER BY m.source_item_id)
            FROM nodes m JOIN edges it ON it.src = m.id AND it.type = 'IN_THREAD'
            JOIN nodes t ON t.id = it.dst AND t.type = 'Thread'
            JOIN source_items si ON si.id = m.source_item_id
            WHERE m.id = ANY(:mentions)
            GROUP BY it.dst HAVING count(DISTINCT date(si.occurred_at)) >= :min
        """), {"mentions": [hit[0] for hit in found], "min": MIN_CHAT_DAYS}):
            links.add(thread_id, "PART_OF", "chat mentions it", 0.8, item_ids[0], items=item_ids)

        # People: roles and source items are retained as evidence, never inferred without observations.
        for person_id, count, roles, item_ids in s.execute(text(f"""
            WITH items AS (SELECT unnest(CAST(:items AS bigint[])) AS i),
            observations AS (
                SELECT e.src AS person, e.dst AS item, e.source_item_id, 'sender' AS role FROM edges e JOIN items a ON a.i = e.dst WHERE e.type = 'SENT'
                UNION ALL SELECT e.dst, e.src, e.source_item_id, 'recipient' FROM edges e JOIN items a ON a.i = e.src WHERE e.type = 'RECEIVED'
                UNION ALL SELECT e.src, e.dst, e.source_item_id, 'attendee' FROM edges e JOIN items a ON a.i = e.dst WHERE e.type = 'ATTENDED'
            ), canon AS (SELECT {identity.head_sql('o.person')} AS person, o.item, o.source_item_id, o.role
                         FROM observations o)
            SELECT canon.person, count(DISTINCT canon.item), array_agg(DISTINCT canon.role ORDER BY canon.role),
                   array_agg(DISTINCT canon.source_item_id ORDER BY canon.source_item_id)
                       FILTER (WHERE canon.source_item_id IS NOT NULL)
            FROM canon JOIN nodes n ON n.id = canon.person AND n.type = 'Person'
            WHERE NOT COALESCE((n.props->>'self')::bool, false) AND COALESCE(n.props->>'kind', '') <> 'service'
            GROUP BY canon.person HAVING count(DISTINCT canon.item) >= :min
        """), {"items": sorted({src for src, type_ in links if type_ == "PART_OF"}), "min": MIN_PEOPLE_ITEMS}):
            extra = {"direct": True} if person_id in listed else {}
            links.add(person_id, "INVOLVED_IN", "observed involvement", 0.8, item_ids[0] if item_ids else None,
                      items=count, roles=roles, **extra)
        links.write(w, pid)
        if log:
            log(f"  {c['display'][:40]:40s} anchors={len(c['anchors'])} mentions={len(found)}")
    return {"projects": len(keep), "joined": len(joined)}


def _with_you(s: Session, nodes: list[int], me: list[int]) -> set[int]:
    """The nodes among `nodes` you are in: you sent, got or attended them, under any of your addresses."""
    if not nodes or not me:
        return set()
    return set(s.execute(text("""
        SELECT e.dst FROM edges e WHERE e.dst = ANY(:n) AND e.type IN ('SENT', 'ATTENDED') AND e.src = ANY(:me)
        UNION SELECT e.src FROM edges e WHERE e.src = ANY(:n) AND e.type = 'RECEIVED' AND e.dst = ANY(:me)
    """), {"n": nodes, "me": me}).scalars())
