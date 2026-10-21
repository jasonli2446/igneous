"""Derived entities: the things your records are about ("Draw Steel", "Igneous", "Dealer Tire").

Nothing is configured. A name becomes a candidate when the data itself treats it as a thing you return to:
  - a recurring calendar series (3+ occurrences), or a title you put on 3+ events
  - a named group chat active on 5+ days
  - a Drive / local folder holding 2+ files
  - the repo an AI coding session ran in
Generic names ("Lunch", "1:1", "Office hours") are dropped by document frequency and by a one-time, cached
LLM check when the model is enabled (otherwise a stop-list).

Each surviving name becomes one `Project` node. Every item that mentions it (by phrase match on its chunks,
across all sources) gets a PART_OF edge to it with the matched span as evidence; the anchors themselves link
structurally (confidence 1.0). People involved in 2+ of its items get INVOLVED_IN with a count. A group chat
that mentions it on 2+ days is linked as a whole: that's how "Draw Steel" on the calendar finds the group
chat, and the chat finds who else plays.

Corrections made in the Inspector live in graph/overrides.py and are replayed last on every run.

Idempotent: every edge here is written with extractor='entities' and the stage deletes and rebuilds its own
edges, so new data (a new mention, a new chat day) updates old entities on the next run.
"""
import hashlib
import json
import re
from itertools import combinations

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings
from igneous.graph import overrides

EXTRACTOR = "entities"
VERSION = "1"
MIN_EVENT_OCC = 3
MIN_CHAT_DAYS = 5
MIN_PEOPLE_ITEMS = 2
MIN_ANCHOR_KINDS = 2
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

CLEAN = re.compile(r"\s*[\(\[].*?[\)\]]\s*|\s+[-\u2013\u2014:|]\s+.*$")


def _norm(name: str) -> str:
    """'Draw Steel (session 4)' -> 'draw steel'; 'Igneous - weekly sync' -> 'igneous'."""
    n = CLEAN.sub("", name or "").strip().lower()
    n = re.sub(r"\b(weekly|biweekly|daily|monthly|session|meeting|sync|standup|call)\b|#?\b\d+\b", " ", n)
    return re.sub(r"\s+", " ", n).strip(" -:")


def _usable(n: str) -> bool:
    return 3 <= len(n) <= 48 and n not in GENERIC and not re.fullmatch(r"[\d\s:/.-]+", n) and "@" not in n


def candidates(s: Session) -> dict[str, dict]:
    """name -> {display, anchors: [(node_id, how)]}. Anchors are the structural members."""
    out: dict[str, dict] = {}

    def add(name: str, node_id: int, how: str):
        n = _norm(name)
        if not _usable(n):
            return
        c = out.setdefault(n, {"display": CLEAN.sub("", name).strip() or name, "anchors": []})
        c["anchors"].append((node_id, how))

    # Recurring events: one node per series already; also titles repeated across non-recurring events.
    for nid, name, occ in s.execute(text("""
        SELECT n.id, n.name, count(si.id) FROM nodes n
        JOIN source_items si ON si.source = 'gcal'
             AND n.key = COALESCE(si.raw_meta->>'event_key', 'gcal:' || COALESCE(si.raw_meta->>'series_key', si.external_id))
        WHERE n.type = 'Event' GROUP BY n.id, n.name
    """)):
        add(name, nid, f"event series ({occ})") if occ >= MIN_EVENT_OCC else None
    titles: dict[str, list[int]] = {}
    for nid, name in s.execute(text("SELECT id, name FROM nodes WHERE type = 'Event'")):
        titles.setdefault(_norm(name), []).append(nid)
    for n, ids in titles.items():
        if len(ids) >= MIN_EVENT_OCC:
            for nid in ids:
                add(n, nid, "repeated event")

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
    """), {"days": MIN_CHAT_DAYS}):
        add(name, nid, "group chat")

    # Folders with 2+ files, and repos AI sessions ran in.
    for nid, name, how in s.execute(text("""
        SELECT f.id, f.name, CASE WHEN f.props->>'kind' = 'repo' THEN 'repo' ELSE 'folder' END
        FROM nodes f WHERE f.type = 'Folder' AND (
            f.props->>'kind' = 'repo'
            OR (SELECT count(*) FROM edges e WHERE e.src = f.id AND e.type = 'CONTAINS') >= 2)
    """)):
        add(name.rsplit("/", 1)[-1], nid, how)

    return out


def _shared_people(s: Session, anchor_ids: list[int]) -> bool:
    """Return whether two anchor nodes have at least two people in common.

    This is the corroboration required before normalized names are merged.  It intentionally does not infer
    membership through arbitrary graph hops: events, conversations, and repo-hosted AI conversations are the
    only direct observations that can make two anchors agree.
    """
    if len(anchor_ids) < 2:
        return False
    rows = s.execute(text("""
        WITH anchor_people AS (
            SELECT e.dst AS anchor, e.src AS person
            FROM edges e WHERE e.type = 'ATTENDED' AND e.dst = ANY(:anchors)
            UNION
            SELECT it.dst AS anchor, p.person
            FROM edges it
            JOIN (
                SELECT dst AS message, src AS person FROM edges WHERE type = 'SENT'
                UNION SELECT src, dst FROM edges WHERE type = 'RECEIVED'
            ) p ON p.message = it.src
            WHERE it.type = 'IN_THREAD' AND it.dst = ANY(:anchors)
            UNION
            SELECT container.src AS anchor, p.person
            FROM edges container
            JOIN edges it ON it.dst = container.dst AND it.type = 'IN_THREAD'
            JOIN (
                SELECT dst AS message, src AS person FROM edges WHERE type = 'SENT'
                UNION SELECT src, dst FROM edges WHERE type = 'RECEIVED'
            ) p ON p.message = it.src
            WHERE container.type = 'CONTAINS' AND container.src = ANY(:anchors)
        )
        SELECT a.anchor, b.anchor
        FROM anchor_people a
        JOIN anchor_people b ON a.person = b.person AND a.anchor < b.anchor
        GROUP BY a.anchor, b.anchor
        HAVING count(DISTINCT a.person) >= 2
    """), {"anchors": anchor_ids}).all()
    return bool(rows)


def _components(s: Session, anchors: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    """Keep strong anchors alone; merge only corroborated anchor pairs into components."""
    parent = list(range(len(anchors)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for left, right in combinations(range(len(anchors)), 2):
        if _shared_people(s, [anchors[left][0], anchors[right][0]]):
            parent[find(left)] = find(right)
    groups: dict[int, list[tuple[int, str]]] = {}
    for i, anchor in enumerate(anchors):
        groups.setdefault(find(i), []).append(anchor)
    return list(groups.values())


def _screen(names: list[str]) -> set[str]:
    """Ask the model, once per name (cached in pipeline_state), which names are a specific thing (a project,
    game, club, course, org, product) rather than a generic activity. Without the model, keep them all."""
    from igneous.llm import LLMDisabled, structured

    if not names:
        return set()
    if not settings.local_sources_model_enabled:
        # Project candidates include local folder and repository names; do not disclose even those to a model
        # until the owner opts in. The conservative generic stop-list still filters obvious non-projects.
        return set(names)
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
        return set(names)


def _screened(s: Session, names: list[str]) -> set[str]:
    """Cache one deterministic verification verdict per candidate in Postgres, not local disk."""
    kept = set()
    for name in names:
        key = "entities-screen:v1:" + hashlib.sha256(name.encode()).hexdigest()
        cached = s.execute(text("SELECT response FROM llm_cache WHERE cache_key = :key"), {"key": key}).scalar()
        if cached is None:
            verdict = name in _screen([name])
            s.execute(text("""
                INSERT INTO llm_cache (cache_key, response) VALUES (:key, CAST(:response AS jsonb))
                ON CONFLICT (cache_key) DO NOTHING
            """), {"key": key, "response": json.dumps({"keep": verdict, "name": name})})
        else:
            verdict = bool(cached.get("keep"))
        if verdict:
            kept.add(name)
    return kept


def _edge(s: Session, src: int, dst: int, type_: str, props: dict, sid: int | None, conf: float,
          span: tuple[int, int] | None = None) -> int:
    edge_id = s.execute(text("""
        INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence, source_item_id,
                           span_start, span_end)
        VALUES (:src, :dst, :t, CAST(:p AS jsonb), :x, :v, :c, :sid, :a, :b)
        ON CONFLICT (src, dst, type, extractor) DO UPDATE SET props = EXCLUDED.props,
            confidence = GREATEST(edges.confidence, EXCLUDED.confidence)
        RETURNING id
    """), {"src": src, "dst": dst, "t": type_, "p": json.dumps(props), "x": EXTRACTOR, "v": VERSION, "c": conf,
           "sid": sid, "a": span[0] if span else None, "b": span[1] if span else None}).scalar_one()
    s.execute(text("""
        INSERT INTO edge_evidence (edge_id, source_item_id, kind, detail)
        VALUES (:edge_id, :sid, :kind, CAST(:detail AS jsonb))
    """), {"edge_id": edge_id, "sid": sid, "kind": props["why"], "detail": json.dumps(props)})
    return edge_id


def run(s: Session, log=print) -> int:
    cands = candidates(s)
    # Equal names only propose a merge. Strong single anchors remain Projects; only corroborated pairs combine.
    cands = {f"{name}:{min(node_id for node_id, _ in component)}": {**candidate, "anchors": component, "screen": name}
             for name, candidate in cands.items() for component in _components(s, candidate["anchors"])}
    screened = _screened(s, sorted({candidate["screen"] for candidate in cands.values()}))
    keep = {key for key, candidate in cands.items() if candidate["screen"] in screened}
    # Rebuild this stage's output from scratch: cheap, and new evidence always reaches old entities.
    s.execute(text("DELETE FROM edges WHERE extractor = :x"), {"x": EXTRACTOR})
    s.execute(text("""DELETE FROM nodes WHERE type = 'Project' AND props->>'derived' = 'true'
                      AND NOT (key = ANY(:keep))"""), {"keep": [f"project:{n}" for n in keep]})
    built = 0
    for project_key in sorted(keep):
        c = cands[project_key]
        n = c["screen"]
        pid = s.execute(text("""
            INSERT INTO nodes (type, key, name, props) VALUES ('Project', :k, :name, CAST(:p AS jsonb))
            ON CONFLICT (type, key) DO UPDATE SET props = nodes.props || EXCLUDED.props RETURNING id
<<<<<<< HEAD
        """), {"k": f"project:{project_key}", "name": c["display"],
               "p": json.dumps({"derived": True, "original_name": c["display"],
                                "anchors": sorted({h for _, h in c["anchors"]})})}).scalar_one()

        # Structural members.
        for nid, how in c["anchors"]:
            _edge(s, nid, pid, "PART_OF", {"why": how}, None, 1.0)

        # Mentions anywhere: phrase match on chunk text, one edge per item (Message/File/Event node).
        hits = s.execute(text("""
            SELECT DISTINCT ON (c.node_id) c.node_id, c.source_item_id, c.text
            FROM chunks c WHERE c.node_id IS NOT NULL AND c.tsv @@ phraseto_tsquery('english', :q)
            ORDER BY c.node_id, c.ord LIMIT :lim
        """), {"q": n, "lim": MAX_ITEMS_PER_ENTITY}).all()
        pat = re.compile(re.escape(n).replace(r"\ ", r"[\s\-_]+"), re.I)
        mentioned = 0
        for node_id, sid, body in hits:
            m = pat.search(body or "")
            if not m:
                continue  # stemming matched a different word; keep only literal mentions
            _edge(s, node_id, pid, "PART_OF", {"why": "mentions", "quote": body[max(0, m.start() - 80):m.end() + 80]},
                  sid, 0.7)
            mentioned += 1

        # Group chats that keep talking about it join as a whole. The evidence is the matching message items.
        chats = s.execute(text("""
            SELECT it.dst, array_agg(DISTINCT pe.source_item_id) FILTER (WHERE pe.source_item_id IS NOT NULL)
            FROM edges pe JOIN edges it ON it.src = pe.src AND it.type = 'IN_THREAD'
            JOIN source_items si ON si.id = pe.source_item_id
            JOIN nodes t ON t.id = it.dst AND t.type = 'Thread'
            WHERE pe.dst = :pid AND pe.type = 'PART_OF' AND pe.extractor = :x
            GROUP BY it.dst HAVING count(DISTINCT date(si.occurred_at)) >= :min
        """), {"pid": pid, "x": EXTRACTOR, "min": MIN_CHAT_DAYS}).all()
        for thread_id, item_ids in chats:
            _edge(s, thread_id, pid, "PART_OF", {"why": "chat mentions it", "items": item_ids},
                  item_ids[0] if item_ids else None, 0.8)

        # People: roles and source items are retained as evidence, never inferred without observations.
        people = s.execute(text("""
            WITH items AS (SELECT src AS i FROM edges WHERE dst = :pid AND type = 'PART_OF' AND extractor = :x),
            observations AS (
                SELECT e.src AS person, e.dst AS item, e.source_item_id, 'sender' AS role FROM edges e JOIN items a ON a.i = e.dst WHERE e.type = 'SENT'
                UNION ALL SELECT e.dst, e.src, e.source_item_id, 'recipient' FROM edges e JOIN items a ON a.i = e.src WHERE e.type = 'RECEIVED'
                UNION ALL SELECT e.src, e.dst, e.source_item_id, 'attendee' FROM edges e JOIN items a ON a.i = e.dst WHERE e.type = 'ATTENDED'
            ), canon AS (SELECT COALESCE(c.canonical_id, o.person) AS person, o.item, o.source_item_id, o.role
                          FROM observations o LEFT JOIN canonical c ON c.node_id = o.person)
            SELECT canon.person, count(DISTINCT canon.item), array_agg(DISTINCT canon.role),
                   array_agg(DISTINCT canon.source_item_id) FILTER (WHERE canon.source_item_id IS NOT NULL)
            FROM canon JOIN nodes n ON n.id = canon.person AND n.type = 'Person'
            WHERE NOT COALESCE((n.props->>'self')::bool, false) AND COALESCE(n.props->>'kind', '') <> 'service'
            GROUP BY canon.person HAVING count(DISTINCT canon.item) >= :min
        """), {"pid": pid, "x": EXTRACTOR, "min": MIN_PEOPLE_ITEMS}).all()
        for person_id, count, roles, item_ids in people:
            _edge(s, person_id, pid, "INVOLVED_IN", {"why": "observed involvement", "items": count, "roles": roles},
                  item_ids[0] if item_ids else None, 0.8)
        built += 1
        log(f"  {c['display'][:40]:40s} anchors={len(c['anchors'])} mentions={mentioned}") if log else None
    overrides.apply(s)  # corrections (rename / hide / merge / unlink) always win, so they survive every rebuild
    s.commit()
    return built
