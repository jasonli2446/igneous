# Igneous

A creation layer for personal knowledge graphs: ingest files, email, documents, and calendar into a typed,
provenance-tracked graph in Postgres + pgvector, then search and explore it. Backlog: GitHub Issues.

## Setup

### 1. Prerequisites

- **Docker**: runs Postgres 16 with pgvector (`pgvector/pgvector:pg16`, see `docker-compose.yml`). If you bring your
  own Postgres it must be 16+ with the `vector` extension available; the first migration runs `CREATE EXTENSION vector`.
- **[uv](https://docs.astral.sh/uv/)**: installs Python 3.12 and the API dependencies.
- **Node 20+** and npm for the web app.
- Optional: an AWS Bedrock API key for Ask and AI extraction (step 3). Search and the graph work without it.

```bash
git clone https://github.com/jasonli2446/igneous.git && cd igneous
```

### 2. Database and migrations

```bash
make db                                  # docker compose up -d db  (Postgres on :5432, user/password/db: igneous)
cd api && uv sync                        # create api/.venv
uv run alembic upgrade head              # create the schema (or `make migrate` from the repo root)
```

The default connection is `postgresql+psycopg://igneous:igneous@localhost:5432/igneous` (`api/igneous/config.py`).

Every table comes from a migration. After pulling, run `uv run alembic upgrade head` again (once per database); the
API refuses to start on an older schema and prints that command.

Tests (`make test`) never use that database or your personal one: `api/tests/conftest.py` points them at
`igneous_test` on the same server, recreated and migrated on every run.

### 3. Configuration (`api/.env`)

Every API setting is an environment variable with the prefix `IGNEOUS_`; you can put them in `api/.env`
(git-ignored, read when you run commands from `api/`). A minimal file:

```bash
# Which database to use. Omit for the Enron demo DB "igneous". Use a separate DB for your own data (see below).
# IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_personal

# Sources a hosted model may read (Ask, AI extraction, the checks on each link). Empty, the default, sends
# nothing, and Ask stays off. Comma-separated ids from the Sources page: email (the Enron demo) or gmail,ai_chats,takeout
IGNEOUS_LLM_SOURCES=

# Optional
# IGNEOUS_LLM_ENABLED=false             # turn off every hosted model call
# IGNEOUS_FILESYSTEM_ROOTS=~/Documents/notes,~/Desktop   # folders for the "Local files" source
# IGNEOUS_WEB_URL=http://localhost:3000 # where Google sign-in sends you back
# IGNEOUS_DATA_DIR=../data              # OAuth client, encrypted tokens, Enron mailboxes
```

The Bedrock credentials are read from the process environment, not from `api/.env`, so export them in the shell that
starts the API (and the CLI, if you run `igneous pipeline --extract`):

```bash
export AWS_BEARER_TOKEN_BEDROCK=...   # your Bedrock API key
export AWS_REGION=us-east-1           # default if unset
```

Without the key, `/ask` returns 503 and extraction is skipped; everything else works. The default chat model is
`us.anthropic.claude-sonnet-5-5` (`IGNEOUS_LLM_MODEL`), with `IGNEOUS_LLM_CHEAP_MODEL` for high-volume yes/no judgments.
Embeddings and the reranker run locally (FastEmbed), so the first search or pipeline run downloads small model files.
A model reads only the sources in `IGNEOUS_LLM_SOURCES` ([Model calls](#model-calls)); local documents and Apple
Notes also need `IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED=true`.

### 4. Start the API and the web app

```bash
make api    # terminal 1: cd api && uv run uvicorn igneous.main:app --reload   (API on :8000, docs at /docs)
make web    # terminal 2: cd web && npm install && npm run dev                 (app on :3000)
```

Always run the API with `--reload`; otherwise you test stale code. Both Make targets already do. The web app talks to
`http://localhost:8000` unless `NEXT_PUBLIC_API_URL` is set. `docker compose up` runs the API and web in containers
instead (against the `igneous` database).

### 5. Open the Sources page and follow it

Open <http://localhost:3000/settings> (**Sources** in the top bar). Every source is one entry, grouped as
**Connected accounts** (Google, GitHub), **Exports you upload** (ChatGPT, Claude.ai, LinkedIn, Google Takeout, with the
upload or path box inline) and **On this computer** (Messages, Contacts, Claude Code and Codex chats, local files, Apple
Notes). Each entry says what it reads, shows its status and last sync or import, and lists the steps to connect it.
The list comes from `GET /sources`, so the page and the CLI never drift apart. Everything is read-only.

Connect or import at least one source, then press **Rebuild** under "Build the graph". Rebuild runs the same pipeline as
`igneous pipeline`: it reads your sources into people, threads and events, chunks and embeds text, finds topics and
entities, works out which person is you, and merges duplicates. It is incremental, so it is safe to press again after
each sync or import; the first run takes a few minutes (embedding is the slow part). Then open **Graph**, **Ask** and
**Search**.

### Demo data (Enron) or your own data

Two databases keep shareable and private data apart:

| Database | Holds | Select with |
|---|---|---|
| `igneous` | The public Enron email corpus. Safe for demos, evaluation and screenshots. | the default; no setting needed |
| `igneous_personal` | Your real data (Gmail, Calendar, Messages, Contacts, exports). **Never commit, export or demo it.** | `IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_personal` |

Enron demo (one mailbox, about 3k emails, end to end in a few minutes):

```bash
make enron      # download and extract the mailboxes into data/ (~450 MB, once)
make demo       # make migrate, igneous sync email --mailbox allen-p, igneous pipeline --extract 100
```

`make demo` calls the model for up to 100 messages (`EXTRACT=100`); that needs `AWS_BEARER_TOKEN_BEDROCK` and
`IGNEOUS_LLM_SOURCES=email`, which Ask and the evaluation need too. Run `make demo EXTRACT=0` to skip model calls. `make baseline` ingests six mailboxes
(about 26k emails; embedding takes about 45 minutes on CPU, and search falls back to lexical for unembedded chunks
meanwhile). Then try searching `jeff.skilling` or `phillip.allen` on the Graph page.

Your own data goes in a separate database. Create it, migrate it, and point the API and CLI at it:

```bash
docker compose exec db createdb -U igneous igneous_personal
cd api
IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_personal uv run alembic upgrade head
```

Then uncomment `IGNEOUS_DATABASE_URL` in `api/.env` (step 3) and restart the API. The Sources page now shows your own data.

### Command line

Everything on the Sources page has a CLI equivalent (run from `api/`; `uv run igneous --help` lists all commands).
The CLI uses the same `IGNEOUS_DATABASE_URL`, so `igneous sync ...` fills whichever database `api/.env` selects.

```bash
cd api
uv run igneous connect google                       # sign in to a Google account (read-only); repeat to add more
uv run igneous sync gmail --since-days 90           # also: gcal, gcontacts, drive, github, imessage, maccontacts, notes, ai_chats
uv run igneous sync fs --path ~/Documents/notes     # local files (default: IGNEOUS_FILESYSTEM_ROOTS)
uv run igneous sync email --mailbox allen-p         # Enron demo mailbox

uv run igneous import takeout ~/Downloads/takeout-*.zip     # or --downloads to use ~/Downloads/takeout-*.zip
uv run igneous import linkedin ~/Downloads/Basic_LinkedInDataExport.zip   # a zip or an extracted folder
# ChatGPT and Claude.ai exports are uploaded on the Sources page.

uv run igneous pipeline                             # build the graph from whatever is new (same as Rebuild)
uv run igneous pipeline --extract 200               # also LLM-extract up to 200 newest messages (costs money)
uv run igneous schedule install                     # macOS: incremental sync every 30 minutes
```

The Google steps (the OAuth client file) are under [Google](#google-gmail-drive-calendar-contacts) below.

`make synthetic-corpus` writes a small, deterministic fixture corpus to `data/synthetic-cross-source/`: five
invented document-style files plus two ICS calendar events that reuse the public Enron demo persona addresses.
It makes no network or model calls and **does not import** the data; it is input for the future filesystem and
ICS importers. Every output is labeled synthetic and excludes real email text, credentials, and personal data.
Re-run it safely to restore the exact fixture, or validate an existing output with:

```bash
cd api && uv run igneous verify-synthetic-corpus ../data/synthetic-cross-source
```

`igneous pipeline` runs spine -> chunk -> embed -> extract -> topics -> entities -> profile -> threads -> backbone -> self
-> resolve -> organizations -> verify (`api/igneous/pipeline.py`, the same list Rebuild runs). The topics stage lowercases and
conservatively lemmatizes keys, merges only high-similarity local-embedding duplicates, and adds `RELATED_TO` after
three shared messages; moved `ABOUT` edges keep their original provenance. Extraction, like every model call,
reads only sources listed in `IGNEOUS_LLM_SOURCES`; set it to `email` before `make demo`, then use `EXTRACT=0` to
skip calls or `EXTRACT=500` to process more. Everything is incremental: re-running only processes what changed.
`docker compose up` also runs the api and web in containers.

## Layout

| Path | What |
|---|---|
| `api/igneous/connectors/` | `base.py` connector contract + resumable sync runner; one module per source (`gmail.py`, `gcal.py`, `drive.py`, `github.py`, `imessage.py`, `ai_chats.py`, `filesystem.py`, `takeout.py`, `linkedin.py`, ...); `redact.py` strips secrets before anything is stored |
| `api/igneous/graph/` | `spine.py` deterministic graph; `extract.py` model mentions with span provenance; `entities.py`, `threads.py`, `resolve_threads.py`, `profile.py`, `backbone.py` derived projects, life threads and your orgs; `resolve.py` entity resolution; `verify.py` checks every derived link; `traversal.py` what the explorer shows; `invariants.py` `igneous check-graph` |
| `api/igneous/rag/` | `chunking.py`, `embed.py` (local ONNX, 384-d), `search.py` hybrid lexical + dense with RRF, `rerank.py` optional local cross-encoder, `qa.py` graph-augmented answers with citations |
| `api/igneous/llm.py` | structured model calls and the rule for what a model may read (`allowed_sources`, `visible_nodes`); `providers.py` talks to Bedrock |
| `api/eval/` | gold questions, ER labels, harnesses, results (see `api/eval/README.md`) |
| `api/igneous/pipeline.py` | per-stage watermarks; every stage is incremental and safe to re-run |
| `api/igneous/main.py` | FastAPI app: search, ask, the explorer's `/graph/*` routes, sources, sync and import jobs (the full list at <http://localhost:8000/docs>) |
| `api/migrations/` | the whole schema, as plain SQL migrations (`0001_core_schema.py` onward); app code never creates tables |
| `web/` | Next.js: the 3D explorer (`Graph3D.tsx`, 3d-force-graph) with the inspector and why-view, Ask, Search, and the Sources page |

## Your own Messages (iMessage / SMS)

Use the personal database (see Setup). Reads `~/Library/Messages/chat.db` read-only; each conversation-day becomes one
item. The app that runs the API or CLI (Terminal, iTerm, your editor) needs Full Disk Access (System Settings,
Privacy & Security). Press Sync now on the Messages entry, or:

```bash
cd api && uv run igneous sync imessage --since-days 90 && uv run igneous pipeline
```

**Never commit or demo personal data** (proposal CO-5): use the Enron DB for anything shared.

## Adding a stage or connector

- **Connector:** implement `sync(cursor) -> Iterator[SourceItem | Checkpoint]` (see `connectors/base.py`), register it in `cli.py`.
- **Pipeline stage:** read rows newer than `watermark(s, STAGE)`, write, then `advance(...)`. See `rag/chunking.py`.

## Known gaps in the baseline

- Enron has no `In-Reply-To`/`References` headers. Threads are keyed on subject + participants; `REPLIES_TO` is inferred and sparse.
- Enron stores one email in several folders with different Message-IDs. Messages dedupe on a content fingerprint (`raw_meta.fingerprint`).
- Embeddings stay local (`bge-small-en-v1.5`, 384-d, FastEmbed); Bedrock as a second index was dropped
  (`docs/decisions/0002-bedrock-interop.md`).

## Model calls

Q&A uses Claude Sonnet 5.5 on Amazon Bedrock. Set `AWS_BEARER_TOKEN_BEDROCK` and `AWS_REGION` (e.g. `us-east-1`);
application model access goes through `api/igneous/providers.py`: local FastEmbed embeddings, a local FastEmbed
cross-encoder reranker, and the existing Bedrock chat client. Select them with `IGNEOUS_EMBEDDING_PROVIDER`,
`IGNEOUS_RERANK_PROVIDER`, and `IGNEOUS_CHAT_PROVIDER`; the supported defaults are `fastembed`, `cross_encoder`,
and `bedrock`. `IGNEOUS_LLM_ENABLED=false` turns off hosted model calls (search and the graph keep working; `/ask`
returns 503).

**What a model may read.** Only the sources listed in `IGNEOUS_LLM_SOURCES`: the comma-separated ids the Sources
page shows (`gmail,ai_chats`; `takeout` covers every Takeout product, `ai_chats` every AI chat import). Every hosted
call follows one rule (`api/igneous/llm.py`): a prompt carries text only from records in those sources, names only of
people and things that appear in them, and counts for everything else. Local files and Apple Notes also need
`IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED=true`. Search may also send the question itself (never records) to pick out
names and dates. Empty, the default, means no model call at all; Ask then says to opt a source in. Results the model
gave earlier stay cached and keep counting. The Sources page shows, per source, whether
a model may read it.

Q&A reranks the graph-expanded candidate pool down to the top 8 evidence chunks. Set
`IGNEOUS_RERANKER_ENABLED=false` to preserve graph retrieval order, or adjust the bounded input with
`IGNEOUS_RERANKER_POOL_SIZE` (32 by default). If its model files cannot load, Q&A logs a warning and safely
preserves that order instead.

To verify providers without credentials, run `cd api && uv run python scripts/provider_smoke.py`. It exercises the
local embedding and reranking models only. Add `--hosted` only with an explicit real `AWS_BEARER_TOKEN_BEDROCK`; that
mode makes one paid Bedrock chat request and is never run by the test suite.

## MCP server (use the graph from Claude Code, Claude Desktop, Cursor)

Twelve read-only tools: `me` (the big picture), `find` (a name to node ids), `open_node`, `why_connected`, `sources`
and `read_item` for the graph; `search` and `ask` for the text; and the lower-level `get_node`, `neighbors`,
`correspondents` and `evidence`.

```bash
claude mcp add igneous -e AWS_REGION=us-east-1 -e AWS_BEARER_TOKEN_BEDROCK=$AWS_BEARER_TOKEN_BEDROCK \
  -- uv run --directory "$(pwd)/api" igneous mcp
```

The model credentials only matter for `ask`, which reads only the sources in `IGNEOUS_LLM_SOURCES` (`api/.env`, since
the server runs from `api/`). MCP clients don't pass your shell's environment through, so set the credentials with
`-e` as above (other tools work without them). Postgres must be running (`make db`).

## Google (Gmail, Drive, Calendar, Contacts)

1. Put the OAuth desktop client JSON at `data/google/client_secret.json` (git-ignored).
2. `cd api && uv run igneous connect google` opens Google sign-in (read-only scopes). Click **Continue** past
   "Google hasn't verified this app": the project is in Testing mode, so tokens expire after 7 days; re-run it then.
3. `uv run igneous sync drive`, `uv run igneous sync gcal`, `uv run igneous sync gcontacts`,
   `uv run igneous sync gmail --since-days 90`, then `uv run igneous pipeline`. Drive backfills files once,
   then uses the Changes API; Google Docs, Sheets, and Slides export to parser-friendly text formats.

Gmail skips Promotions/Social/Forums by default (`--all-mail` to include them); in a real inbox they were 94% of mail.
**Quota:** this Testing-mode project measured about 730 units/min, so roughly 120 messages/min, far below Google's
published default. The connector paces itself (`IGNEOUS_GMAIL_UNITS_PER_MIN`, default 600), so a large inbox
takes a while but won't error out. Resuming continues mid-page.
