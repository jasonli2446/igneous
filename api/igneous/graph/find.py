"""Explorer "Jump to" search: things, not rows (`GET /graph/find`).

Returns at most a handful of entities (people, projects, group conversations, events, organizations, places,
folders, repos, files), deduped by canonical identity, never messages, services or hidden projects. Ranking is
text-match quality first (exact > prefix > word prefix > substring > fuzzy), then closeness to the seed.
"""
import math
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph.traversal import CATEGORY, _canon_head, handle_names, pretty_thread

TYPES = ("Person", "Project", "Thread", "Event", "Organization", "Place", "Folder", "Repo", "File", "Course")
LIMIT = 8
CANDIDATES = 150
GENERIC_THREADS = ("(no subject)", "")


def match_quality(q: str, name: str) -> float:
    """0..1 text match: 1 exact, .85 prefix, .7 every query word starts a word, .5 substring, else 0."""
    q, name = q.strip().lower(), (name or "").lower()
    if not q or not name:
        return 0.0
    if name == q:
        return 1.0
    if name.startswith(q):
        return 0.85
    words = re.findall(r"\w+", name)
    toks = re.findall(r"\w+", q)
    if toks and all(any(w.startswith(t) for w in words) for t in toks):
        return 0.7
    return 0.5 if q in name else 0.0


def _has_trgm(s: Session) -> bool:
    return bool(s.execute(text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm')")).scalar())


def _closeness(s: Session, seed_ids: list[int], heads: list[int]) -> dict[int, tuple[int, int]]:
    """Per candidate person: (messages shared with the seed, of which in the last 30 days)."""
    if not heads or not seed_ids:
        return {}
    canon = "COALESCE((SELECT canonical_id FROM canonical WHERE node_id = {x}), {x})"
    rows = s.execute(text(f"""
        WITH mine AS (SELECT dst AS m FROM edges WHERE src = ANY(:me) AND type = 'SENT'
                      UNION SELECT src FROM edges WHERE dst = ANY(:me) AND type = 'RECEIVED')
        SELECT p, count(*) AS n, count(*) FILTER (WHERE at > now() - interval '30 days') AS recent FROM (
            SELECT {canon.format(x='x.p')} AS p, mine.m, si.occurred_at AS at
            FROM mine JOIN (SELECT src AS p, dst AS m FROM edges WHERE type = 'SENT'
                            UNION ALL SELECT dst, src FROM edges WHERE type = 'RECEIVED') x ON x.m = mine.m
            JOIN nodes msg ON msg.id = mine.m JOIN source_items si ON si.id = msg.source_item_id
            WHERE x.p = ANY(:heads) OR x.p IN (SELECT node_id FROM canonical WHERE canonical_id = ANY(:heads))
        ) q WHERE p = ANY(:heads) GROUP BY p
    """), {"me": seed_ids, "heads": heads}).all()
    return {p: (n, r) for p, n, r in rows}


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def find(s: Session, q: str, seed: int | None = None, limit: int = LIMIT) -> list[dict]:
    q = " ".join(q.split())
    if len(q) < 2:
        return []
    trgm = _has_trgm(s)
    sim = "similarity(lower(n.name), lower(:q))" if trgm else "0.0"
    rows = s.execute(text(f"""
        SELECT n.id AS alias, n.type, n.name AS alias_name, n.key, h.id AS head, h.name AS head_name, h.key AS head_key,
               {sim} AS sim
        FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id JOIN nodes h ON h.id = COALESCE(c.canonical_id, n.id)
        WHERE n.type = ANY(:types)
          AND NOT COALESCE((n.props->>'hidden')::bool, false) AND NOT COALESCE((h.props->>'hidden')::bool, false)
          AND COALESCE(n.props->>'kind', '') <> 'service' AND COALESCE(h.props->>'kind', '') <> 'service'
          AND NOT (n.type = 'Thread' AND n.name = ANY(:generic))
          AND (n.name ILIKE :like OR (n.type = 'Person' AND n.key ILIKE :like) {"OR " + sim + " > 0.35" if trgm else ""})
        ORDER BY (lower(n.name) = lower(:q)) DESC, (n.name ILIKE :prefix) DESC, length(n.name), n.id
        LIMIT :cands
    """), {"types": list(TYPES), "generic": list(GENERIC_THREADS), "q": q, "like": f"%{q}%", "prefix": f"{q}%",
           "cands": CANDIDATES}).mappings().all()
    if not rows:
        return []

    # One entry per canonical identity, scored by its best-matching alias (a person found by an old address
    # is still shown under their real name).
    best: dict[int, dict] = {}
    for r in rows:
        quality = max(match_quality(q, r["alias_name"]), match_quality(q, r["head_name"]),
                      0.45 * match_quality(q, r["key"]) if r["type"] == "Person" else 0.0)
        if quality == 0.0:
            quality = round(0.45 * float(r["sim"]), 3)
        if quality <= 0.0:
            continue
        cur = best.get(r["head"])
        if cur is None or quality > cur["quality"]:
            best[r["head"]] = {"id": r["head"], "type": r["type"], "name": r["head_name"], "quality": quality}
    cands = list(best.values())
    # Thread-like heads can't be canonical; people always keep type Person.
    ids = [c["id"] for c in cands]
    seed_ids = []
    if seed is None:
        seed = s.execute(text("SELECT id FROM nodes WHERE type = 'Person' AND props->>'self' = 'true' LIMIT 1")).scalar()
    if seed is not None:
        head = _canon_head(s, seed)
        seed_ids = [head, *s.execute(text("SELECT node_id FROM canonical WHERE canonical_id = :h"), {"h": head}).scalars()]
    people = [c["id"] for c in cands if c["type"] == "Person"]
    shared = _closeness(s, seed_ids, people)

    degree = dict(s.execute(text("""
        SELECT x, count(*) FROM (SELECT src AS x FROM edges WHERE src = ANY(:ids) UNION ALL
                                 SELECT dst FROM edges WHERE dst = ANY(:ids)) e GROUP BY x
    """), {"ids": ids}).all())
    # Group conversations and org/project sizes, only for the candidates that need a context line.
    members = dict(s.execute(text("""
        SELECT t, count(DISTINCT p) FROM (
            SELECT it.dst AS t, c.src AS p FROM edges it JOIN edges c ON c.dst = it.src AND c.type = 'SENT'
            WHERE it.type = 'IN_THREAD' AND it.dst = ANY(:ids)
            UNION ALL SELECT dst, src FROM edges WHERE dst = ANY(:ids) AND type IN ('INVOLVED_IN', 'ATTENDED', 'MEMBER_OF')
        ) m GROUP BY t
    """), {"ids": ids}).all())
    names = handle_names(s)

    out = []
    for c in cands:
        t, n_members = c["type"], int(members.get(c["id"], 0))
        if t == "Thread" and n_members < 3:
            continue  # a two-person email chain is a pair of people, not a group
        msgs, recent = shared.get(c["id"], (0, 0))
        near = math.log1p(msgs) / 6 if msgs else math.log1p(degree.get(c["id"], 0)) / 12
        # Match quality dominates (a name that starts with what you typed always beats one that merely contains it:
        # "drew" finds Drew Tobias before Andrew); closeness only orders matches of the same quality.
        c["score"] = round(c["quality"] * 10 + min(1.0, near) * (1.0 if t == "Person" else 0.6), 4)
        c["category"] = CATEGORY.get("Group" if t == "Thread" else t)
        if t == "Person":
            c["context"] = (f"{_plural(recent, 'message')} this month" if recent else
                            f"{_plural(msgs, 'message')} with you" if msgs else
                            f"{_plural(degree.get(c['id'], 0), 'message')} in the graph" if degree.get(c["id"]) else "Person")
        elif t == "Thread":
            c["name"] = pretty_thread(c["name"], names) or c["name"]
            c["context"] = f"Group conversation · {_plural(n_members, 'person', 'people')}"
        else:
            label = {"Organization": "Organization", "Folder": "Folder", "Repo": "Repository"}.get(t, t)
            c["context"] = f"{label} · {_plural(n_members, 'person', 'people')}" if n_members else label
        out.append(c)
    out.sort(key=lambda c: (-c["score"], c["name"].lower()))
    return [{k: c[k] for k in ("id", "type", "name", "category", "context", "score")} for c in out[:limit]]
