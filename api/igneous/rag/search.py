"""Hybrid search: lexical (tsvector) + dense (pgvector), merged with reciprocal rank fusion."""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.rag.embed import embed_query

RRF_K = 60
POOL = 50


@dataclass
class Filters:
    source: str | None = None
    person: str | None = None  # email address; matches sender or recipient
    since: datetime | None = None
    until: datetime | None = None


def _where(f: Filters) -> tuple[str, dict]:
    clauses, params = [], {}
    if f.source:
        clauses.append("si.source = :source")
        params["source"] = f.source
    if f.since:
        clauses.append("si.occurred_at >= :since")
        params["since"] = f.since
    if f.until:
        clauses.append("si.occurred_at < :until")
        params["until"] = f.until
    if f.person:
        clauses.append("""c.node_id IN (
            SELECT e.dst FROM edges e JOIN nodes p ON p.id = e.src WHERE p.key = :person AND e.type = 'SENT'
            UNION SELECT e.src FROM edges e JOIN nodes p ON p.id = e.dst WHERE p.key = :person AND e.type = 'RECEIVED')""")
        params["person"] = f.person.lower()
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def lexical(s: Session, q: str, f: Filters, n: int = POOL) -> list[int]:
    """websearch_to_tsquery ANDs every term, so a natural-language question ("who pitched Enron on ...")
    matches almost nothing (recall@10 was 0.14). OR the terms instead and let ts_rank_cd reward chunks that
    match more of them. Quoted phrases still work: the <-> operator survives the rewrite."""
    where, params = _where(f)
    return list(s.execute(text(f"""
        WITH q AS (SELECT CAST(replace(CAST(websearch_to_tsquery('english', :q) AS text), ' & ', ' | ')
                               AS tsquery) AS tq)
        SELECT c.id FROM chunks c JOIN source_items si ON si.id = c.source_item_id, q
        WHERE c.tsv @@ q.tq {where}
        ORDER BY ts_rank_cd(c.tsv, q.tq, 32) DESC LIMIT :n
    """), {"q": q, "n": n, **params}).scalars())


def dense(s: Session, q: str, f: Filters, n: int = POOL) -> list[int]:
    where, params = _where(f)
    s.execute(text("SET LOCAL hnsw.ef_search = 100"))
    return list(s.execute(text(f"""
        SELECT c.id FROM chunks c JOIN source_items si ON si.id = c.source_item_id
        WHERE c.embedding IS NOT NULL {where}
        ORDER BY c.embedding <=> CAST(:v AS vector) LIMIT :n
    """), {"v": str(embed_query(q)), "n": n, **params}).scalars())


def rrf(*rankings: list[int]) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def hybrid(s: Session, q: str, f: Filters | None = None, k: int = 10, mode: str = "hybrid") -> list[dict]:
    f = f or Filters()
    lex = lexical(s, q, f) if mode in ("hybrid", "lexical") else []
    den = dense(s, q, f) if mode in ("hybrid", "dense") else []
    fused = rrf(lex, den)[:k]
    if not fused:
        return []
    ids = [cid for cid, _ in fused]
    rows = {r.id: r for r in s.execute(text("""
        SELECT c.id, c.text, c.node_id, si.id AS source_item_id, si.source, si.external_id, si.title,
               si.occurred_at
        FROM chunks c JOIN source_items si ON si.id = c.source_item_id WHERE c.id = ANY(:ids)
    """), {"ids": ids})}
    return [{
        "chunk_id": cid, "score": round(score, 5), "text": rows[cid].text, "node_id": rows[cid].node_id,
        "source_item_id": rows[cid].source_item_id, "source": rows[cid].source,
        "external_id": rows[cid].external_id, "title": rows[cid].title,
        "occurred_at": rows[cid].occurred_at,
        "matched": [m for m, lst in (("lexical", lex), ("dense", den)) if cid in lst],
    } for cid, score in fused]


def experts(s: Session, q: str, k: int = 10, evidence_limit: int = 3, mode: str = "hybrid") -> list[dict]:
    """Rank canonical people attached to the most relevant items, with a small evidence card per item.

    Retrieval determines which items are about the query. Graph relationships only establish a person's
    involvement in those items; they do not infer expertise from adjacent people or organizations.
    """
    hits = hybrid(s, q, k=POOL, mode=mode)
    candidates: dict[int, dict] = {}
    for hit in hits:
        item_id = hit["source_item_id"]
        previous = candidates.get(item_id)
        if previous is None or hit["score"] > previous["score"]:
            candidates[item_id] = hit
    if not candidates:
        return []

    item_ids = list(candidates)
    relevance = [candidates[item_id]["score"] for item_id in item_ids]
    rows = s.execute(text("""
        WITH candidate_items AS (
            SELECT item_id, relevance
            FROM unnest(CAST(:item_ids AS bigint[]), CAST(:relevance AS double precision[]))
                 AS candidate(item_id, relevance)
        ), involvement AS (
            SELECT COALESCE(c.canonical_id, e.src) AS person_id, m.source_item_id, 'authored'::text AS kind
            FROM edges e
            JOIN nodes m ON m.id = e.dst AND m.type = 'Message'
            LEFT JOIN canonical c ON c.node_id = e.src
            WHERE e.type = 'SENT'
            UNION
            SELECT COALESCE(c.canonical_id, e.src), f.source_item_id, 'authored'::text
            FROM edges e
            JOIN nodes f ON f.id = e.dst AND f.type = 'File'
            LEFT JOIN canonical c ON c.node_id = e.src
            WHERE e.type = 'AUTHORED'
            UNION
            SELECT COALESCE(c.canonical_id, e.dst), m.source_item_id, 'mentioned'::text
            FROM edges e
            JOIN nodes m ON m.id = e.src AND m.type = 'Message'
            LEFT JOIN canonical c ON c.node_id = e.dst
            WHERE e.type = 'MENTIONS'
            UNION
            SELECT COALESCE(c.canonical_id, e.src), event.source_item_id, 'attended'::text
            FROM edges e
            JOIN nodes event ON event.id = e.dst AND event.type = 'Event'
            LEFT JOIN canonical c ON c.node_id = e.src
            WHERE e.type = 'ATTENDED'
        ), evidence AS (
            SELECT i.person_id, i.source_item_id, array_agg(DISTINCT i.kind ORDER BY i.kind) AS involvement,
                   ci.relevance, si.source, si.external_id, si.title, si.occurred_at
            FROM involvement i
            JOIN candidate_items ci ON ci.item_id = i.source_item_id
            JOIN source_items si ON si.id = i.source_item_id
            GROUP BY i.person_id, i.source_item_id, ci.relevance, si.source, si.external_id, si.title, si.occurred_at
        ), ranked_people AS (
            SELECT person_id, sum(relevance) AS score, count(*) AS evidence_count
            FROM evidence
            GROUP BY person_id
            ORDER BY score DESC, evidence_count DESC, person_id
            LIMIT :k
        ), ranked_evidence AS (
            SELECT e.*, row_number() OVER (
                PARTITION BY e.person_id ORDER BY e.relevance DESC, e.occurred_at DESC NULLS LAST, e.source_item_id
            ) AS item_rank
            FROM evidence e JOIN ranked_people p ON p.person_id = e.person_id
        )
        SELECT p.person_id, n.name, p.score, p.evidence_count, e.source_item_id, e.involvement,
               e.source, e.external_id, e.title, e.occurred_at
        FROM ranked_people p
        JOIN nodes n ON n.id = p.person_id
        JOIN ranked_evidence e ON e.person_id = p.person_id AND e.item_rank <= :evidence_limit
        ORDER BY p.score DESC, p.evidence_count DESC, p.person_id, e.item_rank
    """), {"item_ids": item_ids, "relevance": relevance, "k": k, "evidence_limit": evidence_limit}).mappings().all()

    excerpts = {item_id: candidates[item_id]["text"][:400] for item_id in item_ids}
    out: dict[int, dict] = {}
    for row in rows:
        expert = out.setdefault(row["person_id"], {
            "id": row["person_id"], "name": row["name"], "score": round(float(row["score"]), 5),
            "evidence_count": row["evidence_count"], "evidence": [],
        })
        expert["evidence"].append({
            "source_item_id": row["source_item_id"], "source": row["source"],
            "external_id": row["external_id"], "title": row["title"], "occurred_at": row["occurred_at"],
            "involvement": list(row["involvement"]), "excerpt": excerpts[row["source_item_id"]],
        })
    return list(out.values())
