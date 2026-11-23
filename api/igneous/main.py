import zipfile
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from igneous import jobs, source_catalog, sources, uploads
from igneous.access import LocalOnly, WebAppCORS
from igneous.config import settings
from igneous.connectors import secrets
from igneous.db import SessionLocal, get_session, schema_status
from igneous.graph import identity
from igneous.graph.entity_api import router as entity_router
from igneous.graph.find import TYPES as FIND_TYPES
from igneous.graph.find import find
from igneous.graph.history import commitments
from igneous.graph.summary import summarize
from igneous.graph.traversal import expand, explain, view
from igneous.items import router as items_router
from igneous.llm import ProviderError, ProviderUnavailable, model_status
from igneous.rag import qa
from igneous.rag.constraints import apply, describe, person_by_address_or_name
from igneous.rag.search import Filters, experts, hybrid
from igneous.timeline import timeline


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


app = FastAPI(title="Igneous", version="0.1.0", lifespan=lifespan)
# Added last, LocalOnly runs first: a foreign Host or Origin is refused before CORS or any route sees it.
app.add_middleware(WebAppCORS, allow_methods=["*"], allow_headers=["*"])
app.add_middleware(LocalOnly)
app.include_router(entity_router)
app.include_router(items_router)


@app.exception_handler(identity.NotFound)
def node_not_found(_request: Request, e: identity.NotFound) -> JSONResponse:
    """A route given the id of no node answers 404, whatever reader found it missing."""
    return JSONResponse({"detail": str(e)}, status_code=404)


@app.get("/health")
def health(s: Session = Depends(get_session)):
    s.execute(text("SELECT 1"))
    return {"ok": True}


SearchMode = Literal["hybrid", "lexical", "dense"]


def _request_filters(s: Session, sources: list[str] | None, person: str | None, since: datetime | None,
                     until: datetime | None) -> Filters:
    """The filters a request passes, checked: known source values, a person in the records, a forward date range."""
    unknown = sorted(set(sources or ()) - set(source_catalog.BY_VALUE))
    if unknown:
        raise HTTPException(422, f"Unknown source {', '.join(unknown)}. Sources: {', '.join(source_catalog.BY_VALUE)}.")
    if since and until and since >= until:
        raise HTTPException(422, "`from` must be earlier than `to`.")
    person_id = None
    if person and person.strip():
        person_id = person_by_address_or_name(s, person)
        if person_id is None:
            raise HTTPException(422, f"No one in your records has the address or name {person.strip()!r}.")
    return Filters(sources=tuple(sources or ()), person_id=person_id, since=since, until=until)


@app.get("/search")
def search(q: str = Query(..., min_length=1, max_length=500, pattern=r"\S"),
           sources: list[str] | None = Query(None), person: str | None = None,
           since: datetime | None = Query(None, alias="from"), until: datetime | None = Query(None, alias="to"),
           k: int = Query(10, ge=1, le=50), mode: SearchMode = "hybrid", s: Session = Depends(get_session)):
    """Passages for `q`, best first. `sources` (repeatable) are stored source values; `person` is an address or an
    exact name, matching messages any of that person's merged identities sent or received; `from`/`to` bound the
    date (`to` exclusive). The query can narrow further ("from Jeff Skilling", "in my calendar", "in May 2001");
    `filters` says what was applied and which of it came from the query."""
    f = apply(s, q, _request_filters(s, sources, person, since, until))
    return {"hits": hybrid(s, q, f, k=k, mode=mode), "filters": describe(s, f)}


@app.get("/experts")
def expert_search(q: str = Query(..., min_length=1, max_length=500, pattern=r"\S"), k: int = Query(10, ge=1, le=20),
                  evidence_limit: int = Query(3, ge=1, le=5), sources: list[str] | None = Query(None),
                  person: str | None = None, since: datetime | None = Query(None, alias="from"),
                  until: datetime | None = Query(None, alias="to"), mode: SearchMode = "hybrid",
                  s: Session = Depends(get_session)):
    """People involved in query-relevant items, with at most five bounded evidence excerpts each. Takes the same
    filters as /search, so its people come from the items a search with them finds."""
    f = apply(s, q, _request_filters(s, sources, person, since, until))
    return {"experts": experts(s, q, k=k, evidence_limit=evidence_limit, mode=mode, f=f), "filters": describe(s, f)}


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


@app.post("/import/vcard")
async def import_vcard(request: Request):
    """Import a contacts export (vCard .vcf from Google Contacts, Apple Contacts or iCloud) as a background job; poll
    /jobs/{id}. Send multipart `file` parts (the .vcf files) or JSON/form `{"path": "<file, folder or zip>"}`. Its
    result: {cards, phones, emails, without_phone_or_email, files, inserted, updated, unchanged, redactions}."""
    from igneous.connectors import vcard

    req = await _import_request(request, "vcard")

    def prepare():
        resolved = vcard.resolve_paths([*req.paths, *map(str, req.staged.files)])
        vcard.preflight(resolved)
        return lambda sess, progress: vcard.run_import(sess, resolved, progress)

    return await _start_import("vcard", req, prepare)


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
    question: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    person: str | None = None  # an address or exact name: only messages that person sent or received
    # False: the eval's baseline (retrieval only: no graph expansion, no reranking).
    use_graph: bool = True
    mode: SearchMode = "hybrid"


@app.post("/ask")
def ask(body: AskBody, s: Session = Depends(get_session)):
    """A cited answer from the sources opted in to the hosted model (IGNEOUS_LLM_SOURCES), or `answerable: false`;
    `filters` says what narrowed the search, as in /search. 503 when the hosted model is off, not configured or
    can't be used now (unreachable, rate-limited, refusing the credentials); 502 when it gives no usable answer (a
    refusal, a cut-off or empty response, a rejected request). `detail` says which."""
    f = _request_filters(s, None, body.person, None, None)
    try:
        return qa.answer(s, body.question, f, use_graph=body.use_graph, mode=body.mode)
    except ProviderUnavailable as e:
        raise HTTPException(503, str(e)) from e
    except ProviderError as e:
        raise HTTPException(502, str(e)) from e


@app.get("/ask/suggestions")
def ask_suggestions(s: Session = Depends(get_session)):
    """Starter questions about the user's own people and projects; [] on an empty graph."""
    from igneous.suggestions import suggestions
    return suggestions(s)


@app.get("/timeline")
def relationship_timeline(a: int = Query(..., ge=1), b: int | None = Query(None, ge=1),
                          limit: int = Query(50, ge=1, le=200), s: Session = Depends(get_session)):
    """Dated links between two nodes, or one node's dated activity, newest first: graph metadata and source item ids,
    never record text (igneous/timeline.py). 404 for an unknown node."""
    return timeline(s, a=a, b=b, limit=limit)


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


FindType = Literal[FIND_TYPES]


@app.get("/graph/find")
def graph_find(q: str = Query(..., min_length=1, max_length=200), seed: int | None = Query(None, ge=1),
               limit: int = Query(8, ge=1, le=20), types: list[FindType] | None = Query(None),
               s: Session = Depends(get_session)):
    """Explorer "Jump to" and the pickers: a few well-ranked things (people, projects, groups, events, orgs, places,
    folders, files) named like `q`, each identity once, ranked by closeness to `seed` (default: you); `types`
    (repeatable) keeps only those. Messages are never returned; use /search for those."""
    return {"results": find(s, q, seed, limit, types)}


@app.get("/graph/view")
def graph_view(seed: int | None = Query(None, ge=1), per: int = Query(6, ge=1, le=20),
               s: Session = Depends(get_session)):
    """The opening view for a centre (default: you): your orgs, then the strongest people, groups and sustained
    projects, each linked to the centre, with what is part of what and the ties among them. Every edge carries its
    `origin` and whether it is `stated`. With no seed and no one to open on, `{seed: null, nodes: [], edges: [], empty:
    true, reason}`: 'nothing_built' (connect a source on the Sources page and press Rebuild) or 'no_owner' (no source
    says which person is you, such as your Google account, your Messages, a mail archive with one owner or your
    LinkedIn export, or nothing links you to anyone yet: search for someone to start from). 404 for an unknown seed."""
    return view(s, seed, per)


@app.get("/graph/expand")
def graph_expand(node: int = Query(..., ge=1), seed: int | None = Query(None, ge=1),
                 visible: list[int] = Query([], max_length=2000), k: int = Query(6, ge=1, le=20),
                 s: Session = Depends(get_session)):
    """Reveal a node's top connections that aren't on screen (`visible`) yet, around the centre `seed`. 404 for an
    unknown node or seed."""
    return expand(s, seed, node, visible, k=k)


@app.get("/graph/explain")
def graph_explain(a: int = Query(..., ge=1), b: int = Query(..., ge=1), s: Session = Depends(get_session)):
    """Why a and b are connected: shared conversations and events, their latest messages together, the links between
    them (each with its `origin`, `stated` and, in plain words, `how` it was made) or the shortest path, and your
    corrections to the pair. 404 for an unknown node, 422 when a and b are one."""
    try:
        return explain(s, a, b)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/graph/node/{node_id}/summary")
def graph_node_summary(node_id: int = PathParam(ge=1), s: Session = Depends(get_session)):
    """2-4 plain sentences on how this node connects to the rest of the graph, its top connections, and the
    source items the summary drew on. Cached; a deterministic summary stands in when the model is off."""
    return summarize(s, node_id)
