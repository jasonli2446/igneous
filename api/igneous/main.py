from datetime import datetime
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.db import get_session
from igneous.graph.traversal import GraphFilters, explain, neighborhood, network, people
from igneous.llm import LLMDisabled
from igneous.rag import qa
from igneous.rag.search import Filters, hybrid
from igneous.sync_status import sync_status

app = FastAPI(title="Igneous", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health(s: Session = Depends(get_session)):
    s.execute(text("SELECT 1"))
    return {"ok": True}


@app.get("/stats")
def stats(s: Session = Depends(get_session)):
    q = lambda sql: s.execute(text(sql)).all()  # noqa: E731
    return {
        "source_items": dict(q("SELECT source, count(*) FROM source_items GROUP BY 1")),
        "nodes": dict(q("SELECT type, count(*) FROM nodes GROUP BY 1")),
        "edges": dict(q("SELECT type, count(*) FROM edges GROUP BY 1")),
        "chunks": s.execute(text("SELECT count(*) AS total, count(embedding) AS embedded FROM chunks")).one()._asdict(),
    }


@app.get("/sync")
def sync(
    connector: Literal["email", "imessage", "gmail", "gcal", "gcontacts"] | None = None,
    account: str | None = None,
    s: Session = Depends(get_session),
):
    """Report persisted sync checkpoints; this endpoint never starts a sync."""
    return {"syncs": sync_status(s, connector=connector, account=account)}


@app.get("/search")
def search(q: str, source: str | None = None, person: str | None = None,
           since: datetime | None = Query(None, alias="from"), until: datetime | None = Query(None, alias="to"),
           k: int = Query(10, le=50), mode: str = Query("hybrid", pattern="^(hybrid|lexical|dense)$"),
           s: Session = Depends(get_session)):
    return {"hits": hybrid(s, q, Filters(source, person, since, until), k=k, mode=mode)}


class AskBody(BaseModel):
    question: str
    person: str | None = None
    use_graph: bool = True  # False = the ablation baseline (criterion 4)
    mode: str = "hybrid"


@app.post("/ask")
def ask(body: AskBody, s: Session = Depends(get_session)):
    try:
        return qa.answer(s, body.question, Filters(person=body.person), use_graph=body.use_graph, mode=body.mode)
    except LLMDisabled as e:
        raise HTTPException(503, str(e)) from e


@app.get("/graph/search")
def graph_search(q: str, type: str | None = None, limit: int = Query(20, le=100),
                 s: Session = Depends(get_session)):
    """Find seed nodes by name or key. Merged identities collapse to their canonical node (one Kenneth Lay,
    not ten addresses); `aliases` counts the folded-in identities."""
    rows = s.execute(text("""
        WITH hit AS (
            SELECT COALESCE(c.canonical_id, n.id) AS id FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id
            WHERE (n.name ILIKE :like OR n.key ILIKE :like) AND (CAST(:type AS text) IS NULL OR n.type = :type)
              AND n.type <> 'Message'
            GROUP BY 1)
        SELECT n.id, n.type, n.name, n.key,
               (SELECT count(*) FROM edges e WHERE e.src = ANY(m.ids) OR e.dst = ANY(m.ids)) AS degree,
               cardinality(m.ids) - 1 AS aliases
        FROM hit JOIN nodes n ON n.id = hit.id
        CROSS JOIN LATERAL (SELECT array_append(array_agg(node_id), hit.id) AS ids FROM canonical
                            WHERE canonical_id = hit.id AND node_id <> hit.id) m
        ORDER BY degree DESC LIMIT :limit
    """), {"like": f"%{q}%", "type": type, "limit": limit}).mappings().all()
    return {"nodes": [dict(r) for r in rows]}


@app.get("/graph/subgraph")
def graph_subgraph(seed: list[int] = Query(...), hops: int = Query(1, le=3), limit: int = Query(300, le=5000),
                   source: str | None = None, types: list[str] | None = Query(None),
                   since: datetime | None = Query(None, alias="from"), until: datetime | None = Query(None, alias="to"),
                   s: Session = Depends(get_session)):
    return neighborhood(s, seed, hops=hops, limit=limit, filters=GraphFilters(source, types, since, until))


@app.get("/graph/people")
def graph_people(seed: int, limit: int = Query(60, le=500), s: Session = Depends(get_session)):
    return people(s, seed, limit=limit)


@app.get("/graph/network")
def graph_network(seed: int, people: int = Query(50, le=200), hubs: int = Query(30, le=100),
                  s: Session = Depends(get_session)):
    """People plus the group chats, threads, and events that join them (clusters, not a star)."""
    return network(s, seed, people_limit=people, hub_limit=hubs)


@app.get("/graph/explain")
def graph_explain(a: int, b: int, s: Session = Depends(get_session)):
    """Why a and b are connected: shared conversations (counts, dates, latest excerpt) and shared events."""
    return explain(s, a, b)


@app.get("/graph/node/{node_id}")
def graph_node(node_id: int, s: Session = Depends(get_session)):
    n = s.execute(text("SELECT id, type, key, name, props, source_item_id FROM nodes WHERE id = :id"),
                  {"id": node_id}).mappings().first()
    if not n:
        raise HTTPException(404)
    degree = dict(s.execute(text("""
        SELECT type, count(*) FROM edges WHERE src = :id OR dst = :id GROUP BY 1
    """), {"id": node_id}).all())
    source = None
    if n["source_item_id"]:
        source = s.execute(text("""
            SELECT id, source, external_id, title, body_text, occurred_at, raw_meta
            FROM source_items WHERE id = :id
        """), {"id": n["source_item_id"]}).mappings().first()
    return {**dict(n), "degree": degree, "source_item": dict(source) if source else None}


def cluster(s: Session, node_id: int) -> list[int]:
    """All identities entity resolution merged with this node (just [node_id] if unmerged)."""
    ids = s.execute(text("""
        SELECT node_id FROM canonical WHERE canonical_id =
            (SELECT COALESCE((SELECT canonical_id FROM canonical WHERE node_id = :id), :id))
    """), {"id": node_id}).scalars().all()
    return list({node_id, *ids})


@app.get("/graph/node/{node_id}/evidence")
def graph_evidence(node_id: int, limit: int = Query(50, le=200), s: Session = Depends(get_session)):
    """Model-derived edges touching this node, each with the source text around its span ("why do you
    believe this"). Deterministic edges are omitted: their evidence is the header itself."""
    rows = s.execute(text("""
        SELECT e.id, e.type, e.src, e.dst, e.confidence, e.model, e.extractor, e.extractor_version, e.props,
               e.span_start, e.span_end, si.id AS source_item_id, si.title, si.occurred_at, si.external_id,
               substr(si.body_text, greatest(1, e.span_start + 1 - 160),
                      least(e.span_end - e.span_start, 400) + least(e.span_start, 160) + 160) AS window,
               greatest(0, e.span_start - 160) AS window_start,
               (SELECT name FROM nodes WHERE id = CASE WHEN e.src = ANY(:ids) THEN e.dst ELSE e.src END) AS other_name,
               (SELECT type FROM nodes WHERE id = CASE WHEN e.src = ANY(:ids) THEN e.dst ELSE e.src END) AS other_type
        FROM edges e JOIN source_items si ON si.id = e.source_item_id
        WHERE (e.src = ANY(:ids) OR e.dst = ANY(:ids)) AND e.extractor <> 'spine' AND e.span_start IS NOT NULL
        ORDER BY si.occurred_at DESC NULLS LAST LIMIT :limit
    """), {"ids": cluster(s, node_id), "limit": limit}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        # Offsets of the span inside `window`, so the client can highlight without re-searching.
        d["hl_start"] = r["span_start"] - r["window_start"]
        d["hl_end"] = min(r["span_end"], r["span_start"] + 400) - r["window_start"]
        out.append(d)
    return {"evidence": out}
