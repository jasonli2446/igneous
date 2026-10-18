# Igneous

A creation layer for personal knowledge graphs: ingest files, email, documents, and calendar into a typed,
provenance-tracked graph in Postgres + pgvector, then search and explore it. Backlog: GitHub Issues.

## Quick start (local dev)

```bash
docker compose up -d db                      # Postgres 16 + pgvector on :5432
make enron                                   # download + extract demo mailboxes into data/ (~450 MB)
cd api && uv sync && uv run alembic upgrade head
uv run igneous sync email --mailbox lay-k --mailbox skilling-j --mailbox allen-p \
  --mailbox arnold-j --mailbox delainey-d --mailbox lavorato-j     # ~26k emails, ~1 min
uv run igneous pipeline                      # spine -> chunk -> embed (embed takes a while on CPU)
uv run uvicorn igneous.main:app --reload     # API on :8000, docs at /docs
cd ../web && npm install && npm run dev      # explorer on :3000
```

Or everything in containers: `docker compose up` (then run the `igneous` CLI with `docker compose exec api igneous ...`).

## Layout

| Path | What |
|---|---|
| `api/igneous/connectors/` | `base.py` connector contract + resumable sync runner; `email_maildir.py` Enron importer |
| `api/igneous/graph/` | `spine.py` deterministic graph (stage one); `traversal.py` bounded k-hop |
| `api/igneous/rag/` | `chunking.py`, `embed.py` (local ONNX, 384-d), `search.py` hybrid lexical + dense with RRF |
| `api/igneous/pipeline.py` | per-stage watermarks; every stage is incremental and safe to re-run |
| `api/igneous/main.py` | FastAPI: `/search`, `/ask` (retrieval-only for now), `/graph/search`, `/graph/subgraph`, `/graph/node/{id}`, `/stats` |
| `api/migrations/` | schema (plain SQL in `0001_core_schema.py`) |
| `web/` | Next.js explorer (sigma.js) and search page |

## Adding a stage or connector

- **Connector:** implement `sync(cursor) -> Iterator[SourceItem | Checkpoint]` (see `connectors/base.py`), register it in `cli.py`.
- **Pipeline stage:** read rows newer than `watermark(s, STAGE)`, write, then `advance(...)`. See `rag/chunking.py`.

## Known gaps in the baseline

- Enron has no `In-Reply-To`/`References` headers. Threads are keyed on subject + participants; `REPLIES_TO` is inferred and sparse.
- Enron stores one email in several folders with different Message-IDs. Messages dedupe on a content fingerprint (`raw_meta.fingerprint`).
- Embedding model is a placeholder (`bge-small-en-v1.5`, 384-d) until the Bedrock interop decision (#11).
