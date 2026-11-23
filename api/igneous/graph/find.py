"""Explorer "Jump to" search and the pickers: things, not rows (`GET /graph/find`).

Returns at most a handful of entities (people, projects, group conversations, events, organizations, places, folders
and repositories, files), each identity once as its head, never a message, a service or a hidden node, optionally only
some types. Near spellings match through the trigram index on names (pg_trgm, migration 0008). Ranking is text-match
quality first (exact > prefix > word prefix > substring > fuzzy), then closeness to the centre. Repository-shaped
people are excluded; unconfirmed identifier-only people follow named people and two-way contacts. A person's line says
how many messages they share with you, the owner, whoever the centre is.
"""
import math
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph import identity
from igneous.graph.traversal import category, label, names
from igneous.graph.visibility import not_service, visible

TYPES = ("Person", "Project", "Thread", "Event", "Organization", "Place", "Folder", "File")
LIMIT = 8
CANDIDATES = 150
FUZZY = 0.35  # trigram similarity a near spelling needs
GENERIC_THREADS = ("(no subject)", "")
REPOSITORY_NAME = r"^[[:alnum:]_.-]+/[[:alnum:]_.-]+$"


def _bare_handle(name: str, key: str, props: dict) -> bool:
    """An identifier used as a label, rather than a name learned from a person/contact."""
    if props.get("named_by") in ("contacts", "linkedin", "user") or props.get("renamed"):
        return False
    label_ = name.strip().casefold()
    identities = {key.casefold(), str(props.get("github_login", "")).casefold()}
    if key.startswith("github:"):
        identities.add(key.split(":", 1)[1].casefold())
    elif "@" in key:
        identities.add(key.split("@", 1)[0].casefold())
    return label_ in identities


def _two_way(s: Session, heads: list[int]) -> set[int]:
    """Candidate identities with reciprocal conversation, including their canonical aliases."""
    if not heads:
        return set()
    return set(s.execute(text(f"""
        WITH identities AS (
            SELECT id AS node, id AS head FROM nodes WHERE id = ANY(:heads)
            UNION SELECT node_id, canonical_id FROM canonical WHERE canonical_id = ANY(:heads)
        ), traffic AS (
            SELECT p.head, COALESCE(c.canonical_id, r.dst) AS peer, true AS outbound
            FROM identities p JOIN edges e ON e.src = p.node AND e.type = 'SENT'
            JOIN edges r ON r.src = e.dst AND r.type = 'RECEIVED'
            JOIN nodes m ON m.id = e.dst AND {visible('m')}
            LEFT JOIN canonical c ON c.node_id = r.dst
            UNION ALL
            SELECT p.head, COALESCE(c.canonical_id, e.src), false
            FROM identities p JOIN edges r ON r.dst = p.node AND r.type = 'RECEIVED'
            JOIN edges e ON e.dst = r.src AND e.type = 'SENT'
            JOIN nodes m ON m.id = r.src AND {visible('m')}
            LEFT JOIN canonical c ON c.node_id = e.src
        )
        SELECT head FROM traffic WHERE head <> peer GROUP BY head, peer
        HAVING bool_or(outbound) AND bool_or(NOT outbound)
    """), {"heads": heads}).scalars())


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


def shared_messages(s: Session, centre: int, people: list[int]) -> dict[int, tuple[int, int]]:
    """{person: (visible messages they share with `centre`, of which in the last 30 days)} for each of `people`
    (identity heads) who shares any. Starts from the centre's own messages."""
    if not people:
        return {}
    rows = s.execute(text(f"""
        WITH mine AS (SELECT DISTINCT m.id AS m, si.occurred_at AS at FROM (
                          SELECT dst AS m FROM edges WHERE src = ANY(:me) AND type = 'SENT'
                          UNION ALL SELECT src FROM edges WHERE dst = ANY(:me) AND type = 'RECEIVED') x
                      JOIN nodes m ON m.id = x.m AND {visible('m')} JOIN source_items si ON si.id = m.source_item_id),
        parts AS (SELECT mine.m, mine.at, COALESCE(c.canonical_id, e.src) AS p
                  FROM mine JOIN edges e ON e.dst = mine.m AND e.type = 'SENT' LEFT JOIN canonical c ON c.node_id = e.src
                  UNION
                  SELECT mine.m, mine.at, COALESCE(c.canonical_id, e.dst)
                  FROM mine JOIN edges e ON e.src = mine.m AND e.type = 'RECEIVED' LEFT JOIN canonical c ON c.node_id = e.dst)
        SELECT p, count(*), count(*) FILTER (WHERE at > now() - interval '30 days')
        FROM parts WHERE p = ANY(:people) AND p <> :centre GROUP BY p
    """), {"me": identity.members(s, centre), "centre": centre, "people": people}).all()
    return {p: (n, r) for p, n, r in rows}


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def find(s: Session, q: str, seed: int | None = None, limit: int = LIMIT, types: list[str] | None = None) -> list[dict]:
    """The best few things named like `q`, ranked by match and then by closeness to `seed` (default: you), only of
    `types` when given. [{id, type, name, category, context, score}]. Raises identity.NotFound for an unknown seed."""
    q = " ".join(q.split())
    if len(q) < 2:
        return []
    wanted = [t for t in TYPES if not types or t in types]
    owner = identity.self_id(s)
    centre = identity.node(s, seed)["id"] if seed is not None else owner
    # Near spellings: `%` is pg_trgm's similarity test, served by the trigram index on nodes.name (migration 0008).
    s.execute(text("SELECT set_config('pg_trgm.similarity_threshold', :t, true)"), {"t": str(FUZZY)})
    rows = s.execute(text(f"""
        SELECT n.id AS alias, h.type, n.name AS alias_name, n.key, h.id AS head, h.name AS head_name, h.key AS head_key,
               h.props AS head_props, similarity(n.name, :q) AS sim
        FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id JOIN nodes h ON h.id = COALESCE(c.canonical_id, n.id)
        WHERE n.type = ANY(:types) AND {visible("n")} AND {visible("h")} AND {not_service("n")} AND {not_service("h")}
          AND NOT (n.type = 'Person' AND (n.name ~ :repo OR h.name ~ :repo
                   OR n.key ~ :repo_key OR h.key ~ :repo_key))
          AND NOT (n.type = 'Thread' AND n.name = ANY(:generic))
          AND (n.name ILIKE :like OR (n.type = 'Person' AND n.key ILIKE :like) OR n.name % :q)
        ORDER BY (lower(n.name) = lower(:q)) DESC, (n.name ILIKE :prefix) DESC, length(n.name), n.id
        LIMIT :cands
    """), {"types": wanted, "generic": list(GENERIC_THREADS), "q": q, "like": f"%{q}%", "prefix": f"{q}%",
           "cands": CANDIDATES, "repo": REPOSITORY_NAME,
           "repo_key": "^github:" + REPOSITORY_NAME[1:]}).mappings().all()
    if not rows:
        return []

    # One entry per identity, scored by its best-matching alias (a person found by an old address is still shown
    # under their real name).
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
            best[r["head"]] = {"id": r["head"], "type": r["type"], "name": r["head_name"], "quality": quality,
                               "kind": (r["head_props"] or {}).get("kind"),
                               "bare_handle": r["type"] == "Person" and _bare_handle(
                                   r["head_name"], r["head_key"], r["head_props"])}
    cands = list(best.values())
    ids = [c["id"] for c in cands]
    people = [c["id"] for c in cands if c["type"] == "Person"]
    near = shared_messages(s, centre, people) if centre is not None else {}
    yours = near if centre == owner else shared_messages(s, owner, people) if owner is not None else {}
    two_way = _two_way(s, [c["id"] for c in cands if c["bare_handle"]])
    # How much each candidate is in the graph at all: a person's visible messages (every identity), a thing's links.
    size = dict(s.execute(text(f"""
        WITH ids AS (SELECT COALESCE(c.node_id, x) AS id, x AS head FROM unnest(CAST(:ids AS bigint[])) x
                     LEFT JOIN canonical c ON c.canonical_id = x)
        SELECT head, count(DISTINCT m.id) FROM (
            SELECT ids.head, e.dst AS o FROM ids JOIN edges e ON e.src = ids.id AND e.type = 'SENT'
            UNION ALL SELECT ids.head, e.src FROM ids JOIN edges e ON e.dst = ids.id AND e.type = 'RECEIVED') x
        JOIN nodes m ON m.id = x.o AND {visible('m')} GROUP BY head
    """), {"ids": people}).all()) if people else {}
    degree = dict(s.execute(text("""
        SELECT x, count(*) FROM (SELECT src AS x FROM edges WHERE src = ANY(:ids) UNION ALL
                                 SELECT dst FROM edges WHERE dst = ANY(:ids)) e GROUP BY x
    """), {"ids": [i for i in ids if i not in size]}).all())
    # Who is in each group conversation, organization or project, for the candidates that need a context line.
    members = dict(s.execute(text(f"""
        SELECT t, count(DISTINCT COALESCE(c.canonical_id, p)) FROM (
            SELECT it.dst AS t, x.src AS p FROM edges it JOIN nodes m ON m.id = it.src AND {visible('m')}
            JOIN edges x ON x.dst = it.src AND x.type = 'SENT'
            WHERE it.type = 'IN_THREAD' AND it.dst = ANY(:ids)
            UNION ALL SELECT dst, src FROM edges WHERE dst = ANY(:ids) AND type IN ('INVOLVED_IN', 'ATTENDED', 'MEMBER_OF')
        ) m LEFT JOIN canonical c ON c.node_id = m.p GROUP BY t
    """), {"ids": ids}).all())
    named = names(s)

    out = []
    for c in cands:
        t, n_members = c["type"], int(members.get(c["id"], 0))
        msgs, _recent = near.get(c["id"], (0, 0))
        closeness = math.log1p(msgs) / 6 if msgs else math.log1p(size.get(c["id"], degree.get(c["id"], 0))) / 12
        # Match quality dominates (a name that starts with what you typed always beats one that merely contains it:
        # "drew" finds Drew Lee before Andrew); unconfirmed handles are a lower tier below these matches.
        c["score"] = round(c["quality"] * 10 + min(1.0, closeness) * (1.0 if t == "Person" else 0.6), 4)
        if c["bare_handle"] and c["id"] not in two_way:
            c["score"] -= 11  # keep searchable, after named people and reciprocal correspondents
        c["category"] = category(t, c["kind"])
        c["name"] = label(c["name"], t, named)
        if t == "Person":
            with_you, recent = yours.get(c["id"], (0, 0))
            c["context"] = ("You" if c["id"] == owner else
                            f"{_plural(recent, 'message')} with you this month" if recent else
                            f"{_plural(with_you, 'message')} with you" if with_you else
                            f"{_plural(size[c['id']], 'message')}" if size.get(c["id"]) else "Person")
        elif t == "Thread":
            c["context"] = f"Group conversation · {_plural(n_members, 'person', 'people')}"
        else:
            kind = {"Organization": "Organization", "Folder": "Folder"}.get(t, c["category"] or t)
            c["context"] = f"{kind} · {_plural(n_members, 'person', 'people')}" if n_members else kind
        out.append(c)
    out.sort(key=lambda c: (-c["score"], c["name"].lower(), c["id"]))
    return [{k: c[k] for k in ("id", "type", "name", "category", "context", "score")} for c in out[:limit]]

