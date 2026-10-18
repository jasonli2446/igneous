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


def subgraph(s: Session, node_ids: list[int]) -> dict:
    nodes = s.execute(text("""
        SELECT n.id, n.type, n.name, n.key,
               (SELECT count(*) FROM edges e WHERE e.src = n.id OR e.dst = n.id) AS degree
        FROM nodes n WHERE n.id = ANY(:ids)
    """), {"ids": node_ids}).mappings().all()
    edges = s.execute(text("""
        SELECT id, src, dst, type, extractor, confidence FROM edges
        WHERE src = ANY(:ids) AND dst = ANY(:ids)
    """), {"ids": node_ids}).mappings().all()
    return {"nodes": [dict(n) for n in nodes], "edges": [dict(e) for e in edges]}
