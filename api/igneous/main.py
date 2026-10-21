from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import jobs, sources
from igneous.config import settings
from igneous.connectors import secrets
from igneous.connectors.ai_exports import ExportConnector, ExportError, export_items, parse_export
from igneous.connectors.base import run_sync
from igneous.db import SessionLocal, get_session
from igneous.graph.entity_api import router as entity_router
from igneous.graph.history import commitments, decisions, relationship_history
from igneous.graph.traversal import GraphFilters, expand, explain, neighborhood, network, people, view
from igneous.llm import LLMDisabled
from igneous.rag import qa
from igneous.rag.constraints import apply
from igneous.rag.search import Filters, experts, hybrid
from igneous.sync_status import sync_status
from igneous.timeline import EntityNotFoundError, timeline


class NamedResponse(JSONResponse):
    """Every string the API returns shows contact names instead of phone numbers, wherever a number has a saved
    contact. Identity fields (`key`, `external_id`) stay raw so clients can still address things by them."""
    KEEP = frozenset({"key", "external_id", "chat", "handle"})
    LABELS = frozenset({"name", "thread", "title", "a_name", "b_name"})

    def render(self, content) -> bytes:
        from igneous.db import SessionLocal
        from igneous.graph.traversal import handle_names, name_handles
        with SessionLocal() as s:
            named = handle_names(s)

        def walk(v, field=None):
            if isinstance(v, str):
                # Labels (`name`, `thread`, `title`) mask unknown numbers; body text keeps them in full.
                return v if field in self.KEEP else name_handles(v, named, mask=field in self.LABELS)
            if isinstance(v, dict):
                return {k: walk(x, k) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x, field) for x in v]
            return v
        return super().render(walk(jsonable_encoder(content)))


app = FastAPI(title="Igneous", version="0.1.0", default_response_class=NamedResponse)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(entity_router)


@app.on_event("startup")
def _fail_interrupted_jobs():
    with SessionLocal() as s:
        jobs.fail_interrupted(s)


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
    connector: Literal["email", "imessage", "gmail", "gcal", "gcontacts", "drive", "fs", "notes"] | None = None,
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
    return {"hits": hybrid(s, q, apply(s, q, Filters(source, person, since, until)), k=k, mode=mode)}


@app.get("/experts")
def expert_search(q: str = Query(..., min_length=1), k: int = Query(10, ge=1, le=20),
                  evidence_limit: int = Query(3, ge=1, le=5),
                  mode: str = Query("hybrid", pattern="^(hybrid|lexical|dense)$"),
                  s: Session = Depends(get_session)):
    """People involved in query-relevant items, with at most five bounded evidence excerpts each."""
    if not q.strip():
        raise HTTPException(422, "q must contain non-whitespace characters")
    return {"experts": experts(s, q, k=k, evidence_limit=evidence_limit, mode=mode)}


@app.post("/import/ai-export")
def import_ai_export(file: UploadFile, provider: Annotated[str | None, Form()] = None,
                     s: Session = Depends(get_session)):
    """Import a ChatGPT / claude.ai / Gemini (Takeout JSON) export, zipped or raw. Only the parsed, redacted
    day-items are stored; the upload sits in Starlette's spooled temp file and is discarded after the request."""
    try:
        kind, convs = parse_export(file.file, provider or None)
    except ExportError as e:
        raise HTTPException(422, str(e)) from e
    items = export_items(kind, convs)
    stats = run_sync(s, ExportConnector(items), account=kind)
    return {"provider": kind, "conversations": len(convs), "items_inserted": stats["inserted"],
            "items_updated": stats["updated"], "redactions": sum(i.raw_meta["redactions"] for i in items)}


@app.post("/import/takeout")
async def import_takeout(request: Request):
    """Import a Google Takeout export (My Activity incl. Gemini, Chrome, Maps) as a background job; poll /jobs/{id}.
    Send multipart `file` parts (the zip(s) as downloaded) or JSON/form `{"path": "<zip or folder>"}` and/or
    `scan_downloads: true` to read ~/Downloads/takeout-*.zip in place. This API runs on your computer, so reading
    a local path is fine and avoids pushing multi-GB zips through the browser."""
    import shutil
    import tempfile
    from pathlib import Path

    from igneous.connectors import takeout

    tmp: str | None = None
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            body = await request.json()
            paths, scan = body.get("path") or body.get("paths"), bool(body.get("scan_downloads"))
        else:
            form = await request.form()
            paths, scan = [str(p) for p in form.getlist("path") if p], str(form.get("scan_downloads", "")).lower() in ("1", "true", "on")
            uploads = [f for f in form.getlist("file") if hasattr(f, "file")]
            if uploads:
                tmp = tempfile.mkdtemp(prefix="igneous-takeout-")
                for i, up in enumerate(uploads):
                    with open(Path(tmp) / f"{i}-{Path(up.filename or 'takeout.zip').name}", "wb") as out:
                        shutil.copyfileobj(up.file, out)
                paths = [*paths, tmp]
        resolved = takeout.resolve_paths(paths, scan)
        takeout.preflight(resolved)
    except (takeout.TakeoutError, ValueError) as e:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(422, str(e)) from e

    def runner(sess, progress):
        try:
            return takeout.run_import(sess, resolved, progress)
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)

    try:
        return _start_job("import", "takeout", runner)
    except HTTPException:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        raise


@app.get("/sources")
def list_sources(s: Session = Depends(get_session)):
    """Every connector and import format with plain-language setup, requirement checks, and live status."""
    return {"sources": sources.describe(s)}


def _start_job(kind: str, source: str, runner, account: str | None = None):
    try:
        return sources.job_view(jobs.start(kind, source, runner, account))
    except jobs.JobBusy as e:
        raise HTTPException(409, str(e)) from e


@app.post("/sources/{source_id}/sync")
def sync_source(source_id: str, account: str | None = None):
    """Run `igneous sync <source_id>` in the background; poll /jobs/{id}."""
    if source_id not in sources.SYNCABLE:
        raise HTTPException(404, f"{source_id} is not a source you can sync from here")
    ok, message = sources.check_requirements(source_id)
    if not ok:
        raise HTTPException(422, message)
    return _start_job("sync", source_id, lambda sess, progress: sources.build(source_id, sess, account, progress),
                      account)


@app.get("/sources/google/connect")
def google_connect(request: Request):
    """Redirect to Google's consent screen; Google sends the browser back to /sources/google/callback."""
    try:
        url = sources.google_authorization_url(str(request.url_for("google_callback")))
    except FileNotFoundError as e:
        raise HTTPException(422, str(e)) from e
    return RedirectResponse(url)


@app.get("/sources/google/callback")
def google_callback(state: str = "", code: str = "", error: str = "", s: Session = Depends(get_session)):
    back = f"{settings.web_url.rstrip('/')}/settings"
    if error or not code:
        return RedirectResponse(f"{back}?connect_error={quote(error or 'Google sign-in was cancelled.')}")
    try:
        email = sources.google_finish(s, state, code)
    except Exception as e:
        return RedirectResponse(f"{back}?connect_error={quote(str(e)[:200] or 'Google sign-in failed.')}")
    return RedirectResponse(f"{back}?connected={quote(email)}")


class GithubToken(BaseModel):
    token: str


@app.post("/sources/github/token")
def github_token(body: GithubToken, s: Session = Depends(get_session)):
    """Store a fine-grained read-only token encrypted at rest; it is never returned or logged."""
    token = body.token.strip()
    if len(token) < 20 or " " in token:
        raise HTTPException(422, "That does not look like a GitHub token.")
    from igneous.connectors.github import GithubClient, GithubError

    try:
        profile, _, _ = GithubClient(token).get("/user")
    except GithubError as e:
        raise HTTPException(422, str(e)) from e
    login = profile.get("login") if isinstance(profile, dict) else None
    if not login:
        raise HTTPException(422, "GitHub did not return an account for that token.")
    secrets.save(s, "github", login, token)
    return {"stored": True}


@app.get("/jobs")
def list_jobs(s: Session = Depends(get_session)):
    return {"jobs": [sources.job_view(j) for j in jobs.recent(s)]}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, s: Session = Depends(get_session)):
    job = jobs.get(s, job_id)
    if job is None:
        raise HTTPException(404)
    return sources.job_view(job)


@app.post("/pipeline/run")
def run_pipeline():
    """Build the graph from whatever is new (spine, chunk, embed, topics, entities, self, resolve; not extract)."""
    return _start_job("pipeline", "pipeline", sources.run_pipeline)


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


@app.get("/timeline")
def relationship_timeline(a: int = Query(..., ge=1), b: int | None = Query(None, ge=1),
                          limit: int = Query(50, ge=1, le=200), s: Session = Depends(get_session)):
    """Dated, bounded direct edges for two entities, or one entity's dated activity.

    Evidence is metadata-only: this endpoint never returns source titles, bodies,
    external IDs, raw metadata, or extracted text spans.
    """
    try:
        return timeline(s, a=a, b=b, limit=limit)
    except EntityNotFoundError as e:
        raise HTTPException(404, f"Entity {e.args[0]} not found") from e


@app.get("/graph/version")
def graph_version(s: Session = Depends(get_session)):
    """Cheap change token for clients to refresh an open graph without polling its data."""
    from igneous.graph import dirty
    return {"version": str(dirty.version(s))}


@app.get("/commitments")
def list_commitments(status: str = Query("open", pattern="^(open|resolved|all)$"),
                     limit: int = Query(50, ge=1, le=200), s: Session = Depends(get_session)):
    """Model-derived commitments with verified, source-local citations.

    This never presents a derived promise as a user correction or manually entered
    fact: every returned record has ``derived: true`` and ``origin: model``.
    """
    return {"commitments": commitments(s, status=status, limit=limit)}


@app.get("/decisions")
def list_decisions(limit: int = Query(50, ge=1, le=200), s: Session = Depends(get_session)):
    """Model-derived decisions with verified source-local citations only."""
    return {"decisions": decisions(s, limit=limit)}


@app.get("/relationships/history")
def past_relationships(person: int = Query(..., ge=1), before: datetime | None = None,
                       days: int = Query(90, ge=1, le=730), limit: int = Query(20, ge=1, le=100),
                       s: Session = Depends(get_session)):
    """Who this person talked to in a bounded historical window, with item citations only."""
    try:
        # Validate upfront so unknown IDs don't misleadingly look like a quiet relationship window.
        from igneous.timeline import _entity
        entity = _entity(s, person)
    except EntityNotFoundError as e:
        raise HTTPException(404, f"Entity {e.args[0]} not found") from e
    if entity["type"] != "Person":
        raise HTTPException(422, "person must identify a Person node")
    return {"relationships": relationship_history(s, person, before=before, days=days, limit=limit)}


@app.get("/graph/search")
def graph_search(q: str, type: str | None = None, limit: int = Query(20, le=100),
                 s: Session = Depends(get_session)):
    """Find seed nodes by name or key. Merged identities collapse to their canonical node (one Kenneth Lay,
    not ten addresses); `aliases` counts the folded-in identities."""
    rows = s.execute(text("""
        WITH hit AS (
            SELECT COALESCE(c.canonical_id, n.id) AS id FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id
            WHERE (n.name ILIKE :like OR n.key ILIKE :like) AND (CAST(:type AS text) IS NULL OR n.type = :type)
              AND n.type <> 'Message' AND NOT COALESCE((n.props->>'hidden')::bool, false)
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


@app.get("/graph/view")
def graph_view(seed: int | None = None, per: int = Query(6, le=20), s: Session = Depends(get_session)):
    """The single graph: seed (default: you) -> categories that exist in the data -> their top members."""
    if seed is None:
        seed = s.execute(text("SELECT id FROM nodes WHERE type = 'Person' AND props->>'self' = 'true' LIMIT 1")).scalar()
        if seed is None:
            raise HTTPException(404, "No self node yet: run `igneous pipeline --stage self`.")
    return view(s, seed, per=per)


@app.get("/graph/expand")
def graph_expand(seed: int, node: int, visible: list[int] = Query([]), k: int = Query(6, le=20),
                 offset: int = 0, s: Session = Depends(get_session)):
    """Reveal a node's top connections that aren't already on screen."""
    return expand(s, seed, node, visible, k=k, offset=offset)


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
