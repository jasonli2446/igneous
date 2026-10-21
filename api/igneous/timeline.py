"""Safe, bounded temporal relationship timelines for the public API."""
from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session


class EntityNotFoundError(Exception):
    """Raised when a requested timeline entity does not exist."""


def _entity(session: Session, node_id: int) -> dict:
    """Resolve an identity to its canonical node without exposing node props."""
    row = session.execute(text("""
        SELECT n.id, n.type
        FROM nodes n
        WHERE n.id = COALESCE(
            (SELECT canonical_id FROM canonical WHERE node_id = :node_id), :node_id
        )
    """), {"node_id": node_id}).mappings().first()
    if row is None:
        raise EntityNotFoundError(node_id)
    return dict(row)


def _edge_payload(row: Mapping) -> dict:
    """Serialize only graph metadata; source titles, bodies, and raw metadata stay private."""
    # Legacy partial intervals are rendered as a dated point instead of an unusable open range.
    start = row["valid_from"] or row["occurred_at"] or row["valid_to"]
    end = row["valid_to"] or row["occurred_at"] or row["valid_from"]
    payload = {
        "id": row["id"],
        "type": row["type"],
        "direction": "outgoing" if row["a_is_src"] else "incoming",
        "counterpart": {"id": row["other_id"], "type": row["other_type"]},
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
    return payload


def timeline(session: Session, a: int, b: int | None = None, limit: int = 50) -> dict:
    """Return dated direct edges for two entities, or dated activity for one entity.

    The returned rows use canonical identities for matching but retain the requested
    entity as the direction anchor.  The query deliberately selects no raw source
    content (titles, bodies, external IDs, or metadata).
    """
    entity_a = _entity(session, a)
    entity_b = _entity(session, b) if b is not None else None
    rows = session.execute(text("""
        WITH a_cluster AS (
            SELECT CAST(:a AS bigint) AS id
            UNION SELECT node_id FROM canonical WHERE canonical_id = :a
        ),
        b_cluster AS (
            SELECT CAST(:b AS bigint) AS id WHERE CAST(:b AS bigint) IS NOT NULL
            UNION SELECT node_id FROM canonical WHERE canonical_id = :b
        )
        SELECT e.id, e.src, e.dst, e.type, e.src IN (SELECT id FROM a_cluster) AS a_is_src,
               e.valid_from, e.valid_to,
               e.extractor, e.extractor_version, e.model, e.confidence,
               e.source_item_id, e.span_start, e.span_end,
               si.source, si.occurred_at,
               other.id AS other_id, other.type AS other_type
        FROM edges e
        LEFT JOIN source_items si ON si.id = e.source_item_id
        JOIN nodes other ON other.id = COALESCE(
            (SELECT canonical_id FROM canonical WHERE node_id = CASE
                WHEN e.src IN (SELECT id FROM a_cluster) THEN e.dst ELSE e.src END),
            CASE WHEN e.src IN (SELECT id FROM a_cluster) THEN e.dst ELSE e.src END
        )
        WHERE (e.src IN (SELECT id FROM a_cluster) OR e.dst IN (SELECT id FROM a_cluster))
          AND (:b IS NULL OR (e.src IN (SELECT id FROM b_cluster) OR e.dst IN (SELECT id FROM b_cluster)))
          AND COALESCE(e.valid_from, e.valid_to, si.occurred_at) IS NOT NULL
        ORDER BY COALESCE(e.valid_from, e.valid_to, si.occurred_at) DESC, e.id DESC
        LIMIT :limit_plus_one
    """), {"a": entity_a["id"], "b": entity_b["id"] if entity_b else None,
            "limit_plus_one": limit + 1}).mappings().all()
    truncated = len(rows) > limit
    return {
        "mode": "relationship" if entity_b else "activity",
        "a": entity_a,
        "b": entity_b,
        "edges": [_edge_payload(row) for row in rows[:limit]],
        "truncated": truncated,
    }
