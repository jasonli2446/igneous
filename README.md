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

`make synthetic-corpus` writes a small, deterministic fixture corpus to `data/synthetic-cross-source/`: five
invented document-style files plus two ICS calendar events that reuse the public Enron demo persona addresses.
It makes no network or model calls and **does not import** the data; it is input for the future filesystem and
ICS importers. Every output is labeled synthetic and excludes real email text, credentials, and personal data.
Re-run it safely to restore the exact fixture, or validate an existing output with:

```bash
cd api && uv run igneous verify-synthetic-corpus ../data/synthetic-cross-source
```

`igneous pipeline` runs spine -> chunk -> embed -> extract -> topics -> resolve. The topics stage lowercases and
conservatively lemmatizes keys, merges only high-similarity local-embedding duplicates, and adds `RELATED_TO` after
three shared messages; moved `ABOUT` edges keep their original provenance. Extraction calls the model, so it's
bounded: `make demo EXTRACT=0` skips it, `EXTRACT=500` does more. Everything is incremental: re-running only
processes what changed.
`docker compose up` also runs the api and web in containers.

## Layout

| Path | What |
|---|---|
| `api/igneous/connectors/` | `base.py` connector contract + resumable sync runner; `email_maildir.py` Enron importer |
| `api/igneous/graph/` | `spine.py` deterministic graph (stage one); `extract.py` LLM mentions/topics with span provenance (stage two); `resolve.py` entity resolution; `traversal.py` k-hop + people view |
| `api/igneous/rag/` | `chunking.py`, `embed.py` (local ONNX, 384-d), `search.py` hybrid lexical + dense with RRF, `rerank.py` optional local cross-encoder, `qa.py` graph-augmented answers with citations |
| `api/igneous/llm.py` | the only place model calls happen |
| `api/eval/` | gold questions, ER labels, harnesses, results (see `api/eval/README.md`) |
| `api/igneous/pipeline.py` | per-stage watermarks; every stage is incremental and safe to re-run |
| `api/igneous/main.py` | FastAPI: `/search`, `/ask`, `/graph/search`, `/graph/subgraph`, `/graph/people`, `/graph/node/{id}`, `/graph/node/{id}/evidence`, `/stats` |
| `api/migrations/` | schema (plain SQL in `0001_core_schema.py`) |
| `web/` | Next.js: explorer (sigma.js) with inspector + evidence, Ask, Search |

## Your own Messages (iMessage / SMS)

```bash
createdb -h localhost -U igneous igneous_personal     # keep personal data out of the shared Enron DB
export IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_personal
cd api && uv run alembic upgrade head
uv run igneous sync imessage --since-days 90 && uv run igneous pipeline --extract 0
```

Reads `~/Library/Messages/chat.db` read-only; each conversation-day becomes one item. Your terminal needs
Full Disk Access (System Settings → Privacy & Security). **Never commit or demo personal data** (proposal
CO-5): use the Enron DB for anything shared.

## Adding a stage or connector

- **Connector:** implement `sync(cursor) -> Iterator[SourceItem | Checkpoint]` (see `connectors/base.py`), register it in `cli.py`.
- **Pipeline stage:** read rows newer than `watermark(s, STAGE)`, write, then `advance(...)`. See `rag/chunking.py`.

## Known gaps in the baseline

- Enron has no `In-Reply-To`/`References` headers. Threads are keyed on subject + participants; `REPLIES_TO` is inferred and sparse.
- Enron stores one email in several folders with different Message-IDs. Messages dedupe on a content fingerprint (`raw_meta.fingerprint`).
- Embedding model is a placeholder (`bge-small-en-v1.5`, 384-d) until the Bedrock interop decision (#11).

## Model calls

Q&A uses Claude Sonnet 5.5 on Amazon Bedrock. Set `AWS_BEARER_TOKEN_BEDROCK` and `AWS_REGION` (e.g. `us-east-1`);
application model access goes through `api/igneous/providers.py`: local FastEmbed embeddings, a local FastEmbed
cross-encoder reranker, and the existing Bedrock chat client. Select them with `IGNEOUS_EMBEDDING_PROVIDER`,
`IGNEOUS_RERANK_PROVIDER`, and `IGNEOUS_CHAT_PROVIDER`; the supported defaults are `fastembed`, `cross_encoder`,
and `bedrock`. `IGNEOUS_LLM_ENABLED=false` turns off hosted model calls (search and the graph keep working; `/ask`
returns 503).

Q&A reranks the graph-expanded candidate pool down to the top 8 evidence chunks. Set
`IGNEOUS_RERANKER_ENABLED=false` to preserve graph retrieval order, or adjust the bounded input with
`IGNEOUS_RERANKER_POOL_SIZE` (32 by default). If its model files cannot load, Q&A logs a warning and safely
preserves that order instead.

To verify providers without credentials, run `cd api && uv run python scripts/provider_smoke.py`. It exercises the
local embedding and reranking models only. Add `--hosted` only with an explicit real `AWS_BEARER_TOKEN_BEDROCK`; that
mode makes one paid Bedrock chat request and is never run by the test suite.

## MCP server (use the graph from Claude Code, Claude Desktop, Cursor)

Read-only tools: `search`, `find_entity`, `get_node`, `correspondents`, `neighbors`, `evidence`, `ask`.

```bash
claude mcp add igneous -e AWS_REGION=us-east-1 -e AWS_BEARER_TOKEN_BEDROCK=$AWS_BEARER_TOKEN_BEDROCK \
  -- uv run --directory "$(pwd)/api" igneous mcp
```

The model credentials only matter for `ask`; MCP clients don't pass your shell's environment through, so
set them with `-e` as above (other tools work without them). Postgres must be running (`make db`).

## Google (Gmail, Calendar, Contacts)

1. Put the OAuth desktop client JSON at `data/google/client_secret.json` (git-ignored).
2. `cd api && uv run igneous connect google` opens Google sign-in (read-only scopes). Click **Continue** past
   "Google hasn't verified this app": the project is in Testing mode, so tokens expire after 7 days; re-run it then.
3. `uv run igneous sync gcal`, `uv run igneous sync gcontacts`, `uv run igneous sync gmail --since-days 90`, then
   `uv run igneous pipeline`.

Gmail skips Promotions/Social/Forums by default (`--all-mail` to include them); in a real inbox they were 94% of mail.
**Quota:** this Testing-mode project measured about 730 units/min, so roughly 120 messages/min, far below Google's
published default. The connector paces itself (`IGNEOUS_GMAIL_UNITS_PER_MIN`, default 600), so a large inbox
takes a while but won't error out. Resuming continues mid-page.
