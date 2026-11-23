"""Graph-augmented question answering: retrieve, widen along the graph, choose the evidence, then answer with
citations or say the evidence can't answer.

1. Hybrid search retrieves a pool of passages deep enough for the reranker (IGNEOUS_RERANKER_POOL_SIZE), inside
   the sources opted in for hosted models and any filter the question states.
2. Graph expansion adds each hit's nearest neighbours: its direct reply chain, other messages in its thread, and
   messages between the same people within two weeks. This finds the reply that answers a question when only
   the original message used its words.
3. The cross-encoder scores hits and neighbours together and keeps the best CONTEXT_CHUNKS. When it is off or
   can't run, the evidence keeps retrieval order but holds GRAPH_SLOTS places for neighbours.
4. The model answers from that evidence alone and cites it, or says what is missing.

The eval's baseline is `use_graph=False` with `mode="dense"`: dense retrieval only, no expansion, no reranking.
"""
import logging
import time
from collections import defaultdict
from dataclasses import replace

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import llm, source_catalog
from igneous.config import settings
from igneous.rag.constraints import apply, describe
from igneous.rag.rerank import rerank
from igneous.rag.search import Filters, _where, hybrid

CONTEXT_CHUNKS = 8  # passages the model sees
EXPAND_PER_HIT = 2  # graph neighbours kept per hit, the closest first
GRAPH_SLOTS = 3  # of the CONTEXT_CHUNKS, places held for graph neighbours when nothing reranks the evidence
RELATION = {-1: "reply", 0: "thread", 1: "correspondents"}  # how a neighbour is linked to its hit, closest first
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
    """Each hit's closest graph neighbours, at most `per_hit` of them: first its direct reply chain (REPLIES_TO,
    both directions), then other messages in its thread, then messages between its sender and a recipient within
    14 days; within each, nearest in time to the hit first. Neighbours obey the same filters as retrieval and
    inherit the score of the hit that reached them (`via_chunk_id`). A neighbour may itself be a hit that ranks
    below that hit (a reply whose subject repeats the question's words); a hit that ranks above it is not one."""
    rank = {h["chunk_id"]: i for i, h in enumerate(hits)}
    reached_by: dict[int, dict] = {}  # message node -> the best-ranked hit on it
    for h in hits:
        if h["node_id"] is not None:
            reached_by.setdefault(h["node_id"], h)
    if not reached_by:
        return []
    where, params = _where(f or Filters())
    rows = s.execute(text(f"""
        WITH hit AS (
            SELECT h.node, hsi.occurred_at AS at
            FROM unnest(CAST(:nodes AS bigint[])) AS h(node)
            LEFT JOIN nodes hn ON hn.id = h.node LEFT JOIN source_items hsi ON hsi.id = hn.source_item_id),
        reply AS (
            SELECT hit.node AS via, hit.at, CASE WHEN r.src = hit.node THEN r.dst ELSE r.src END AS msg, -1 AS pri
            FROM hit JOIN edges r ON r.type = 'REPLIES_TO' AND (r.src = hit.node OR r.dst = hit.node)),
        thread_sib AS (
            SELECT hit.node AS via, hit.at, sib.src AS msg, 0 AS pri
            FROM hit JOIN edges t ON t.src = hit.node AND t.type = 'IN_THREAD'
            JOIN edges sib ON sib.dst = t.dst AND sib.type = 'IN_THREAD' AND sib.src <> hit.node),
        pair_sib AS (
            SELECT hit.node AS via, hit.at, other.dst AS msg, 1 AS pri
            FROM hit
            JOIN edges snd ON snd.dst = hit.node AND snd.type = 'SENT'
            JOIN edges rcv ON rcv.src = hit.node AND rcv.type = 'RECEIVED'
            JOIN edges other ON other.src IN (snd.src, rcv.dst) AND other.type = 'SENT' AND other.dst <> hit.node
            JOIN edges otherrcv ON otherrcv.src = other.dst AND otherrcv.type = 'RECEIVED'
                 AND otherrcv.dst IN (snd.src, rcv.dst) AND otherrcv.dst <> other.src
            WHERE abs(extract(epoch FROM other.valid_from - snd.valid_from)) < 14 * 86400)
        SELECT c.id, c.text, c.node_id, si.id AS source_item_id, si.source, si.external_id, si.title,
               si.occurred_at, n.via, n.pri, abs(extract(epoch FROM si.occurred_at - n.at)) AS distance
        FROM (SELECT * FROM reply UNION ALL SELECT * FROM thread_sib UNION ALL SELECT * FROM pair_sib) n
        JOIN chunks c ON c.node_id = n.msg AND c.ord = 0
        JOIN source_items si ON si.id = c.source_item_id
        WHERE TRUE {where}
    """), {"nodes": list(reached_by), **params}).mappings().all()
    # Rank every (passage, hit) pair first, closest relation then nearest in time, and only then cap each hit's
    # share; a passage reached from several hits goes to the first that may take it.
    rows = sorted(rows, key=lambda r: (r["pri"], r["distance"] is None, float(r["distance"] or 0), r["id"],
                                       rank[reached_by[r["via"]]["chunk_id"]]))
    taken: dict[int, int] = defaultdict(int)
    out, placed = [], set()
    for r in rows:
        hit = reached_by[r["via"]]
        if r["id"] in placed or rank.get(r["id"], len(hits)) <= rank[hit["chunk_id"]] or taken[r["via"]] >= per_hit:
            continue
        taken[r["via"]] += 1
        placed.add(r["id"])
        also = hits[rank[r["id"]]]["matched"] if r["id"] in rank else []
        out.append({"chunk_id": r["id"], "text": r["text"], "node_id": r["node_id"],
                    "source_item_id": r["source_item_id"], "source": r["source"], "external_id": r["external_id"],
                    "title": r["title"], "occurred_at": r["occurred_at"], "matched": [*also, "graph"],
                    "score": hit["score"], "via_chunk_id": hit["chunk_id"], "relation": RELATION[r["pri"]]})
    return out


def _by_hit(neighbours: list[dict]) -> dict[int, list[dict]]:
    """Neighbours grouped under the hit that reached them, closest first."""
    out: dict[int, list[dict]] = defaultdict(list)
    for n in neighbours:
        out[n["via_chunk_id"]].append(n)
    return out


def _merge(hits: list[dict], neighbours: list[dict]) -> list[dict]:
    """Each hit followed by the neighbours it reached; every passage once, at its first place."""
    groups, out, seen = _by_hit(neighbours), [], set()
    for x in (x for h in hits for x in (h, *groups.get(h["chunk_id"], ()))):
        if x["chunk_id"] not in seen:
            seen.add(x["chunk_id"])
            out.append(x)
    return out


def _pool(hits: list[dict], neighbours: list[dict], size: int) -> list[dict]:
    """What the cross-encoder scores: each hit followed by its neighbours, best hit first, cut at `size`."""
    return _merge(hits, neighbours)[:max(size, 0)]


def _graph_slots(hits: list[dict], neighbours: list[dict], keep: int) -> list[dict]:
    """Retrieval order with GRAPH_SLOTS of the `keep` places held for graph neighbours of the hits shown: each
    hit's closest neighbour in rank order, then each one's second. A place no neighbour takes goes to the next hit,
    and every neighbour follows the hit that reached it."""
    slots = min(GRAPH_SLOTS, keep, len(neighbours))
    top = hits[:keep - slots]
    rank = {h["chunk_id"]: i for i, h in enumerate(top)}
    turn: dict[int, int] = defaultdict(int)
    order = []
    for n in neighbours:  # closest first within each hit
        if n["via_chunk_id"] in rank and n["chunk_id"] not in rank:
            order.append(((turn[n["via_chunk_id"]], rank[n["via_chunk_id"]]), n))
            turn[n["via_chunk_id"]] += 1
    picked = [n for _, n in sorted(order, key=lambda pair: pair[0])[:slots]]
    taken = {n["chunk_id"] for n in picked}
    rest = [h for h in hits[len(top):] if h["chunk_id"] not in taken][:slots - len(picked)]
    return _merge(top + rest, picked)


def choose(question: str, hits: list[dict], neighbours: list[dict], keep: int = CONTEXT_CHUNKS) -> list[dict]:
    """The evidence: the cross-encoder's best `keep` of the hits and their neighbours scored together, or, without
    a reranker, retrieval order with places held for neighbours."""
    ranked = rerank(question, _pool(hits, neighbours, settings.reranker_pool_size), keep)
    return ranked if ranked is not None else _graph_slots(hits, neighbours, keep)


def _evidence_block(items: list[dict]) -> str:
    parts = []
    for it in items:
        when = it["occurred_at"].date().isoformat() if it["occurred_at"] else "undated"
        link = f", {it['relation']} of c{it['via_chunk_id']}" if it.get("via_chunk_id") else ""
        parts.append(f"[c{it['chunk_id']}] ({it['source']}, {when}{link}) {it['title'] or ''}\n{it['text'][:1500]}")
    return "\n\n---\n\n".join(parts)


NO_SOURCES = ("Ask sends the passages it finds to a hosted model, and no source is opted in for that. Add a "
              "source id to IGNEOUS_LLM_SOURCES in api/.env (for example gmail), restart the API, and ask again. "
              "The Sources page shows each source's id.")


def _not_opted_in(message: str, filters: dict) -> dict:
    return {"answerable": False, "answer": message, "citations": [], "evidence": [], "timing": {"retrieve_s": 0.0},
            "reason": "no_opted_in_sources", "filters": filters}


def answer(s: Session, question: str, f: Filters | None = None, use_graph: bool = True,
           mode: str = "hybrid") -> dict:
    """A cited answer, or `answerable: false` with what is missing. `citations` (each an evidence passage with its
    chunk, source item and node ids) is empty unless the answer stands; `evidence` is everything the model saw;
    `filters` is what narrowed the search (constraints.describe), within the opted-in sources."""
    t0 = time.time()
    # A question is consent to ask a model about the sources opted in for it, not about everything: retrieval and
    # graph expansion stay inside that list, and with nothing on it no model is called at all.
    approved = llm.allowed_sources()
    if not approved:
        return _not_opted_in(NO_SOURCES, describe(s, f or Filters()))
    f = apply(s, question, f)
    if f.sources:  # the question or the caller limited where to look: only the opted-in part of that is read
        sources = approved.intersection(f.sources)
        if not sources:
            asked = sorted({source_catalog.BY_VALUE[v].connector if v in source_catalog.BY_VALUE else v
                            for v in f.sources})
            return _not_opted_in(f"This question is limited to {', '.join(asked)}, and none of that is opted in to "
                                 f"the hosted model (opted in: {', '.join(sorted(approved))}). To ask about it, add "
                                 "its id to IGNEOUS_LLM_SOURCES in api/.env and restart the API.", describe(s, f))
        f = replace(f, sources=tuple(sorted(sources)))
    filters = describe(s, f)
    f = f if f.sources else replace(f, sources=tuple(sorted(approved)))
    if use_graph:
        hits = hybrid(s, question, f, k=max(settings.reranker_pool_size, CONTEXT_CHUNKS), mode=mode)
        neighbours = expand(s, hits, f=f)
        t_choose = time.time()
        context = choose(question, hits, neighbours)
        log.info("Q&A evidence: %d hits and %d graph neighbours -> %d passages (choosing took %.3fs)",
                 len(hits), len(neighbours), len(context), time.time() - t_choose)
    else:
        context = hybrid(s, question, f, k=CONTEXT_CHUNKS, mode=mode)
    t_retrieve = time.time() - t0
    if not context:
        return {"answerable": False, "answer": "No matching evidence.", "citations": [], "evidence": [],
                "timing": {"retrieve_s": round(t_retrieve, 2)}, "filters": filters}

    out, usage = llm.structured(SYSTEM, f"Evidence:\n\n{_evidence_block(context)}\n\nQuestion: {question}", SCHEMA)
    by_id = {c["chunk_id"]: c for c in context}
    # The model may only cite what it was shown; anything else is dropped rather than trusted.
    cited = [by_id[i] for i in dict.fromkeys(out["cited"]) if i in by_id]
    answerable = out["answerable"] and bool(cited)
    return {
        "answerable": answerable,
        "answer": out["answer"] if answerable or not out["answerable"] else
        "The model answered without citing the evidence, so the answer was withheld.",
        "citations": cited if answerable else [],
        "evidence": context,
        "filters": filters,
        "timing": {"retrieve_s": round(t_retrieve, 2), "total_s": round(time.time() - t0, 2)},
        "usage": usage,
    }
