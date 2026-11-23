"""Derived entities: the things your records are about ("Gloomhaven Night", "Igneous", "Acme Client").

Nothing is configured. A name becomes a candidate when the data itself treats it as a thing you return to:
  - a recurring calendar series (3+ occurrences), or a title you put on 3+ events
  - a named group chat active on 5+ days
  - a Drive / local folder holding 2+ files, unless it is a filesystem root or a generic name ("Downloads", "src")
  - the repo an AI coding session ran in
Generic names ("Lunch", "1:1", "Office hours") are dropped by a stop-list, and by a one-time, cached model check
for names that appear in opted-in sources when the model is enabled.

Anchors that share a name are one `Project`, shown by the name's most common spelling, unless the people on them
(never you, never a service) show they are different things: groups of two or more people with nobody in common.
Every item that mentions the name (by phrase match on its chunks, across all sources) gets a PART_OF edge with the
matched span as evidence; when one name is several projects, a mention goes to the ones whose people are on it. The
anchors themselves link structurally (confidence 1.0), and a link keeps every reason it has. People involved in 2+
of its items get INVOLVED_IN with a count. A group chat that mentions it on 5+ days is linked as a whole: that's how
"Gloomhaven Night" on the calendar finds the group chat, and the chat finds who else plays.

Every edge here is written with extractor 'entities', and the stage deletes and rebuilds its own edges on every run,
so new data (a new mention, a new chat day) updates old entities. Corrections made in the Inspector live in
graph/overrides.py and are replayed by the pipeline's last step.
"""
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings
from igneous.graph import identity

EXTRACTOR = "entities"
VERSION = "1"
MIN_EVENT_OCC = 3
MIN_CHAT_DAYS = 5
MIN_PEOPLE_ITEMS = 2
MIN_GROUP = 2  # people on an anchor before they can show it is a different thing from a namesake
MAX_ITEMS_PER_ENTITY = 2000

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
    "icloud drive", "mobile documents", "com~apple~clouddocs", "library", "applications", "users",
}

CLEAN = re.compile(r"\s*[\(\[].*?[\)\]]\s*|\s+[-\u2013\u2014:|]\s+.*$")


def _norm(name: str) -> str:
    """'Gloomhaven Night (session 4)' -> 'gloomhaven night'; 'Igneous - weekly sync' -> 'igneous'."""
    n = CLEAN.sub("", name or "").strip().lower()
    n = re.sub(r"\b(weekly|biweekly|daily|monthly|session|meeting|sync|standup|call)\b|#?\b\d+\b", " ", n)
    return re.sub(r"\s+", " ", n).strip(" -:")


def _usable(n: str) -> bool:
    return (3 <= len(n) <= 48 and n not in GENERIC and not re.fullmatch(r"[\d\s:/.-]+", n) and "@" not in n
            and not re.fullmatch(r"[0-9a-f]{8,}", n))  # a hash (a filesystem root's key) names nothing


def _spelling(name: str) -> str:
    """How an anchor spells its name, without the parts _norm drops: 'Gloomhaven Night (session 4)' ->
    'Gloomhaven Night'."""
    return CLEAN.sub("", name or "").strip() or name


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

    def write(self, s: Session, project: int) -> None:
        for (src, type_), link in self.items():
            detail = {k: v for _why, _sid, d in link.reasons for k, v in d.items() if k != "why"}
            props = {**detail, "why": "; ".join(dict.fromkeys(why for why, _sid, _d in link.reasons))}
            edge = s.execute(text("""
                INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence, source_item_id)
                VALUES (:src, :dst, :t, CAST(:p AS jsonb), :x, :v, :c, :sid) RETURNING id
            """), {"src": src, "dst": project, "t": type_, "p": json.dumps(props), "x": EXTRACTOR, "v": VERSION,
                   "c": link.confidence,
                   "sid": next((sid for _w, sid, _d in link.reasons if sid is not None), None)}).scalar_one()
            s.execute(text("""
                INSERT INTO edge_evidence (edge_id, source_item_id, kind, detail)
                VALUES (:edge, :sid, :kind, CAST(:detail AS jsonb))
            """), [{"edge": edge, "sid": sid, "kind": why, "detail": json.dumps(d)} for why, sid, d in link.reasons])


def run(s: Session, log=print) -> int:
    """Rebuild every derived Project and this stage's edges from scratch; returns how many projects there are."""
    from igneous.llm import visible_nodes

    names = candidates(s)
    people = _people(s, sorted({nid for anchors in names.values() for nid, _how, _sp in anchors}))
    cands = {}
    for name, anchors in names.items():
        for project in _projects(anchors, people):
            spellings = Counter(sp for _nid, _how, sp in project)
            display = max(spellings.items(), key=lambda kv: (kv[1], sum(ch.isupper() for ch in kv[0]), kv[0]))[0]
            cands[f"{name}:{min(nid for nid, _how, _sp in project)}"] = {"screen": name, "anchors": project,
                                                                          "display": display}
    visible = visible_nodes(s, [nid for c in cands.values() for nid, _how, _sp in c["anchors"]])
    shown = {c["screen"] for c in cands.values() if any(nid in visible for nid, _how, _sp in c["anchors"])}
    screened = _screened(sorted({c["screen"] for c in cands.values()}), shown)
    keep = sorted(key for key, c in cands.items() if c["screen"] in screened)
    # Rebuild this stage's output from scratch: cheap, and new evidence always reaches old entities.
    s.execute(text("DELETE FROM edges WHERE extractor = :x"), {"x": EXTRACTOR})
    s.execute(text("""DELETE FROM nodes WHERE type = 'Project' AND props->>'derived' = 'true'
                      AND NOT (key = ANY(:keep))"""), {"keep": [f"project:{key}" for key in keep]})
    siblings = Counter(cands[key]["screen"] for key in keep)  # how many projects share each name

    for project_key in keep:
        c = cands[project_key]
        n = c["screen"]
        pid = s.execute(text("""
            INSERT INTO nodes (type, key, name, props) VALUES ('Project', :k, :name, CAST(:p AS jsonb))
            ON CONFLICT (type, key) DO UPDATE SET props = nodes.props || EXCLUDED.props RETURNING id
        """), {"k": f"project:{project_key}", "name": c["display"],
               "p": json.dumps({"derived": True, "original_name": c["display"],
                                "anchors": sorted({how for _nid, how, _sp in c["anchors"]})})}).scalar_one()
        links = Links()
        for nid, how, _spelling in c["anchors"]:  # structural members
            links.add(nid, "PART_OF", how, 1.0)

        # Mentions anywhere: phrase match on chunk text, one edge per item (Message/File/Event node).
        hits = s.execute(text("""
            SELECT DISTINCT ON (c.node_id) c.node_id, c.source_item_id, c.text
            FROM chunks c WHERE c.node_id IS NOT NULL AND c.tsv @@ phraseto_tsquery('english', :q)
            ORDER BY c.node_id, c.ord LIMIT :lim
        """), {"q": n, "lim": MAX_ITEMS_PER_ENTITY}).all()
        pat = re.compile(re.escape(n).replace(r"\ ", r"[\s\-_]+"), re.I)
        found = [(node_id, sid, body, m) for node_id, sid, body in hits if (m := pat.search(body or ""))]
        if siblings[n] > 1:
            # One name, several projects: a mention belongs to the ones whose people are on it.
            group = set().union(*(people.get(nid, set()) for nid, _how, _sp in c["anchors"]))
            on = _people(s, [node_id for node_id, *_ in found])
            found = [hit for hit in found if on.get(hit[0], set()) & group]
        for node_id, sid, body, m in found:
            links.add(node_id, "PART_OF", "mentions", 0.7, sid, quote=body[max(0, m.start() - 80):m.end() + 80])

        # Group chats that keep talking about it join as a whole. The evidence is the matching message items.
        for thread_id, item_ids in s.execute(text("""
            SELECT it.dst, array_agg(DISTINCT m.source_item_id ORDER BY m.source_item_id)
            FROM nodes m JOIN edges it ON it.src = m.id AND it.type = 'IN_THREAD'
            JOIN nodes t ON t.id = it.dst AND t.type = 'Thread'
            JOIN source_items si ON si.id = m.source_item_id
            WHERE m.id = ANY(:mentions)
            GROUP BY it.dst HAVING count(DISTINCT date(si.occurred_at)) >= :min
        """), {"mentions": [node_id for node_id, *_ in found], "min": MIN_CHAT_DAYS}):
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
            links.add(person_id, "INVOLVED_IN", "observed involvement", 0.8, item_ids[0] if item_ids else None,
                 items=count, roles=roles)
        links.write(s, pid)
        if log:
            log(f"  {c['display'][:40]:40s} anchors={len(c['anchors'])} mentions={len(found)}")
    return len(keep)
