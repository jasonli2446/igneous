"""Graph-augmented question answering: hybrid search -> expand along the graph -> cite-or-abstain synthesis.

The baseline for the ablation (criterion 4) is the same pipeline with expand=False and mode="dense".
"""
import logging
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import llm
from igneous.config import settings
from igneous.graph.extract import allowed_sources
from igneous.rag.constraints import apply
from igneous.rag.rerank import rerank
from igneous.rag.search import Filters, _where, hybrid

CONTEXT_CHUNKS = 8
EXPAND_PER_HIT = 2
log = logging.getLogger(__name__)

SYSTEM = """You answer questions about a person's own email, files, and calendar, using only the evidence
given. Each evidence item has an id like [c123].

Rules:
- Every factual sentence in the answer must cite at least one evidence id that supports it, written
  inline as [c123].
- If the evidence does not contain the answer, set answerable to false and say briefly what is missing.
  Do not answer from general knowledge, even if you know it.
- Be concise: a few sentences unless the question asks for a list."""

SCHEMA = {
    "type": "object",
    "properties": {
        "answerable": {"type": "boolean"},
        "answer": {"type": "string"},
        "cited": {"type": "array", "items": {"type": "integer"}, "description": "chunk ids used"},
    },
    "required": ["answerable", "answer", "cited"],
    "additionalProperties": False,
}


def expand(s: Session, hits: list[dict], per_hit: int = EXPAND_PER_HIT, f: Filters | None = None) -> list[dict]:
    """Pull neighbors through the graph: other messages in the same thread as each hit. This is what finds
    the reply that answers the question when only the original mentioned the keywords."""
    seen = {h["chunk_id"] for h in hits}
    node_ids = [h["node_id"] for h in hits if h["node_id"]]
    if not node_ids:
        return []
    # In priority order: (0) the direct reply chain (REPLIES_TO, from quoted headers); (1) other messages in the
    # same thread; (2) messages between the same sender and a recipient within 14 days. Expanded evidence must
    # obey the same source/person/date scope as the original retrieval.
    where, params = _where(f or Filters())
    rows = s.execute(text(f"""
        WITH hit AS (SELECT unnest(CAST(:nodes AS bigint[])) AS node),
        reply AS (  -- direct reply chain, both directions: the most specific evidence there is
            SELECT hit.node AS via, CASE WHEN r.src = hit.node THEN r.dst ELSE r.src END AS msg, -1 AS pri
            FROM hit JOIN edges r ON r.type = 'REPLIES_TO' AND (r.src = hit.node OR r.dst = hit.node)),
        thread_sib AS (
            SELECT hit.node AS via, sib.src AS msg, 0 AS pri
            FROM hit JOIN edges t ON t.src = hit.node AND t.type = 'IN_THREAD'
            JOIN edges sib ON sib.dst = t.dst AND sib.type = 'IN_THREAD' AND sib.src <> hit.node),
        pair_sib AS (
            SELECT hit.node AS via, other.dst AS msg, 1 AS pri
            FROM hit
            JOIN edges snd ON snd.dst = hit.node AND snd.type = 'SENT'
            JOIN edges rcv ON rcv.src = hit.node AND rcv.type = 'RECEIVED'
            JOIN edges other ON other.src IN (snd.src, rcv.dst) AND other.type = 'SENT' AND other.dst <> hit.node
            JOIN edges otherrcv ON otherrcv.src = other.dst AND otherrcv.type = 'RECEIVED'
                 AND otherrcv.dst IN (snd.src, rcv.dst) AND otherrcv.dst <> other.src
            WHERE abs(extract(epoch FROM other.valid_from - snd.valid_from)) < 14 * 86400)
        SELECT DISTINCT ON (c.id) c.id, c.text, c.node_id, si.id AS source_item_id, si.source, si.external_id,
               si.title, si.occurred_at, n.via, n.pri
        FROM (SELECT * FROM reply UNION ALL SELECT * FROM thread_sib UNION ALL SELECT * FROM pair_sib) n
        JOIN chunks c ON c.node_id = n.msg AND c.ord = 0
        JOIN source_items si ON si.id = c.source_item_id
        WHERE TRUE {where}
        ORDER BY c.id, n.pri
    """), {"nodes": node_ids, **params}).mappings().all()
    rows = sorted(rows, key=lambda r: r["pri"])
    per_via: dict[int, int] = {}
    out = []
    for r in rows:
        if r["id"] in seen or per_via.get(r["via"], 0) >= per_hit:
            continue
        per_via[r["via"]] = per_via.get(r["via"], 0) + 1
        seen.add(r["id"])
        out.append({"chunk_id": r["id"], "text": r["text"], "node_id": r["node_id"],
                    "source_item_id": r["source_item_id"], "source": r["source"], "external_id": r["external_id"],
                    "title": r["title"], "occurred_at": r["occurred_at"], "matched": ["graph"], "score": 0.0})
    return out


def _evidence_block(items: list[dict]) -> str:
    parts = []
    for it in items:
        when = it["occurred_at"].date().isoformat() if it["occurred_at"] else "undated"
        parts.append(f"[c{it['chunk_id']}] ({it['source']}, {when}) {it['title'] or ''}\n{it['text'][:1500]}")
    return "\n\n---\n\n".join(parts)


def answer(s: Session, question: str, f: Filters | None = None, use_graph: bool = True,
           mode: str = "hybrid") -> dict:
    t0 = time.time()
    f = apply(s, question, f)
    # A question is explicit consent to ask a model, not to disclose every local source.
    # Keep retrieval and graph expansion inside the same source allow-list as extraction.
    approved = allowed_sources()
    f.sources = tuple(sorted(approved.intersection(f.sources) if f.sources else approved))
    hits = hybrid(s, question, f, k=CONTEXT_CHUNKS, mode=mode)
    # Local filesystem and Notes evidence is searchable in the UI, but cannot be supplied to any model
    # (reranker, embedder, or LLM) until the owner has explicitly opted in.
    if not settings.local_sources_model_enabled:
        hits = [hit for hit in hits if hit["source"] not in ("fs", "notes")]
    candidates = hits + expand(s, hits, f=f) if use_graph else hits
    t_rerank = time.time()
    context = rerank(question, candidates, keep=CONTEXT_CHUNKS) if use_graph else candidates
    rerank_s = time.time() - t_rerank
    t_retrieve = time.time() - t0
    log.info("Q&A retrieval completed in %.3fs (rerank %.3fs; %d candidates -> %d evidence)",
             t_retrieve, rerank_s, len(candidates), len(context))
    if not context:
        return {"answerable": False, "answer": "No matching evidence.", "citations": [], "evidence": [],
                "timing": {"retrieve_s": round(t_retrieve, 2)}}

    out, usage = llm.structured(SYSTEM, f"Evidence:\n\n{_evidence_block(context)}\n\nQuestion: {question}", SCHEMA)
    by_id = {c["chunk_id"]: c for c in context}
    # The model may only cite what it was shown; anything else is dropped rather than trusted.
    cited = [by_id[i] for i in dict.fromkeys(out["cited"]) if i in by_id]
    answerable = out["answerable"] and bool(cited)
    return {
        "answerable": answerable,
        "answer": out["answer"] if answerable or not out["answerable"] else
        "The model answered without citing the evidence, so the answer was withheld.",
        "citations": cited,
        "evidence": context,
        "timing": {"retrieve_s": round(t_retrieve, 2), "total_s": round(time.time() - t0, 2)},
        "usage": usage,
    }
