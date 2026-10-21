"""Bounded, provenance-filtered graph traversals used by the Explorer API."""
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

# Hub nodes (a big Organization, a mass-mail Thread) would swallow the node budget; skip expanding them.
MAX_FANOUT = 200


@dataclass(frozen=True)
class GraphFilters:
    """Request filters applied both while discovering and serializing edges."""

    source: str | None = None
    edge_types: list[str] | None = None
    since: datetime | None = None
    until: datetime | None = None


NO_FILTERS = GraphFilters()


def _edge_where(filters: GraphFilters, edge: str = "e", item: str = "si") -> str:
    """Build a bound-parameter predicate for provenance-backed graph edges."""
    clauses = []
    if filters.edge_types:
        clauses.append(f"{edge}.type = ANY(:types)")
    if filters.source:
        clauses.append(f"{item}.source = :source")
    # Date filters select intervals that overlap the requested window. Point-in-time communication edges
    # have equal bounds; MEMBER_OF remains visible throughout its observed membership span.
    if filters.since:
        clauses.append(f"COALESCE({edge}.valid_to, {item}.occurred_at, {edge}.valid_from) >= :since")
    if filters.until:
        clauses.append(f"COALESCE({edge}.valid_from, {item}.occurred_at, {edge}.valid_to) <= :until")
    return " AND ".join(clauses) if clauses else "TRUE"


def _filter_params(filters: GraphFilters) -> dict:
    return {"types": filters.edge_types, "source": filters.source, "since": filters.since, "until": filters.until}


def neighborhood(s: Session, seeds: list[int], hops: int = 1, limit: int = 300,
                 filters: GraphFilters = NO_FILTERS) -> dict:
    frontier, seen = list(seeds), list(dict.fromkeys(seeds))
    edge_where = _edge_where(filters)
    for _ in range(hops):
        if not frontier or len(seen) >= limit:
            break
        rows = s.execute(text(f"""
            WITH f AS (SELECT unnest(CAST(:frontier AS bigint[])) AS id),
            deg AS (SELECT f.id, (SELECT count(*) FROM edges e WHERE e.src = f.id OR e.dst = f.id) AS d FROM f)
            SELECT CASE WHEN e.src = deg.id THEN e.dst ELSE e.src END AS nbr
            FROM deg JOIN edges e ON (e.src = deg.id OR e.dst = deg.id)
            LEFT JOIN source_items si ON si.id = e.source_item_id
            WHERE (deg.d <= :fanout OR deg.id = ANY(:seeds)) AND {edge_where}
            LIMIT :room
        """), {"frontier": frontier, "fanout": MAX_FANOUT, "seeds": seeds,
               "room": (limit - len(seen)) * 4, **_filter_params(filters)}).scalars().all()
        known = set(seen)
        new = [n for n in dict.fromkeys(rows) if n not in known][: limit - len(seen)]
        seen += new
        frontier = new
    return subgraph(s, seen, filters)


def people(s: Session, seed: int, limit: int = 60) -> dict:
    """Person-to-person view: the seed's top correspondents by messages exchanged, plus the ties between
    them. Edges are synthetic (type CORRESPONDS, weight = shared messages) and computed through Message nodes."""
    # Resolve through entity resolution: every address of the seed counts as the seed, and correspondents
    # are folded to their canonical identity (canon(x) = the cluster head, or x itself if unmerged).
    canon = "COALESCE((SELECT canonical_id FROM canonical WHERE node_id = {x}), {x})"
    head = s.execute(text(f"SELECT {canon.format(x=':seed')}"), {"seed": seed}).scalar_one()
    rows = s.execute(text(f"""
        WITH me AS (SELECT :head AS id UNION SELECT node_id FROM canonical WHERE canonical_id = :head),
        msgs AS (SELECT dst AS m FROM edges WHERE src IN (SELECT id FROM me) AND type = 'SENT'
                 UNION SELECT src FROM edges WHERE dst IN (SELECT id FROM me) AND type = 'RECEIVED'),
        parts AS (SELECT {canon.format(x='e.src')} AS p, e.dst AS m FROM edges e JOIN msgs ON msgs.m = e.dst
                  WHERE e.type = 'SENT'
                  UNION ALL
                  SELECT {canon.format(x='e.dst')}, e.src FROM edges e JOIN msgs ON msgs.m = e.src
                  WHERE e.type = 'RECEIVED')
        SELECT p, count(DISTINCT m) AS w FROM parts WHERE p <> :head GROUP BY p ORDER BY w DESC LIMIT :limit
    """), {"head": head, "limit": limit}).all()
    ids = [head] + [r.p for r in rows]
    ties = s.execute(text(f"""
        WITH parts AS (
            SELECT {canon.format(x='src')} AS p, dst AS m FROM edges WHERE type = 'SENT'
            UNION SELECT {canon.format(x='dst')}, src FROM edges WHERE type = 'RECEIVED'),
        mine AS (SELECT * FROM parts WHERE p = ANY(:ids))
        SELECT a.p AS src, b.p AS dst, count(*) AS w
        FROM mine a JOIN mine b ON a.m = b.m AND a.p < b.p GROUP BY 1, 2
        -- Always draw the seed's own ties (else one-message correspondents float unattached); between
        -- correspondents, require 2+ shared messages to keep the picture readable.
        HAVING count(*) >= 2 OR CAST(:head AS bigint) IN (a.p, b.p)
    """), {"ids": ids, "head": head}).all()
    g = subgraph(s, ids)
    g["edges"] = [{"id": -(i + 1), "src": t.src, "dst": t.dst, "type": "CORRESPONDS", "extractor": "derived",
                   "confidence": None, "weight": t.w} for i, t in enumerate(ties)]
    return g


def network(s: Session, seed: int, people_limit: int = 50, hub_limit: int = 30) -> dict:
    """People plus the shared things that join them: group chats, email threads, events. Each hub is a node
    and people link to the hubs they took part in, so friend groups and teams form visible clusters instead
    of a star around the seed. A hub must join 2+ of the seed's correspondents (a thread with only the seed
    and one other person adds nothing a direct tie doesn't). The seed itself is left off hubs: it is in all of
    them, and drawing those spokes would pull the layout back into a star."""
    base = people(s, seed, limit=people_limit)
    if not base["nodes"]:
        return base
    head = base["nodes"][0]["id"]
    others = [n["id"] for n in base["nodes"][1:]]
    canon = "COALESCE((SELECT canonical_id FROM canonical WHERE node_id = {x}), {x})"
    hubs = s.execute(text(f"""
        WITH part AS (  -- (person, hub) for every conversation thread or event they were part of
            SELECT DISTINCT {canon.format(x='pm.p')} AS person, t.dst AS hub
            FROM (SELECT src AS p, dst AS m FROM edges WHERE type = 'SENT'
                  UNION SELECT dst, src FROM edges WHERE type = 'RECEIVED') pm
            JOIN edges t ON t.src = pm.m AND t.type = 'IN_THREAD'
            UNION
            SELECT DISTINCT {canon.format(x='src')}, dst FROM edges WHERE type = 'ATTENDED'),
        mine AS (SELECT * FROM part WHERE person = ANY(:others))
        SELECT hub, array_agg(person) AS members, count(*) AS n
        FROM mine GROUP BY hub HAVING count(*) >= 2
        ORDER BY count(*) DESC LIMIT :hubs
    """), {"others": others, "hubs": hub_limit}).all()
    # How active each member is in each hub (messages), so a link's thickness means something.
    activity = {(r.hub, r.person): r.n for r in s.execute(text(f"""
        SELECT t.dst AS hub, {canon.format(x='pm.p')} AS person, count(DISTINCT pm.m) AS n
        FROM (SELECT src AS p, dst AS m FROM edges WHERE type = 'SENT'
              UNION SELECT dst, src FROM edges WHERE type = 'RECEIVED') pm
        JOIN edges t ON t.src = pm.m AND t.type = 'IN_THREAD'
        WHERE t.dst = ANY(:hubs) GROUP BY 1, 2
    """), {"hubs": [h.hub for h in hubs]})}
    hub_ids = [h.hub for h in hubs]
    g = subgraph(s, [head, *others, *hub_ids])
    # Keep every edge, including the seed's spokes: prune() decides what survives, and its spanning tree needs
    # the spokes to hold the graph together (dropping them up front left the seed floating in its own island).
    edges = list(base["edges"])
    edges += [{"id": -(100000 + i), "src": p, "dst": h.hub, "type": "PART_OF", "extractor": "derived",
               "confidence": None, "weight": activity.get((h.hub, p), 1)}
              for i, (h, p) in enumerate((h, p) for h in hubs for p in h.members)]
    g["edges"] = prune(g["nodes"], edges, head)
    # Hub nodes carry their size so the UI can say "group chat, 6 people", and readable names.
    sizes = {h.hub: len(h.members) for h in hubs}
    named = handle_names(s)
    for n in g["nodes"]:
        if n["id"] in sizes:
            n["members"] = sizes[n["id"]]
            n["name"] = pretty_thread(n["name"], named) or n["name"]
    return g


def explain(s: Session, a: int, b: int, limit: int = 8) -> dict:
    """Why two nodes are connected: the shared conversations (with message counts and the latest excerpt) and
    shared events, entity-resolved on both sides. This is the evidence behind a CORRESPONDS / PART_OF link."""
    canon = "COALESCE((SELECT canonical_id FROM canonical WHERE node_id = {x}), {x})"
    # Every identity in each side's cluster counts (Ken Lay's 12 addresses are one Ken Lay).
    ids = {}
    for side, n in (("a", a), ("b", b)):
        head = s.execute(text(f"SELECT {canon.format(x=':n')}"), {"n": n}).scalar_one()
        ids[side] = [head, *s.execute(text("SELECT node_id FROM canonical WHERE canonical_id = :h"),
                                      {"h": head}).scalars()]
    names = dict(s.execute(text("SELECT id, name FROM nodes WHERE id = ANY(:i)"), {"i": [a, b]}).all())
    convo = s.execute(text("""
        WITH pm AS (SELECT src AS p, dst AS m FROM edges WHERE type = 'SENT'
                    UNION SELECT dst, src FROM edges WHERE type = 'RECEIVED'),
        shared AS (SELECT DISTINCT x.m FROM pm x JOIN pm y ON x.m = y.m
                   WHERE x.p = ANY(:a) AND y.p = ANY(:b))
        SELECT t.id AS thread_id, t.name AS thread, count(*) AS messages,
               min(si.occurred_at) AS first, max(si.occurred_at) AS last,
               (array_agg(si.source ORDER BY si.occurred_at DESC))[1] AS source,
               (array_agg(left(si.body_text, 220) ORDER BY si.occurred_at DESC))[1] AS excerpt,
               (SELECT count(DISTINCT p) FROM pm JOIN edges it2 ON it2.src = pm.m AND it2.type = 'IN_THREAD'
                WHERE it2.dst = t.id) AS people
        FROM shared JOIN nodes msg ON msg.id = shared.m JOIN source_items si ON si.id = msg.source_item_id
        JOIN edges it ON it.src = shared.m AND it.type = 'IN_THREAD' JOIN nodes t ON t.id = it.dst
        GROUP BY t.id, t.name ORDER BY count(*) DESC LIMIT :lim
    """), {"a": ids["a"], "b": ids["b"], "lim": limit}).mappings().all()
    events = s.execute(text("""
        SELECT e.id, e.name, si.occurred_at AS at
        FROM edges x JOIN edges y ON x.dst = y.dst AND x.type = 'ATTENDED' AND y.type = 'ATTENDED'
        JOIN nodes e ON e.id = x.dst JOIN source_items si ON si.id = e.source_item_id
        WHERE x.src = ANY(:a) AND y.src = ANY(:b) GROUP BY e.id, e.name, si.occurred_at
        ORDER BY si.occurred_at DESC LIMIT :lim
    """), {"a": ids["a"], "b": ids["b"], "lim": limit}).mappings().all()
    total = sum(c["messages"] for c in convo)
    named = handle_names(s)
    out = []
    for c in convo:
        c = dict(c)
        c["thread"] = pretty_thread(c["thread"], named)
        c["excerpt"] = name_handles(c["excerpt"], named)
        out.append(c)
    return {"a": {"id": a, "name": names.get(a)}, "b": {"id": b, "name": names.get(b)},
            "messages": total, "conversations": out, "events": [dict(e) for e in events]}


def handle_names(s: Session) -> dict[str, str]:
    """Phone number / address -> contact name, for every Person a contact card has named."""
    rows = s.execute(text("""
        SELECT key, name FROM nodes WHERE type = 'Person' AND props->>'named_by' = 'contacts'
    """)).all()
    out = {}
    for key, name in rows:
        out[key] = name
        digits = re.sub(r"\D", "", key)
        if len(digits) >= 10:
            out[digits[-10:]] = name  # message text may carry the number without +1
    return out


PHONE = re.compile(r"\+?\d{10,15}")


def name_handles(t: str | None, named: dict[str, str]) -> str | None:
    """'[14:39] +16174075277: Yes' -> '[14:39] Sam Lee: Yes' where the number is a saved contact."""
    if not t:
        return t
    return PHONE.sub(lambda m: named.get(m.group(), named.get(re.sub(r"\D", "", m.group())[-10:], m.group())), t)


def pretty_thread(name: str | None, named: dict[str, str]) -> str | None:
    """An unnamed group chat is titled by its members' handles; show names, and cap long lists."""
    if not name or not PHONE.search(name):
        return name
    parts = [name_handles(p.strip(), named) for p in name.split(",")]
    shown = [p.split()[0] if p and not PHONE.fullmatch(p) else p for p in parts]
    extra = len(shown) - 3
    return ", ".join(shown[:3]) + (f" and {extra} other{'s' if extra != 1 else ''}" if extra > 0 else "")


def prune(nodes: list[dict], edges: list[dict], seed: int, per_node: int = 4) -> list[dict]:
    """Keep the connections that explain the structure: each node's strongest `per_node` links, plus a maximum
    spanning tree so no group gets cut off. Drops the long tail of one-off links that turn the graph into a
    hairball without saying anything. The seed doesn't get a top-N of its own (it touches everyone, so its
    top-N would just be a star); it keeps only the spokes the tree needs to hold groups together."""
    def w(e):
        return e.get("weight") or 1

    keep: set[int] = set()
    by_node: dict[int, list[dict]] = {}
    for e in edges:
        by_node.setdefault(e["src"], []).append(e)
        by_node.setdefault(e["dst"], []).append(e)
    for node, es in by_node.items():
        if node == seed:
            continue
        # A person's strongest ties to *others*. Spokes to the seed are left to the spanning tree below, which
        # keeps exactly the ones needed to attach each group (else it's a star again).
        keep.update(id(e) for e in sorted((e for e in es if seed not in (e["src"], e["dst"])), key=w,
                                          reverse=True)[:per_node])
    # Kruskal for a maximum spanning forest: guarantees connectivity with the strongest possible bridges.
    parent = {n["id"]: n["id"] for n in nodes}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # Peer edges first (strongest first), then spokes: so a group hangs off the seed by one spoke, not many.
    for e in sorted(edges, key=lambda e: (seed in (e["src"], e["dst"]), -w(e))):
        ra, rb = find(e["src"]), find(e["dst"])
        if ra != rb:
            parent[ra] = rb
            keep.add(id(e))
    return [e for e in edges if id(e) in keep]


def subgraph(s: Session, node_ids: list[int], filters: GraphFilters = NO_FILTERS) -> dict:
    nodes = s.execute(text("""
        SELECT n.id, n.type, n.name, n.key,
               (SELECT count(*) FROM edges e WHERE e.src = n.id OR e.dst = n.id) AS degree
        FROM nodes n WHERE n.id = ANY(:ids)
        ORDER BY array_position(CAST(:ids AS bigint[]), n.id)
    """), {"ids": node_ids}).mappings().all()
    edge_where = _edge_where(filters)
    edges = s.execute(text(f"""
        SELECT e.id, e.src, e.dst, e.type, e.extractor, e.confidence FROM edges e
        LEFT JOIN source_items si ON si.id = e.source_item_id
        WHERE e.src = ANY(:ids) AND e.dst = ANY(:ids) AND {edge_where}
    """), {"ids": node_ids, **_filter_params(filters)}).mappings().all()
    return {"nodes": [dict(n) for n in nodes], "edges": [dict(e) for e in edges]}
