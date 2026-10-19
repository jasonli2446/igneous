# Igneous

A creation layer for personal knowledge graphs: ingest files, email, documents, and calendar into a typed,
provenance-tracked graph in Postgres + pgvector, then search and explore it. Backlog: GitHub Issues.

## Quick start

Needs Docker, [uv](https://docs.astral.sh/uv/), and Node 20+.

```bash
make enron      # download + extract the Enron mailboxes into data/ (~450 MB, once)
make demo       # Postgres up, migrate, ingest one mailbox (~3k emails), build graph + index
make api        # terminal 1: API on :8000 (OpenAPI docs at /docs)
make web        # terminal 2: explorer on :3000
```

Try it: on the Explorer, search `jeff.skilling` or `phillip.allen` and pick the Person. Search lives at `/search`.

`make baseline` ingests six mailboxes (~26k emails, the set the demo uses). Ingest + graph take ~3 min;
embedding takes ~45 min on CPU. Search works during that, it just falls back to lexical for unembedded chunks.

`igneous pipeline` runs spine -> chunk -> embed -> extract -> resolve. Extraction calls the model, so it's
bounded: `make demo EXTRACT=0` skips it, `EXTRACT=500` does more. Everything is incremental: re-running only
processes what changed.
`docker compose up` also runs the api and web in containers.

## Layout

| Path | What |
|---|---|
| `api/igneous/connectors/` | `base.py` connector contract + resumable sync runner; `email_maildir.py` Enron importer |
| `api/igneous/graph/` | `spine.py` deterministic graph (stage one); `extract.py` LLM mentions/topics with span provenance (stage two); `resolve.py` entity resolution; `traversal.py` k-hop + people view |
| `api/igneous/rag/` | `chunking.py`, `embed.py` (local ONNX, 384-d), `search.py` hybrid lexical + dense with RRF, `qa.py` graph-augmented answers with citations |
| `api/igneous/llm.py` | the only place model calls happen |
| `api/eval/` | gold questions, ER labels, harnesses, results (see `api/eval/README.md`) |
| `api/igneous/pipeline.py` | per-stage watermarks; every stage is incremental and safe to re-run |
| `api/igneous/main.py` | FastAPI: `/search`, `/ask`, `/graph/search`, `/graph/subgraph`, `/graph/people`, `/graph/node/{id}`, `/graph/node/{id}/evidence`, `/stats` |
| `api/migrations/` | schema (plain SQL in `0001_core_schema.py`) |
| `web/` | Next.js: explorer (sigma.js) with inspector + evidence, Ask, Search |

## Adding a stage or connector

- **Connector:** implement `sync(cursor) -> Iterator[SourceItem | Checkpoint]` (see `connectors/base.py`), register it in `cli.py`.
- **Pipeline stage:** read rows newer than `watermark(s, STAGE)`, write, then `advance(...)`. See `rag/chunking.py`.

## Known gaps in the baseline

- Enron has no `In-Reply-To`/`References` headers. Threads are keyed on subject + participants; `REPLIES_TO` is inferred and sparse.
- Enron stores one email in several folders with different Message-IDs. Messages dedupe on a content fingerprint (`raw_meta.fingerprint`).
- Embedding model is a placeholder (`bge-small-en-v1.5`, 384-d) until the Bedrock interop decision (#11).

## Model calls

Q&A uses Claude Sonnet 5.5 on Amazon Bedrock. Set `AWS_BEARER_TOKEN_BEDROCK` and `AWS_REGION` (e.g. `us-east-1`);
everything goes through `api/igneous/llm.py`. `IGNEOUS_LLM_ENABLED=false` turns off all model calls (search and
the graph keep working; `/ask` returns 503).

## MCP server (use the graph from Claude Code, Claude Desktop, Cursor)

Read-only tools: `search`, `find_entity`, `get_node`, `correspondents`, `neighbors`, `evidence`, `ask`.

```bash
claude mcp add igneous -e AWS_REGION=us-east-1 -e AWS_BEARER_TOKEN_BEDROCK=$AWS_BEARER_TOKEN_BEDROCK \
  -- uv run --directory "$(pwd)/api" igneous mcp
```

The model credentials only matter for `ask`; MCP clients don't pass your shell's environment through, so
set them with `-e` as above (other tools work without them). Postgres must be running (`make db`).
