"""Dated activity of one node, or the dated links between two, as metadata only (`GET /timeline`, MCP `timeline`).

A person is all of their identities, and a counterpart is shown the way the explorer shows nodes: as its identity head,
never hidden and never a service. The answer carries no record text: no body, title, external id, metadata or quoted
span. A counterpart named by a record's title (a message, an event, a file, a conversation named after its subject) has
its name withheld (None) unless that record's source is one a hosted model may read (IGNEOUS_LLM_SOURCES): the MCP
server hands this answer to the model its client runs, and the web app opens the records themselves in the reader.
"""
from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph import identity
from igneous.graph.visibility import shown


def _entity(s: Session, node_id: int) -> dict:
    """{id, type} of the node, a person as their identity head. Raises identity.NotFound."""
    n = identity.node(s, node_id)
    return {"id": n["id"], "type": n["type"]}


def _edge_payload(row: Mapping, named: bool) -> dict:
    """One dated link as graph metadata; its counterpart's name only when `named`."""
    # An edge dated at one end only is shown as a point in time.
    start = row["valid_from"] or row["occurred_at"] or row["valid_to"]
    end = row["valid_to"] or row["occurred_at"] or row["valid_from"]
    return {
        "id": row["id"],
        "type": row["type"],
        "direction": "outgoing" if row["a_is_src"] else "incoming",
        "counterpart": {"id": row["other_id"], "type": row["other_type"], "name": row["other_name"] if named else None},
        "interval": {"from": start, "to": end},
        "provenance": {
            "extractor": row["extractor"],
            "extractor_version": row["extractor_version"],
            "model": row["model"],
            "confidence": row["confidence"],
            "evidence": {
                "source_item_id": row["source_item_id"],
                "source": row["source"],
                "occurred_at": row["occurred_at"],
                "span": {"start": row["span_start"], "end": row["span_end"]}
                if row["span_start"] is not None else None,
            },
        },
    }


def timeline(s: Session, a: int, b: int | None = None, limit: int = 50) -> dict:
    """Dated links between `a` and `b`, or `a`'s dated activity, newest first and at most `limit`; `truncated` says
    whether there were more. Raises identity.NotFound."""
    from igneous.llm import allowed_sources, visible_nodes

    entity_a = _entity(s, a)
    entity_b = _entity(s, b) if b is not None else None
    rows = s.execute(text(f"""
        WITH ends AS (
            SELECT e.*, e.src = ANY(:a) AS a_is_src,
                   COALESCE(c.canonical_id, CASE WHEN e.src = ANY(:a) THEN e.dst ELSE e.src END) AS other
            FROM edges e
            LEFT JOIN canonical c ON c.node_id = CASE WHEN e.src = ANY(:a) THEN e.dst ELSE e.src END
            WHERE (e.src = ANY(:a) OR e.dst = ANY(:a))
              AND (CAST(:b AS bigint[]) IS NULL OR e.src = ANY(:b) OR e.dst = ANY(:b)))
        SELECT e.id, e.type, e.a_is_src, e.valid_from, e.valid_to, e.extractor, e.extractor_version, e.model,
               e.confidence, e.source_item_id, e.span_start, e.span_end, si.source, si.occurred_at,
               other.id AS other_id, other.type AS other_type, other.name AS other_name, osi.source AS other_source
        FROM ends e
        LEFT JOIN source_items si ON si.id = e.source_item_id
        JOIN nodes other ON other.id = e.other AND {shown('other')}
        LEFT JOIN source_items osi ON osi.id = other.source_item_id
        WHERE e.other <> :head AND COALESCE(e.valid_from, e.valid_to, si.occurred_at) IS NOT NULL
        ORDER BY COALESCE(e.valid_from, e.valid_to, si.occurred_at) DESC, e.id DESC
        LIMIT :limit_plus_one
    """), {"a": identity.members(s, entity_a["id"]), "head": entity_a["id"],
           "b": identity.members(s, entity_b["id"]) if entity_b else None,
           "limit_plus_one": limit + 1}).mappings().all()
    allowed = allowed_sources()
    threads = visible_nodes(s, [r["other_id"] for r in rows if r["other_type"] == "Thread"], allowed)

    def named(r: Mapping) -> bool:
        if r["other_source"] is not None:
            return r["other_source"] in allowed
        return r["other_type"] != "Thread" or r["other_id"] in threads
    return {
        "mode": "relationship" if entity_b else "activity",
        "a": entity_a,
        "b": entity_b,
        "edges": [_edge_payload(row, named(row)) for row in rows[:limit]],
        "truncated": len(rows) > limit,
    }
