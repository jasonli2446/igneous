import zipfile
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from igneous import jobs, sources, uploads
from igneous.config import settings
from igneous.connectors import secrets
from igneous.db import SessionLocal, get_session, schema_status
from igneous.graph import identity
from igneous.graph.entity_api import router as entity_router
from igneous.graph.history import commitments, decisions, relationship_history
from igneous.graph.traversal import GraphFilters, expand, explain, neighborhood, network, people, view
from igneous.items import router as items_router
from igneous.llm import LLMDisabled, model_status
from igneous.rag import qa
from igneous.rag.constraints import apply
from igneous.rag.search import Filters, experts, hybrid
from igneous.timeline import EntityNotFoundError, timeline


class NamedResponse(JSONResponse):
    """Every string the API returns shows contact names instead of phone numbers, wherever a number has a saved
    contact. Identity fields (`key`, `external_id`) stay raw so clients can still address things by them."""
    KEEP = frozenset({"key", "external_id", "chat", "handle"})

    def render(self, content) -> bytes:
        from igneous.db import SessionLocal
        from igneous.graph.traversal import handle_names, name_handles
        with SessionLocal() as s:
            named = handle_names(s)

        def walk(v, field=None):
            if isinstance(v, str):
                # Names for known numbers everywhere; unknown numbers shortened to their last 4 digits too.
                return v if field in self.KEEP else name_handles(v, named, mask=True)
            if isinstance(v, dict):
                return {k: walk(x, k) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x, field) for x in v]
            return v
        return super().render(walk(jsonable_encoder(content)))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Refuse to serve a database whose schema isn't the code's; fail the running jobs whose process died, and remove
    the uploads they left."""
    if problem := schema_status():
        raise RuntimeError(problem)
    with SessionLocal() as s:
        jobs.fail_interrupted(s)
    uploads.sweep()
    yield


app = FastAPI(title="Igneous", version="0.1.0", default_response_class=NamedResponse, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(entity_router)
app.include_router(items_router)


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


@dataclass
class ImportRequest:
    """What an import route was sent: local paths to read in place (this API runs on your computer), and uploaded
    files, staged for the job."""
    paths: list[str]
    staged: uploads.Staged
    scan_downloads: bool = False
    provider: str | None = None


async def _import_request(request: Request, prefix: str) -> ImportRequest:
    """A JSON body {"path" | "paths", "scan_downloads", "provider"}, or a multipart form with the same fields and
    `file` parts, whose files are copied off the event loop (the form's own temporary files close once copied)."""
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        raw = body.get("path") or body.get("paths") or []
        return ImportRequest([raw] if isinstance(raw, str) else [str(p) for p in raw], uploads.Staged(None, []),
                             bool(body.get("scan_downloads")), body.get("provider") or None)
    async with request.form() as form:
        files = [f for f in form.getlist("file") if hasattr(f, "file")]
        return ImportRequest([str(p) for p in form.getlist("path") if isinstance(p, str) and p.strip()],
                             await uploads.stage(files, prefix),
                             str(form.get("scan_downloads", "")).lower() in ("1", "true", "on"),
                             str(form.get("provider") or "") or None)


# An input that can't be imported: the reason goes back with 422.
IMPORT_ERRORS = (ValueError, OSError, zipfile.BadZipFile)


async def _start_import(source: str | Callable[[], str], req: ImportRequest, prepare: Callable[[], Callable]):
    """Check the input (`prepare` returns the job's runner, off the event loop) and start the import job, which then
    owns the staged uploads; they are removed at once if it doesn't start."""
    with req.staged:
        try:
            runner = await run_in_threadpool(prepare)
        except IMPORT_ERRORS as e:
            raise HTTPException(422, str(e)) from e
        job = _start_job("import", source() if callable(source) else source, req.staged.removing_after(runner))
        req.staged.hand_off()
        return job


@app.post("/import/ai-export")
async def import_ai_export(request: Request):
    """Import a ChatGPT or Claude.ai export (or Gemini's MyActivity.json from Takeout) as a background job; poll
    /jobs/{id}. Send a multipart `file` (the zip as emailed, or the conversations.json inside it) or JSON/form
    `{"path": ...}` to read it in place, and `provider` (chatgpt, claude_ai, gemini) only if it isn't recognised.
    The job reads one conversation at a time and stores only the redacted day-items; the upload is removed when it
    ends. Its result: {provider, conversations, inserted, updated, unchanged, redactions}."""
    from igneous.connectors import ai_exports

    req = await _import_request(request, "ai-export")
    found: dict[str, str] = {}

    def prepare():
        files = [Path(p).expanduser() for p in req.paths] + req.staged.files
        if len(files) != 1:
            raise ai_exports.ExportError("Send one export: the zip, or the conversations.json inside it.")
        found["provider"] = ai_exports.preflight(files[0], req.provider)
        return lambda sess, progress: ai_exports.run_import(sess, files[0], found["provider"], progress)

    return await _start_import(lambda: found["provider"], req, prepare)


@app.post("/import/takeout")
async def import_takeout(request: Request):
    """Import a Google Takeout export (My Activity incl. Gemini, Chrome, Maps) as a background job; poll /jobs/{id}.
    Send multipart `file` parts (the zip(s) as downloaded) or JSON/form `{"path": "<zip or folder>"}` and/or
    `scan_downloads: true` to read ~/Downloads/takeout-*.zip in place. This API runs on your computer, so reading
    a local path is fine and avoids pushing multi-GB zips through the browser."""
    from igneous.connectors import takeout

    req = await _import_request(request, "takeout")

    def prepare():
        resolved = takeout.resolve_paths([*req.paths, *map(str, req.staged.files)], req.scan_downloads)
        takeout.preflight(resolved)
        return lambda sess, progress: takeout.run_import(sess, resolved, progress)

    return await _start_import("takeout", req, prepare)


@app.get("/sources")
def list_sources(s: Session = Depends(get_session)):
    """Every connector and import format with plain-language setup, requirement checks, and live status, plus
    whether hosted model calls are on and which sources they may read."""
    return {"sources": sources.describe(s), "model": model_status()}


@app.post("/import/linkedin")
async def import_linkedin(request: Request):
    """Import a LinkedIn data export (the zip, or Connections only) as a background job; poll /jobs/{id}.
    Send a multipart `file` (the zip as downloaded) or JSON/form `{"path": "<zip or folder>"}`. This API runs on
    your computer, so reading a local path is fine."""
    from igneous.connectors import linkedin

    req = await _import_request(request, "linkedin")

    def prepare():
        resolved = linkedin.resolve_paths([*req.paths, *map(str, req.staged.files)])
        linkedin.preflight(resolved)
        return lambda sess, progress: linkedin.run_import(sess, resolved, progress)

    return await _start_import("linkedin", req, prepare)


def _start_job(kind: str, source: str, runner, account: str | None = None):
    try:
        return sources.job_view(jobs.start(kind, source, runner, account))
    except jobs.JobBusy as e:
        raise HTTPException(409, str(e)) from e


@app.post("/sources/{source_id}/sync")
def sync_source(source_id: str, account: str | None = None):
    """Sync a source in the background, as `igneous sync <source_id>` does: every connected account, or `account`,
    each from where its last sync stopped. Poll /jobs/{id}."""
    if source_id not in sources.SYNCABLE:
        raise HTTPException(404, f"{source_id} is not a source you can sync from here")
    ok, message = sources.check_requirements(source_id)
    if not ok:
        raise HTTPException(422, message)
    try:
        sources.check_options(source_id, sources.SyncOptions(), account)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _start_job("sync", source_id, lambda sess, progress: sources.sync(source_id, sess, account,
                                                                              progress=progress), account)


def _back_to_sources(**params: str) -> RedirectResponse:
    """Back to the Sources page, which shows `connected` or `connect_error`."""
    query = "&".join(f"{k}={quote(v)}" for k, v in params.items())
    return RedirectResponse(f"{settings.web_url.rstrip('/')}/settings?{query}")


@app.get("/sources/google/connect")
def google_connect(request: Request):
    """Redirect to Google's consent screen; Google sends the browser back to /sources/google/callback. Without an
    OAuth client file, back to the Sources page with what to do."""
    try:
        url = sources.google_authorization_url(str(request.url_for("google_callback")))
    except FileNotFoundError:
        return _back_to_sources(connect_error="No Google OAuth client yet: save its JSON as "
                                              "data/google/client_secret.json (step 1 under Gmail), then connect.")
    return RedirectResponse(url)


@app.get("/sources/google/callback")
def google_callback(state: str = "", code: str = "", error: str = "", s: Session = Depends(get_session)):
    if error or not code:
        return _back_to_sources(connect_error=error or "Google sign-in was cancelled.")
    try:
        email = sources.google_finish(s, state, code)
    except Exception as e:
        return _back_to_sources(connect_error=str(e)[:200] or "Google sign-in failed.")
    return _back_to_sources(connected=email)


@app.delete("/sources/google/accounts/{account}")
def google_disconnect(account: str, s: Session = Depends(get_session)):
    """Forget a Google account's token, so nothing syncs it. What it synced stays; connecting it again resumes."""
    if not secrets.delete(s, "google", account):
        raise HTTPException(404, f"No Google account {account} is connected.")
    return {"removed": account}


@app.delete("/sources/github/accounts/{account}")
def github_disconnect(account: str, s: Session = Depends(get_session)):
    """Forget a GitHub account's token, so nothing syncs it. What it synced stays."""
    if not secrets.delete(s, "github", account):
        raise HTTPException(404, f"No GitHub token is saved for {account}.")
    return {"removed": account}


class GithubToken(BaseModel):
    token: str


@app.post("/sources/github/token")
def github_token(body: GithubToken, s: Session = Depends(get_session)):
    """Store a fine-grained read-only token encrypted at rest; it is never returned or logged. A token for an account
    that already has one replaces it."""
    from igneous.connectors.github import GithubError, account_for

    try:
        login = account_for(body.token)
    except GithubError as e:
        raise HTTPException(422, str(e)) from e
    secrets.save(s, "github", login, body.token.strip())
    return {"stored": True, "account": login}


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
    """Rebuild as a background job: every pipeline stage except extract (igneous/pipeline.py), the derived graph
    published at once at the end; poll /jobs/{id} for the stage, step and counts."""
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


@app.get("/ask/suggestions")
def ask_suggestions(s: Session = Depends(get_session)):
    """Starter questions about the user's own people and projects; [] on an empty graph."""
    from igneous.suggestions import suggestions
    return suggestions(s)


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


@app.get("/graph/find")
def graph_find(q: str, seed: int | None = None, limit: int = Query(8, ge=1, le=20), s: Session = Depends(get_session)):
    """Explorer "Jump to": a few well-ranked things (people, projects, groups, events, orgs, places, files)
    for the query, deduped by identity. Messages are never returned; use /search for those."""
    from igneous.graph.find import find
    return {"results": find(s, q, seed, limit)}


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


_VIEW_CACHE: dict[tuple, dict] = {}


@app.get("/graph/view")
def graph_view(seed: int | None = None, per: int = Query(6, le=20), s: Session = Depends(get_session)):
    """The single graph: seed (default: you) -> categories that exist in the data -> their top members."""
    if seed is None:
        seed = identity.self_id(s)
        if seed is None:
            raise HTTPException(404, "No owner: no source says which person is you yet (your Google account, your "
                                     "Messages, a mail archive with one owner, your LinkedIn export). Pass a seed.")
    # Ranking every relationship takes seconds on a large mailbox; the result only changes when the graph does,
    # so cache it per (seed, per) and graph version (bumped once per published rebuild and by every correction).
    from igneous.graph import dirty
    key = (seed, per, dirty.version(s))
    hit = _VIEW_CACHE.get(key)
    if hit is None:
        hit = view(s, seed, per=per)
        _VIEW_CACHE.clear() if len(_VIEW_CACHE) > 64 else None
        _VIEW_CACHE[key] = hit
    return hit


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


@app.get("/graph/node/{node_id}/summary")
def graph_node_summary(node_id: int, s: Session = Depends(get_session)):
    """2-4 plain sentences on how this node connects to the rest of the graph, its top connections, and the
    source items the summary drew on. Cached; a deterministic summary stands in when the model is off."""
    from igneous.graph.summary import summarize
    out = summarize(s, node_id)
    if out is None:
        raise HTTPException(404)
    return out


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
    """), {"ids": identity.members(s, node_id), "limit": limit}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        # Offsets of the span inside `window`, so the client can highlight without re-searching.
        d["hl_start"] = r["span_start"] - r["window_start"]
        d["hl_end"] = min(r["span_end"], r["span_start"] + 400) - r["window_start"]
        out.append(d)
    return {"evidence": out}
