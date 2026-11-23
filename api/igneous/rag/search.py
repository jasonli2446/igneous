"""Hybrid search: lexical (tsvector) + dense (pgvector), merged with reciprocal rank fusion."""
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings
from igneous.rag.embed import embed_query

RRF_K = 60
POOL = 50
# A record names a person when its text has one of their names as a phrase: two or more words, not an address.
HUMAN_NAME = r"^[^@0-9]+\s+[^@0-9]+$"


@dataclass
class Filters:
    """What a search may return. An empty or None field restricts nothing."""
    sources: tuple[str, ...] = ()  # stored source values (igneous/source_catalog.py)
    person_id: int | None = None  # an identity head: messages any of its merged identities sent or received
    mentions_id: int | None = None  # an identity head: passages naming any of its identities, or linked by MENTIONS
    since: datetime | None = None
    until: datetime | None = None  # exclusive
    from_question: frozenset[str] = field(default=frozenset())  # the fields the question set (constraints.apply)


def _members(param: str) -> str:
    """SQL for the identities of the identity head bound to `param`: the head and every node merged into it."""
    return f"(SELECT CAST(:{param} AS bigint) UNION SELECT node_id FROM canonical WHERE canonical_id = :{param})"


def _where(f: Filters) -> tuple[str, dict]:
    """SQL conditions on `c` (chunks) and `si` (source_items) for `f`, each starting with AND."""
    clauses, params = [], {}
    if f.sources:
        clauses.append("si.source = ANY(:sources)")
        params["sources"] = list(f.sources)
    if f.since:
        clauses.append("si.occurred_at >= :since")
        params["since"] = f.since
    if f.until:
        clauses.append("si.occurred_at < :until")
        params["until"] = f.until
    if f.person_id:
        clauses.append(f"""c.node_id IN (
            SELECT e.dst FROM edges e WHERE e.type = 'SENT' AND e.src IN {_members("person_id")}
            UNION SELECT e.src FROM edges e WHERE e.type = 'RECEIVED' AND e.dst IN {_members("person_id")})""")
        params["person_id"] = f.person_id
    if f.mentions_id:
        clauses.append(f"""(c.node_id IN (SELECT e.src FROM edges e WHERE e.type = 'MENTIONS'
                                          AND e.dst IN {_members("mentions_id")})
            OR EXISTS (SELECT 1 FROM nodes p WHERE p.id IN {_members("mentions_id")} AND p.name ~ :human_name
                       AND c.tsv @@ phraseto_tsquery('english', p.name)))""")
        params["mentions_id"] = f.mentions_id
        params["human_name"] = HUMAN_NAME
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


# Web-search syntax: a word, or a "quoted phrase", optionally negated with a leading minus.
_TOKEN = re.compile(r'-?"[^"]*"|\S+')


def _negations(q: str) -> tuple[str, str, dict]:
    """The query without its negated words and phrases ("-prices", '-"gas tax"'), and an SQL condition on `c`
    that excludes every passage containing one of them (empty when nothing is negated)."""
    kept, negated = [], []
    for token in _TOKEN.findall(q):
        if token.startswith("-") and len(token) > 1:
            negated.append(token[1:].strip('"').strip())
        else:
            kept.append(token)
    negated = [n for n in negated if n]
    if not negated:
        return " ".join(kept), "", {}
    return (" ".join(kept), " AND NOT c.tsv @@ websearch_to_tsquery('english', :negated)",
            {"negated": " or ".join(f'"{n}"' for n in negated)})


def lexical(s: Session, q: str, f: Filters, n: int = POOL) -> list[int]:
    """Full-text search. websearch_to_tsquery ANDs every term, which a question in plain words rarely satisfies, so
    the terms are ORed and ts_rank_cd ranks passages that match more of them higher; quoted phrases keep their word
    order. A negated word or phrase ("-prices") excludes every passage that contains it."""
    positive, negation, negation_params = _negations(q)
    if not positive.strip():
        return []
    where, params = _where(f)
    return list(s.execute(text(f"""
        WITH q AS (SELECT CAST(replace(CAST(websearch_to_tsquery('english', :q) AS text), ' & ', ' | ')
                               AS tsquery) AS tq)
        SELECT c.id FROM chunks c JOIN source_items si ON si.id = c.source_item_id, q
        WHERE c.tsv @@ q.tq {negation} {where}
        ORDER BY ts_rank_cd(c.tsv, q.tq, 32) DESC LIMIT :n
    """), {"q": positive, "n": n, **negation_params, **params}).scalars())


def dense(s: Session, q: str, f: Filters, n: int = POOL) -> list[int]:
    """Nearest passages to the query's embedding, among vectors from the same model (IGNEOUS_EMBEDDING_MODEL; others
    wait for the embed stage); a negated word or phrase excludes the passages that contain it."""
    positive, negation, negation_params = _negations(q)
    if not positive.strip():
        return []
    where, params = _where(f)
    s.execute(text("SET LOCAL hnsw.ef_search = 100"))
    return list(s.execute(text(f"""
        SELECT c.id FROM chunks c JOIN source_items si ON si.id = c.source_item_id
        WHERE c.embedding_model = :embedding_model {negation} {where}
        ORDER BY c.embedding <=> CAST(:v AS vector) LIMIT :n
    """), {"v": str(embed_query(positive)), "embedding_model": settings.embedding_model, "n": n,
           **negation_params, **params}).scalars())


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


def experts(s: Session, q: str, k: int = 10, evidence_limit: int = 3, mode: str = "hybrid",
            f: Filters | None = None) -> list[dict]:
    """Rank canonical people attached to the most relevant items, with a small evidence card per item.

    Retrieval, under the same filters as search, determines which items are about the query. Graph relationships
    only establish a person's involvement in those items; they do not infer expertise from adjacent people or
    organizations.
    """
    hits = hybrid(s, q, f, k=POOL, mode=mode)
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
