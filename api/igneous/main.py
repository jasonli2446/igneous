from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.db import get_session
from igneous.graph.traversal import neighborhood, people
from igneous.rag.search import Filters, hybrid

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
        "chunks": s.execute(text("SELECT count(*), count(embedding) FROM chunks")).one()._asdict(),
    }


@app.get("/search")
def search(q: str, source: str | None = None, person: str | None = None,
           since: datetime | None = Query(None, alias="from"), until: datetime | None = Query(None, alias="to"),
           k: int = Query(10, le=50), mode: str = Query("hybrid", pattern="^(hybrid|lexical|dense)$"),
           s: Session = Depends(get_session)):
    return {"hits": hybrid(s, q, Filters(source, person, since, until), k=k, mode=mode)}


@app.post("/ask")
def ask(body: dict, s: Session = Depends(get_session)):
    """Placeholder until graph-augmented Q&A lands (#qa): returns the evidence an answer would cite."""
    hits = hybrid(s, body["question"], k=8)
    return {"answer": None, "status": "retrieval-only", "citations": hits}


@app.get("/graph/search")
def graph_search(q: str, type: str | None = None, limit: int = Query(20, le=100),
                 s: Session = Depends(get_session)):
    rows = s.execute(text("""
        SELECT n.id, n.type, n.name, n.key,
               (SELECT count(*) FROM edges e WHERE e.src = n.id OR e.dst = n.id) AS degree
        FROM nodes n
        WHERE (n.name ILIKE :like OR n.key ILIKE :like) AND (CAST(:type AS text) IS NULL OR n.type = :type)
          AND n.type <> 'Message'
        ORDER BY degree DESC LIMIT :limit
    """), {"like": f"%{q}%", "type": type, "limit": limit}).mappings().all()
    return {"nodes": [dict(r) for r in rows]}


@app.get("/graph/subgraph")
def graph_subgraph(seed: list[int] = Query(...), hops: int = Query(1, le=3), limit: int = Query(300, le=5000),
                   types: list[str] | None = Query(None), s: Session = Depends(get_session)):
    return neighborhood(s, seed, hops=hops, limit=limit, edge_types=types)


@app.get("/graph/people")
def graph_people(seed: int, limit: int = Query(60, le=500), s: Session = Depends(get_session)):
    return people(s, seed, limit=limit)


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
