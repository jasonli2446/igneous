"""Derived, evidence-backed commitments, decisions and introductions.

This module never promotes an inference to a user fact.  Its rows are marked
``derived`` and retain the source item and verified text span that produced them.
"""
import hashlib
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings
from igneous.graph.extract import locate

EXTRACTOR = "relationship-history"
VERSION = "1"


def _key(kind: str, source_item_id: int, quote: str) -> str:
    digest = hashlib.sha256(quote.encode()).hexdigest()[:16]
    return f"{kind}:{source_item_id}:{digest}"


def store_claims(s: Session, item, body: str, out: dict, model: str) -> dict:
    """Store only claims whose model-supplied quote resolves against stored text."""
    stats = {"commitments": 0, "decisions": 0, "dropped_claims": 0}
    for kind, node_type, edge_type, values in (
        ("commitment", "Commitment", "HAS_COMMITMENT", out.get("commitments", [])),
        ("decision", "Decision", "HAS_DECISION", out.get("decisions", [])),
    ):
        for value in values:
            quote = value.get("quote", "")
            span = locate(body, quote)
            if span is None:
                stats["dropped_claims"] += 1
                continue
            key = _key(kind, item.id, quote)
            props = {"derived": True, "origin": "model", "quote": quote,
                     "what": value.get("what", "").strip()}
            if kind == "commitment":
                props.update({"who": value.get("who", "").strip(), "by_when": value.get("by_when") or None,
                              "status": "open"})
            nid = s.execute(text("""
                INSERT INTO nodes (type, key, name, props, source_item_id)
                VALUES (:type, :key, :name, CAST(:props AS jsonb), :sid)
                ON CONFLICT (type, key) DO UPDATE SET
                    props = nodes.props || (EXCLUDED.props - 'status')
                RETURNING id
            """), {"type": node_type, "key": key, "name": props["what"][:160] or kind.title(),
                  "props": json.dumps(props), "sid": item.id}).scalar_one()
            s.execute(text("""
                INSERT INTO edges (src, dst, type, props, valid_from, extractor, extractor_version, model,
                                   confidence, source_item_id, span_start, span_end)
                VALUES (:src, :dst, :type, CAST(:props AS jsonb), :at, :extractor, :version, :model,
                        .9, :sid, :start, :end)
                ON CONFLICT (src, dst, type, extractor) DO UPDATE SET props = EXCLUDED.props,
                    span_start = EXCLUDED.span_start, span_end = EXCLUDED.span_end
            """), {"src": item.node_id, "dst": nid, "type": edge_type,
                     "props": json.dumps({"quote": quote, "derived": True}), "at": item.occurred_at,
                     "extractor": EXTRACTOR, "version": VERSION, "model": model, "sid": item.id,
                     "start": span[0], "end": span[1]})
            stats[f"{kind}s"] += 1
    return stats


def refresh_commitment_status(s: Session) -> int:
    """Mark derived commitments resolved when later messages in their thread explicitly confirm them done, cancelled or
    superseded: one cached model check per commitment and set of later messages. Returns how many it resolved.

    A check reads only the commitment and later messages from opted-in sources (IGNEOUS_LLM_SOURCES), most similar
    first. A result the model gave earlier counts even with the model off; with none, or when the call fails, the
    commitment stays open and is checked again on the next run.
    """
    from igneous.llm import LLMDisabled, allowed_sources, cache_lookup, cache_store, structured

    sources = sorted(allowed_sources())
    if not sources:
        return 0
    rows = s.execute(text("""
        SELECT n.id, n.props, si.source, e.source_item_id
        FROM nodes n JOIN edges e ON e.dst = n.id AND e.type = 'HAS_COMMITMENT' AND e.extractor = :ex
        JOIN source_items si ON si.id = e.source_item_id
        WHERE n.type = 'Commitment' AND COALESCE(n.props->>'status', 'open') = 'open'
          AND si.source = ANY(CAST(:sources AS text[]))
        ORDER BY n.id
    """), {"ex": EXTRACTOR, "sources": sources}).mappings().all()
    model_on, changed = settings.llm_enabled, 0
    for row in rows:
        # Rank follow-ups by local embedding similarity first; a missing embedding
        # model or unembedded message safely falls back to chronological evidence.
        try:
            from igneous.rag.embed import embed_query
            vector = str(embed_query(row["props"].get("what", "")))
        except Exception:
            vector = None
        later = s.execute(text("""
            SELECT later_item.body_text FROM edges in_thread
            JOIN edges original_thread ON original_thread.dst = in_thread.dst AND original_thread.type = 'IN_THREAD'
            JOIN nodes original ON original.id = original_thread.src
            JOIN source_items origin_item ON origin_item.id = original.source_item_id
            JOIN nodes later ON later.id = in_thread.src
            JOIN source_items later_item ON later_item.id = later.source_item_id
            LEFT JOIN chunks c ON c.source_item_id = later_item.id AND c.embedding IS NOT NULL
                 AND c.embedding_model = :embedding_model
            WHERE original.source_item_id = :sid AND in_thread.type = 'IN_THREAD'
              AND later_item.occurred_at > origin_item.occurred_at
              AND later_item.source = ANY(CAST(:sources AS text[]))
            GROUP BY later.id, later_item.id
            ORDER BY CASE WHEN CAST(:vector AS text) IS NULL OR count(c.id) = 0 THEN 1 ELSE 0 END,
                     min(c.embedding <=> CAST(:vector AS vector)), later_item.occurred_at, later_item.id LIMIT 8
        """), {"sid": row["source_item_id"], "sources": sources, "vector": vector,
              "embedding_model": settings.embedding_model}).scalars().all()
        if not later:
            continue
        material = "\n---\n".join(later)
        key = "commitment-resolution:v1:" + hashlib.sha256((str(row["id"]) + material).encode()).hexdigest()
        verdict = cache_lookup(key)
        if verdict is None:
            if not model_on:
                continue
            try:
                verdict, _ = structured(
                    "Decide whether later messages explicitly confirm that a promise was completed, cancelled, or superseded. "
                    "Return resolved=true only with clear evidence; otherwise false.",
                    f"Promise: {row['props'].get('what')}\nLater messages:\n{material[:6000]}",
                    {"type": "object", "properties": {"resolved": {"type": "boolean"}},
                     "required": ["resolved"], "additionalProperties": False}, max_tokens=300)
            except LLMDisabled:
                model_on = False
                continue
            except Exception:  # this commitment stays open and is checked again next run
                continue
            cache_store(key, verdict)
        if verdict.get("resolved"):
            s.execute(text("UPDATE nodes SET props = props || '{\"status\": \"resolved\"}' WHERE id = :id"),
                      {"id": row["id"]})
            changed += 1
    return changed


MAX_INTRO_PARTICIPANTS = 4   # sender + recipients; larger threads are groups or mass mail, not introductions
INTRO_WINDOW_DAYS = 180

# Parameters: :ex, :version, :max_participants, :window_days, :blocked (rejected pairs, graph/derived.py).
# Participants are canonical identities.
INTRODUCTIONS_SQL = """
WITH raw AS (
  SELECT dst AS msg, src AS node, true AS is_sender, valid_from AS at, source_item_id AS sid
  FROM edges WHERE type = 'SENT' AND valid_from IS NOT NULL
  UNION ALL
  SELECT src, dst, false, valid_from, source_item_id
  FROM edges WHERE type = 'RECEIVED' AND valid_from IS NOT NULL
), parts AS MATERIALIZED (
  SELECT r.msg, COALESCE(c.canonical_id, r.node) AS person, bool_or(r.is_sender) AS is_sender,
         min(r.at) AS at, min(r.sid) FILTER (WHERE r.is_sender) AS sid
  FROM raw r LEFT JOIN canonical c ON c.node_id = r.node GROUP BY r.msg, COALESCE(c.canonical_id, r.node)
), sizes AS MATERIALIZED (
  SELECT msg, count(*) AS n, count(*) FILTER (WHERE is_sender) AS senders, min(at) AS at
  FROM parts GROUP BY msg
), eligible AS MATERIALIZED (
  SELECT id FROM nodes WHERE COALESCE(props->>'kind', '') <> 'service'
), cand AS MATERIALIZED (
  SELECT a.person AS a, b.person AS b, c.person AS c, a.msg, z.at, a.sid
  FROM sizes z JOIN parts a ON a.msg = z.msg AND a.is_sender
  JOIN parts b ON b.msg = z.msg AND NOT b.is_sender
  JOIN parts c ON c.msg = z.msg AND NOT c.is_sender AND c.person > b.person
  WHERE z.senders = 1 AND z.n BETWEEN 3 AND :max_participants
    AND b.person IN (SELECT id FROM eligible) AND c.person IN (SELECT id FROM eligible)
), pm AS MATERIALIZED (
  SELECT p.msg, p.person AS b, q.person AS c, z.at, z.n
  FROM parts p JOIN parts q ON q.msg = p.msg AND q.person > p.person
  JOIN sizes z ON z.msg = p.msg
  WHERE p.person IN (SELECT b FROM cand) AND q.person IN (SELECT c FROM cand)
), firsts AS (
  SELECT DISTINCT ON (x.b, x.c) x.* FROM cand x
  WHERE NOT (CAST(x.b AS text) || '>' || CAST(x.c AS text) = ANY(CAST(:blocked AS text[])))
    AND NOT EXISTS (SELECT 1 FROM pm WHERE pm.b = x.b AND pm.c = x.c AND pm.at < x.at)
    AND EXISTS (SELECT 1 FROM pm WHERE pm.b = x.b AND pm.c = x.c AND pm.n <= :max_participants
                AND pm.at > x.at AND pm.at <= x.at + make_interval(days => :window_days)
                AND NOT EXISTS (SELECT 1 FROM parts r WHERE r.msg = pm.msg AND r.person = x.a))
  ORDER BY x.b, x.c, x.at, x.msg
)
INSERT INTO edges (src, dst, type, props, valid_from, valid_to, extractor, extractor_version, confidence, source_item_id)
SELECT b, c, 'INTRODUCED', jsonb_build_object('introducer', a, 'derived', true),
       at, at, :ex, :version, .8, sid FROM firsts
ON CONFLICT (src, dst, type, extractor) DO NOTHING
"""


def derive_introductions(s: Session) -> int:
    """Create conservative B->C introductions from a sender's small email or thread.

    Only a message with at most ``MAX_INTRO_PARTICIPANTS`` canonical participants and one
    sender can introduce (so at most three pairs per message).  B and C must never have shared
    a message before, and must later share a small message *without* the introducer within
    ``INTRO_WINDOW_DAYS``.  Each pair gets one edge, from its earliest qualifying message.
    Services are never B or C.  The user stays eligible as B or C, and a pair you rejected never is
    (graph/derived.py).  Deterministic, source-cited, and separate from model claims.  Rebuilds its own edges, so it
    is idempotent.
    """
    from igneous.graph.derived import rejections

    s.execute(text("DELETE FROM edges WHERE extractor = :ex AND type = 'INTRODUCED'"), {"ex": EXTRACTOR})
    return s.execute(text(INTRODUCTIONS_SQL), {
        "ex": EXTRACTOR, "version": VERSION, "max_participants": MAX_INTRO_PARTICIPANTS,
        "window_days": INTRO_WINDOW_DAYS, "blocked": rejections(s).id_pairs()}).rowcount or 0


def commitments(s: Session, status: str = "open", limit: int = 50, node_id: int | None = None) -> list[dict]:
    """Cited model-derived claims, optionally scoped to a node's own or directly connected records.

    A person's scope includes sent/received messages; an entity's scope includes its member records.
    Scope is applied before the result limit, and merged identities share the same records.
    """
    if node_id is not None:
        from igneous.timeline import _entity
        node_id = _entity(s, node_id)["id"]
    rows = s.execute(text("""
        WITH scope AS (
            SELECT CAST(:node_id AS bigint) AS id WHERE CAST(:node_id AS bigint) IS NOT NULL
            UNION SELECT node_id FROM canonical WHERE canonical_id = :node_id
        ), items AS (
            SELECT source_item_id AS id FROM nodes WHERE id IN (SELECT id FROM scope)
            UNION SELECT m.source_item_id FROM edges link JOIN nodes m ON m.id = CASE
                WHEN link.src IN (SELECT id FROM scope) THEN link.dst ELSE link.src END
            WHERE (link.src IN (SELECT id FROM scope) OR link.dst IN (SELECT id FROM scope))
              AND link.type IN ('SENT', 'RECEIVED', 'ATTENDED', 'PART_OF', 'IN_THREAD', 'ATTACHED', 'CONTAINS')
              AND COALESCE(link.props->>'verdict', '') <> 'drop'
            UNION SELECT link.source_item_id FROM edges link
            WHERE (link.src IN (SELECT id FROM scope) OR link.dst IN (SELECT id FROM scope))
              AND COALESCE(link.props->>'verdict', '') <> 'drop'
            UNION SELECT ev.source_item_id FROM edges link JOIN edge_evidence ev ON ev.edge_id = link.id
            WHERE (link.src IN (SELECT id FROM scope) OR link.dst IN (SELECT id FROM scope))
              AND COALESCE(link.props->>'verdict', '') <> 'drop'
        )
        SELECT n.id, n.name, n.props, e.source_item_id, e.span_start, e.span_end, si.source, si.occurred_at
        FROM nodes n JOIN edges e ON e.dst = n.id AND e.type = 'HAS_COMMITMENT' AND e.extractor = :ex
        JOIN source_items si ON si.id = e.source_item_id
        WHERE n.type = 'Commitment' AND (:status = 'all' OR COALESCE(n.props->>'status', 'open') = :status)
          AND (CAST(:node_id AS bigint) IS NULL OR e.source_item_id IN (SELECT id FROM items))
        ORDER BY si.occurred_at DESC NULLS LAST, n.id DESC LIMIT :limit
    """), {"ex": EXTRACTOR, "status": status, "limit": limit, "node_id": node_id}).mappings()
    return [{"id": r["id"], "what": r["props"].get("what"), "who": r["props"].get("who"),
             "by_when": r["props"].get("by_when"), "status": r["props"].get("status", "open"),
             "derived": True, "origin": "model", "evidence": {"source_item_id": r["source_item_id"],
             "source": r["source"], "occurred_at": r["occurred_at"],
             "span": {"start": r["span_start"], "end": r["span_end"]}}} for r in rows]
