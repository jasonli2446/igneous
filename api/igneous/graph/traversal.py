"""Bounded k-hop neighborhoods. Shared by the explorer API and (later) graph-augmented retrieval."""
from sqlalchemy import text
from sqlalchemy.orm import Session

# Hub nodes (a big Organization, a mass-mail Thread) would swallow the node budget; skip expanding them.
MAX_FANOUT = 200


def neighborhood(s: Session, seeds: list[int], hops: int = 1, limit: int = 300,
                 edge_types: list[str] | None = None) -> dict:
    frontier, seen = list(seeds), list(dict.fromkeys(seeds))
    type_clause = "AND e.type = ANY(:types)" if edge_types else ""
    for _ in range(hops):
        if not frontier or len(seen) >= limit:
            break
        rows = s.execute(text(f"""
            WITH f AS (SELECT unnest(CAST(:frontier AS bigint[])) AS id),
            deg AS (SELECT f.id, (SELECT count(*) FROM edges e WHERE e.src = f.id OR e.dst = f.id) AS d FROM f)
            SELECT CASE WHEN e.src = deg.id THEN e.dst ELSE e.src END AS nbr
            FROM deg JOIN edges e ON (e.src = deg.id OR e.dst = deg.id) {type_clause}
            WHERE deg.d <= :fanout OR deg.id = ANY(:seeds)
            LIMIT :room
        """), {"frontier": frontier, "types": edge_types, "fanout": MAX_FANOUT, "seeds": seeds,
               "room": (limit - len(seen)) * 4}).scalars().all()
        new = [n for n in dict.fromkeys(rows) if n not in set(seen)][: limit - len(seen)]
        seen += new
        frontier = new
    return subgraph(s, seen)


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
    hub_ids = [h.hub for h in hubs]
    g = subgraph(s, [head, *others, *hub_ids])
    # Direct ties: keep person-person ties between correspondents (thinner); drop the seed's spokes except
    # for people in no hub, who would otherwise float.
    in_hub = {p for h in hubs for p in h.members}
    edges = [e for e in base["edges"] if head not in (e["src"], e["dst"])
             or ({e["src"], e["dst"]} - {head}).isdisjoint(in_hub)]
    edges += [{"id": -(100000 + i), "src": p, "dst": h.hub, "type": "PART_OF", "extractor": "derived",
               "confidence": None, "weight": 1}
              for i, (h, p) in enumerate((h, p) for h in hubs for p in h.members)]
    g["edges"] = edges
    return g


def subgraph(s: Session, node_ids: list[int]) -> dict:
    nodes = s.execute(text("""
        SELECT n.id, n.type, n.name, n.key,
               (SELECT count(*) FROM edges e WHERE e.src = n.id OR e.dst = n.id) AS degree
        FROM nodes n WHERE n.id = ANY(:ids)
        ORDER BY array_position(CAST(:ids AS bigint[]), n.id)
    """), {"ids": node_ids}).mappings().all()
    edges = s.execute(text("""
        SELECT id, src, dst, type, extractor, confidence FROM edges
        WHERE src = ANY(:ids) AND dst = ANY(:ids)
    """), {"ids": node_ids}).mappings().all()
    return {"nodes": [dict(n) for n in nodes], "edges": [dict(e) for e in edges]}
