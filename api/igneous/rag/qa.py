"""Graph-augmented question answering: hybrid search -> expand along the graph -> cite-or-abstain synthesis.

The baseline for the ablation (criterion 4) is the same pipeline with expand=False and mode="dense".
"""
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import llm
from igneous.rag.search import Filters, hybrid

CONTEXT_CHUNKS = 12
EXPAND_PER_HIT = 2

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


def expand(s: Session, hits: list[dict], per_hit: int = EXPAND_PER_HIT) -> list[dict]:
    """Pull neighbors through the graph: other messages in the same thread as each hit. This is what finds
    the reply that answers the question when only the original mentioned the keywords."""
    seen = {h["chunk_id"] for h in hits}
    node_ids = [h["node_id"] for h in hits if h["node_id"]]
    if not node_ids:
        return []
    # Two hops, in priority order: (1) other messages in the same thread; (2) messages between the same sender
    # and a recipient within 14 days. (2) matters because Enron threading is sparse (no reply headers).
    rows = s.execute(text("""
        WITH hit AS (SELECT unnest(CAST(:nodes AS bigint[])) AS node),
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
        FROM (SELECT * FROM thread_sib UNION ALL SELECT * FROM pair_sib) n
        JOIN chunks c ON c.node_id = n.msg AND c.ord = 0
        JOIN source_items si ON si.id = c.source_item_id
        ORDER BY c.id, n.pri
    """), {"nodes": node_ids}).mappings().all()
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


def rerank(s: Session, question: str, items: list[dict], keep: int) -> list[dict]:
    """Order graph-expanded chunks by cosine similarity to the question; expansion is recall-oriented, so
    most of what it pulls in is off-topic. (Cheap stand-in for the cross-encoder in #rerank.)"""
    if not items:
        return []
    from igneous.rag.embed import embed_query

    sims = dict(s.execute(text("""
        SELECT id, 1 - (embedding <=> CAST(:v AS vector)) FROM chunks
        WHERE id = ANY(:ids) AND embedding IS NOT NULL
    """), {"v": str(embed_query(question)), "ids": [i["chunk_id"] for i in items]}).all())
    for i in items:
        i["score"] = round(float(sims.get(i["chunk_id"], 0.0)), 4)
    return sorted(items, key=lambda i: i["score"], reverse=True)[:keep]


def _evidence_block(items: list[dict]) -> str:
    parts = []
    for it in items:
        when = it["occurred_at"].date().isoformat() if it["occurred_at"] else "undated"
        parts.append(f"[c{it['chunk_id']}] ({it['source']}, {when}) {it['title'] or ''}\n{it['text'][:1500]}")
    return "\n\n---\n\n".join(parts)


def answer(s: Session, question: str, f: Filters | None = None, use_graph: bool = True,
           mode: str = "hybrid") -> dict:
    t0 = time.time()
    hits = hybrid(s, question, f, k=8, mode=mode)
    extra = rerank(s, question, expand(s, hits), keep=CONTEXT_CHUNKS - len(hits)) if use_graph else []
    context = hits + extra
    t_retrieve = time.time() - t0
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
