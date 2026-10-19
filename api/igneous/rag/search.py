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
