"""Derived entities: the things your records are about ("Draw Steel", "Igneous", "Dealer Tire").

Nothing is configured. A name becomes a candidate when the data itself treats it as a thing you return to:
  - a recurring calendar series (3+ occurrences), or a title you put on 3+ events
  - a named group chat
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
import json
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph import overrides

EXTRACTOR = "entities"
VERSION = "1"
MIN_EVENT_OCC = 3
MIN_CHAT_DAYS = 2
MIN_PEOPLE_ITEMS = 2
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
             AND n.key = 'gcal:' || COALESCE(si.raw_meta->>'series_key', si.external_id)
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

    # Named group chats (display names are chosen by people, so they're meaningful).
    for nid, name in s.execute(text("""
        SELECT id, name FROM nodes WHERE type = 'Thread' AND (props->>'group')::bool
          AND name !~ '[0-9]{7,}' AND name !~ '@' AND name NOT LIKE '%,%'
    """)):
        add(name, nid, "group chat")

    # Folders with 2+ files, and repos AI sessions ran in.
    for nid, name, how in s.execute(text("""
        SELECT f.id, f.name, CASE WHEN f.props->>'kind' = 'repo' THEN 'repo' ELSE 'folder' END
        FROM nodes f WHERE f.type = 'Folder' AND (
            f.props->>'kind' = 'repo'
            OR (SELECT count(*) FROM edges e WHERE e.src = f.id AND e.type = 'CONTAINS') >= 2)
    """)):
        add(name.rsplit("/", 1)[-1], nid, how)

    # A name has to come from 2+ anchors or 2+ kinds of anchor, unless it's a group chat or repo (a single
    # named chat is already a deliberate grouping).
    return {n: c for n, c in out.items()
            if len(c["anchors"]) >= 2 or any(h in ("group chat", "repo") for _, h in c["anchors"])}


def _screen(names: list[str]) -> set[str]:
    """Ask the model, once per name (cached in pipeline_state), which names are a specific thing (a project,
    game, club, course, org, product) rather than a generic activity. Without the model, keep them all."""
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
        return set(names)


def _screened(s: Session, names: list[str]) -> set[str]:
    """The screen verdicts are cached per database (in git-ignored data/), so each name costs one model call ever."""
    from pathlib import Path

    from igneous.config import settings
    db = str(s.get_bind().url.database)
    path = Path(settings.data_dir) / f"entities_screen.{db}.json"
    seen: dict[str, bool] = json.loads(path.read_text()) if path.exists() else {}
    todo = [n for n in names if n not in seen]
    if todo:
        keep = _screen(todo)
        seen.update({n: n in keep for n in todo})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen, indent=0, sort_keys=True))
    return {n for n in names if seen.get(n)}


def _edge(s: Session, src: int, dst: int, type_: str, props: dict, sid: int | None, conf: float,
          span: tuple[int, int] | None = None):
    s.execute(text("""
        INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence, source_item_id,
                           span_start, span_end)
        VALUES (:src, :dst, :t, CAST(:p AS jsonb), :x, :v, :c, :sid, :a, :b)
        ON CONFLICT (src, dst, type, extractor) DO UPDATE SET props = EXCLUDED.props,
            confidence = GREATEST(edges.confidence, EXCLUDED.confidence)
    """), {"src": src, "dst": dst, "t": type_, "p": json.dumps(props), "x": EXTRACTOR, "v": VERSION, "c": conf,
           "sid": sid, "a": span[0] if span else None, "b": span[1] if span else None})


def run(s: Session, log=print) -> int:
    cands = candidates(s)
    keep = _screened(s, sorted(cands))
    # Rebuild this stage's output from scratch: cheap, and new evidence always reaches old entities.
    s.execute(text("DELETE FROM edges WHERE extractor = :x"), {"x": EXTRACTOR})
    s.execute(text("""DELETE FROM nodes WHERE type = 'Project' AND props->>'derived' = 'true'
                      AND NOT (key = ANY(:keep))"""), {"keep": [f"project:{n}" for n in keep]})
    built = 0
    for n in sorted(keep):
        c = cands[n]
        pid = s.execute(text("""
            INSERT INTO nodes (type, key, name, props) VALUES ('Project', :k, :name, CAST(:p AS jsonb))
            ON CONFLICT (type, key) DO UPDATE SET props = nodes.props || EXCLUDED.props RETURNING id
        """), {"k": f"project:{n}", "name": c["display"],
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

        # Group chats that keep talking about it join as a whole.
        s.execute(text("""
            INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
            SELECT it.dst, :pid, 'PART_OF', jsonb_build_object('why', 'chat mentions it', 'days', count(*)),
                   :x, :v, 0.8
            FROM edges pe JOIN edges it ON it.src = pe.src AND it.type = 'IN_THREAD'
            JOIN nodes t ON t.id = it.dst AND t.type = 'Thread'
            WHERE pe.dst = :pid AND pe.type = 'PART_OF' AND pe.extractor = :x
            GROUP BY it.dst HAVING count(*) >= :min
            ON CONFLICT (src, dst, type, extractor) DO NOTHING
        """), {"pid": pid, "x": EXTRACTOR, "v": VERSION, "min": MIN_CHAT_DAYS})

        # People: whoever sent, received or attended 2+ of its items (canonical identities), not you.
        s.execute(text("""
            WITH items AS (SELECT src AS i FROM edges WHERE dst = :pid AND type = 'PART_OF' AND extractor = :x),
            thread_items AS (SELECT it.src AS i FROM edges it JOIN items ON it.dst = items.i AND it.type = 'IN_THREAD'),
            all_items AS (SELECT i FROM items UNION SELECT i FROM thread_items),
            ppl AS (
                SELECT e.src AS p, e.dst AS i FROM edges e JOIN all_items a ON a.i = e.dst WHERE e.type IN ('SENT', 'ATTENDED')
                UNION SELECT e.dst, e.src FROM edges e JOIN all_items a ON a.i = e.src WHERE e.type = 'RECEIVED'),
            canon AS (SELECT COALESCE(c.canonical_id, ppl.p) AS p, ppl.i FROM ppl LEFT JOIN canonical c ON c.node_id = ppl.p)
            INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
            SELECT canon.p, :pid, 'INVOLVED_IN', jsonb_build_object('items', count(DISTINCT canon.i)), :x, :v, 0.8
            FROM canon JOIN nodes n ON n.id = canon.p AND n.type = 'Person'
            WHERE NOT COALESCE((n.props->>'self')::bool, false) AND COALESCE(n.props->>'kind', '') <> 'service'
            GROUP BY canon.p HAVING count(DISTINCT canon.i) >= :min
            ON CONFLICT (src, dst, type, extractor) DO NOTHING
        """), {"pid": pid, "x": EXTRACTOR, "v": VERSION, "min": MIN_PEOPLE_ITEMS})
        built += 1
        log(f"  {c['display'][:40]:40s} anchors={len(c['anchors'])} mentions={mentioned}") if log else None
    overrides.apply(s)  # corrections (rename / hide / merge / unlink) always win, so they survive every rebuild
    s.commit()
    return built
