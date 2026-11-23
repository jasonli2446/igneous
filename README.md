# Igneous

A creation layer for personal knowledge graphs: ingest files, email, documents, and calendar into a typed,
provenance-tracked graph in Postgres + pgvector, then search and explore it. Backlog: GitHub Issues.

## Setup

### 1. Prerequisites

- **Docker**: runs Postgres 16 with pgvector (`pgvector/pgvector:pg16`, see `docker-compose.yml`). If you bring your
  own Postgres it must be 16+ with the `vector` and `pg_trgm` extensions available; the migrations create both.
- **[uv](https://docs.astral.sh/uv/)**: installs the API dependencies and Python 3.12, the version `api/.python-version`
  names and CI and the API image run (any Python from 3.12 on works).
- **Node 22** and npm for the web app (CI and the web image run 22; Next 16 needs at least 20.9).
- Optional: an AWS Bedrock API key for Ask and AI extraction (step 3). Search and the graph work without it.

```bash
git clone https://github.com/jasonli2446/igneous.git && cd igneous
```

### 2. Database and migrations

```bash
make db                                  # docker compose up -d --wait db  (Postgres on :5432, user/password/db: igneous)
cd api && uv sync                        # create api/.venv
uv run alembic upgrade head              # create the schema (or `make migrate` from the repo root)
```

The default connection is `postgresql+psycopg://igneous:igneous@localhost:5432/igneous` (`api/igneous/config.py`).

Every table comes from a migration. After pulling, run `uv run alembic upgrade head` again (once per database). The
API, the CLI and the MCP server check the schema first and refuse to run on any other, with the reason and the fix:
a database behind the code, one migrated by newer code, no schema yet, or Postgres not reachable.

Upgrading to migration 0012 from an earlier one (0012 tracks which records each node and edge come from): the next
Rebuild or `igneous pipeline` reads every record into the graph once more, about a minute for the six Enron baseline
mailboxes (26k records), and then deletes what no record supports. Chunks and embeddings are kept.

Tests (`make test`) never use that database or your personal one, nor your `api/.env` or model credentials: each
run creates, migrates and finally drops its own `igneous_test_<pid>` database on the same server
(`api/tests/conftest.py`), so two runs can share a server.
`make check` runs the checks every pull request must pass: ruff and the tests for the API, then `next typegen`, `tsc`,
ESLint and the production build for the web app. CI runs them too, and audits the locked dependencies (`make audit`).
`web/README.md` covers the explorer walkthrough (`npm run walkthrough`), a browser check run against the demo database.

### 3. Configuration (`api/.env`)

Every API setting is an environment variable with the prefix `IGNEOUS_`; you can put them in `api/.env`
(git-ignored; read wherever a command starts, and an environment variable overrides it). `api/.env.example` lists
every setting with its default and what it means, all commented out: `cp api/.env.example api/.env`, then
uncomment what you change. The ones most setups touch:

```bash
# Which database to use. Omit for the Enron demo DB "igneous". Use a separate DB for your own data (see below).
# IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_personal

# Sources a hosted model may read (Ask, AI extraction, the checks on each link). Empty, the default, sends
# nothing, and Ask stays off. Comma-separated ids from the Sources page: email (the Enron demo) or gmail,ai_chats,takeout
IGNEOUS_LLM_SOURCES=

# Optional
# IGNEOUS_LLM_ENABLED=false             # turn off every hosted model call
# IGNEOUS_FILESYSTEM_ROOTS=~/Documents/notes,~/Desktop   # "Local files" folders (default: Documents, Desktop, Downloads, iCloud Drive)
# IGNEOUS_WEB_URL=http://localhost:3000 # where Google sign-in sends you back
# IGNEOUS_DATA_DIR=../data              # OAuth client, token encryption key, Enron mailboxes (relative to api/)
```

The Bedrock credentials are read from the process environment, not from `api/.env`, so export them in the shell that
starts the API (and the CLI, if you run `igneous pipeline --extract`):

```bash
export AWS_BEARER_TOKEN_BEDROCK=...   # your Bedrock API key
export AWS_REGION=us-east-1           # default if unset
```

Without the key, `/ask` returns 503 and extraction is skipped; everything else works. The default chat model is
`us.anthropic.claude-sonnet-5-5` (`IGNEOUS_LLM_MODEL`), with `IGNEOUS_LLM_CHEAP_MODEL` for high-volume yes/no judgments.
Embeddings and the reranker run locally (FastEmbed) for every source, so the first search or pipeline run downloads
small model files into `data/models` (`IGNEOUS_MODEL_CACHE_DIR` moves them). A hosted model reads only the sources in
`IGNEOUS_LLM_SOURCES` ([Model calls](#model-calls)); local documents and Apple Notes also need
`IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED=true` for that, while search indexes them on this computer either way.

### 4. Start the API and the web app

```bash
make api    # terminal 1: cd api && uv run uvicorn igneous.main:app --reload   (API on :8000, docs at /docs)
make web    # terminal 2: cd web && npm install && npm run dev                 (app on :3000)
```

Always run the API with `--reload`; otherwise you test stale code. Both Make targets already do. The web app talks to
`http://localhost:8000` unless `NEXT_PUBLIC_API_URL` is set (`web/.env.example`). `docker compose up` runs the API and
web in containers instead, against the `igneous` database; the API container reads `api/.env` for its other settings.

### 5. Open the Sources page and follow it

Open <http://localhost:3000/settings> (**Sources** in the top bar). Every source is one entry, grouped as
**Connected accounts** (Google, GitHub), **Exports you upload** (ChatGPT, Claude.ai, LinkedIn, Google Takeout, with the
upload or path box inline) and **On this computer** (Messages, Contacts, Claude Code and Codex chats, local files, Apple
Notes). Each entry says what it reads, shows its status and last sync or import, and lists the steps to connect it.
Messages, Contacts and Apple Notes, the scheduled sync, and opening a local file from the reader work on macOS only.
The page shows `GET /sources`, the registry in `api/igneous/sources.py`. That registry also builds each source's
connector, so **Sync now**, `igneous sync` and the scheduled sync read a source the same way. Everything is read-only.

Connect or import at least one source, then press **Rebuild** under "Build the graph". Rebuild runs the same pipeline as
`igneous pipeline` (without model extraction): it reads what is new in your sources into people, conversations and
events, chunks and embeds text, then works out which person is you, merges duplicate people, and finds your projects,
life threads and organizations again, publishing them all at once when it finishes. It is safe to press again after
each sync or import; the first run takes a few minutes (embedding is the slow part). Then open **Graph**, **Ask** and
**Search**.

### Demo data (Enron, the synthetic persona) or your own data

Separate databases keep shareable and private data apart:

| Database | Holds | Select with |
|---|---|---|
| `igneous` | The public Enron email corpus. Safe for demos, evaluation and screenshots. | the default; no setting needed |
| `igneous_persona` | The synthetic persona: one invented life in every export format. Safe for demos, evaluation and screenshots. | `IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_persona` |
| `igneous_personal` | Your real data (Gmail, Calendar, Messages, Contacts, exports). **Never commit, export or demo it.** | `IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_personal` |

Enron demo (one mailbox, about 3k emails, end to end in a few minutes):

```bash
make enron      # download and extract the mailboxes into data/ (~450 MB, once)
make demo       # make migrate, igneous sync email (the allen-p demo mailbox), igneous pipeline --extract 100
```

`make demo` calls the model for up to 100 messages (`EXTRACT=100`); that needs `AWS_BEARER_TOKEN_BEDROCK` and
`IGNEOUS_LLM_SOURCES=email`, which Ask and the evaluation need too. Run `make demo EXTRACT=0` to skip model calls. `make baseline` ingests six mailboxes
(about 26k emails; embedding takes about 45 minutes on CPU, and search falls back to lexical for unembedded chunks
meanwhile). The demo's Graph page opens on Phillip Allen: one mailbox's sent folders name its owner. Six mailboxes
are six people's mail, so nobody is "you" there: search for `jeff.skilling` or `phillip.allen` to start.

Enron is mail only. The synthetic persona has every kind of source, so it shows what mail alone can't: life
threads, the backbone of schools and workplaces, the profile, and evidence across sources. It is Mira Castellanos,
an invented computer science student, from August 2024 to October 2026: a mailbox, a Google Takeout export (chat,
tasks, Meet, Maps, My Activity, Chrome, and a calendar no importer reads yet), LinkedIn and ChatGPT exports, and a
documents folder with resumes, notes and a git checkout (`api/igneous/persona`). Generated from a seed on this
computer, with no network or model calls:

```bash
make persona          # generate data/persona, create igneous_persona, import every export, build the graph
make persona CLEAN=1  # the same, replacing an igneous_persona that exists
```

Each export goes through its own source on the Sources page (the "Synthetic persona" entry in the Demo group says
how), never with raw inserts, and the persona's sync refuses any database other than `igneous_persona` that already
holds records, so it never mixes with Enron or your own data. The first build places no thread under Lakeshore
University or Northwind Robotics (it finds their email domains after placing threads); a Rebuild does. Without a
model, threads are named by their term and month ("Lisbon · Jan-Mar 2025"); with `AWS_BEARER_TOKEN_BEDROCK` set and
its sources opted in (`IGNEOUS_LLM_SOURCES=email,fs,takeout,linkedin,ai_chats`,
`IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED=true`), the Rebuild also names them and checks where each was placed. Its gold
set (threads, the identities that are one person, Ask questions) makes it an eval too: `api/eval/README.md`.
`docs/final-demo.md` runs on it.

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
uv run igneous connect github < token.txt           # a read-only GitHub token (or type it at the hidden prompt)
uv run igneous disconnect google you@gmail.com      # forget an account's token (also github LOGIN); its items stay
uv run igneous sync gmail                           # every connected account; `igneous sync --help` lists the sources
uv run igneous sync fs                              # local files: the IGNEOUS_FILESYSTEM_ROOTS folders
uv run igneous sync fs --path ~/Documents/notes     # just this folder; other folders' files are left alone
uv run igneous sync email                           # Enron demo mailbox allen-p (--mailbox to pick others)
uv run igneous persona generate ../data/persona     # the synthetic persona's exports; make persona does it all
uv run igneous sync persona                         # import them, only into igneous_persona or an empty database

uv run igneous import takeout ~/Downloads/takeout-*.zip     # or --downloads to use ~/Downloads/takeout-*.zip
uv run igneous import linkedin ~/Downloads/Basic_LinkedInDataExport.zip   # a zip or an extracted folder
uv run igneous import ai-export ~/Downloads/chatgpt-export.zip            # ChatGPT or Claude.ai, zip or JSON

uv run igneous pipeline                             # read what is new and rebuild the graph (same as Rebuild)
uv run igneous pipeline --extract 200               # also LLM-extract up to 200 newest messages (costs money)
uv run igneous pipeline --stage resolve             # only some stages (in pipeline order); --help lists them
uv run igneous schedule install                     # macOS: incremental sync every 30 minutes
```

The Google steps (the OAuth client file) are under [Google](#google-gmail-drive-calendar-contacts) below.

`make synthetic-corpus` writes a small, deterministic fixture corpus to `data/synthetic-cross-source/`: five
invented document-style files plus two ICS calendar events that reuse the public Enron demo persona addresses, so
they link to the Enron graph (the synthetic persona above is a separate, whole life with its own people).
It makes no network or model calls and **does not import** the data. The Local files source reads the documents
(`igneous sync fs --path ../data/synthetic-cross-source`); no source reads ICS calendars. Every output is labeled
synthetic and excludes real email text, credentials, and personal data.
Re-run it safely to restore the exact fixture, or validate an existing output with:

```bash
cd api && uv run igneous verify-synthetic-corpus ../data/synthetic-cross-source
```

`igneous pipeline` runs redact -> spine -> chunk -> embed -> extract -> self -> resolve -> thread_flags -> introductions
-> topics -> entities -> profile -> organizations -> threads -> backbone -> verify -> corrections
(`api/igneous/pipeline.py`, the same list Rebuild runs, without extract). The order is what each stage reads: redact
applies the current credential rules to items stored under older ones before anything reads them, who you are and
which identities are one person come before everything that counts people, your resume and the organizations your mail
shows you at before the threads placed under them, the verifier after every link it checks, and your corrections last,
so they always win. The source stages
(redact through extract) only read what is new; the stages after them recompute what is derived. The topics stage
lowercases and conservatively lemmatizes keys, merges only high-similarity local-embedding duplicates, and adds
`RELATED_TO` after three shared messages; moved `ABOUT` edges keep their original provenance. Extraction, like every
model call, reads only sources listed in `IGNEOUS_LLM_SOURCES`; set it to `email` before `make demo`, then use
`EXTRACT=0` to skip calls or `EXTRACT=500` to process more. Every chunk opens with its record's title (an email's
subject, a file's name, a conversation's name). When the chunk layout changes (`LAYOUT` in
`api/igneous/rag/chunking.py`), each item is chunked again once and only chunks whose text changed are embedded again.
`docker compose up` also runs the api and web in containers.

## Layout

| Path | What |
|---|---|
| `api/igneous/connectors/` | `base.py` connector contract + resumable sync runner, which stores every item's title, text and metadata text with the credentials `redact.py` matches replaced by `[REDACTED:<kind>]` (and `redact_stored`, the pipeline's first step, which brings items stored under an older version of those rules up to date); one module per source (`gmail.py`, `gcal.py`, `drive.py`, `github.py`, `imessage.py`, `ai_chats.py`, `filesystem.py`, `takeout.py`, `linkedin.py`, ...) |
| `api/igneous/graph/` | `spine.py` the graph read from record metadata, recording which records support each node and edge and what each says about it; `extract.py` model mentions with span provenance; `entities.py`, `threads.py`, `resolve_threads.py`, `profile.py`, `backbone.py` derived projects, life threads and your orgs; `resolve.py` entity resolution; `verify.py` checks every derived link; `traversal.py` what the explorer shows; `invariants.py` `igneous check-graph` |
| `api/igneous/rag/` | `chunking.py`, `embed.py` (local ONNX, 384-d), `search.py` hybrid lexical + dense with RRF, `constraints.py` the filters a question states, `rerank.py` optional local cross-encoder, `qa.py` graph-augmented answers with citations |
| `api/igneous/llm.py` | structured model calls and the rule for what a model may read (`allowed_sources`, `visible_nodes`); `providers.py` talks to Bedrock |
| `api/igneous/persona/` | the synthetic persona: the cast and its storylines (`school.py`, `work.py`, `trips.py`, `project.py`, `everyday.py`, `routine.py`), the export formats (`formats.py`), loading it into its own database (`load.py`), and its gold set (`gold/`) |
| `api/eval/` | gold questions, ER labels, harnesses, results (see `api/eval/README.md`); `persona_eval.py` scores the persona's gold set |
| `api/igneous/pipeline.py` | the stage list in dependency order and the one runner `igneous pipeline` and Rebuild share; spine and chunk read only the records whose content changed since they read them, so every stage is safe to re-run |
| `api/igneous/main.py` | FastAPI app: search, ask, the explorer's `/graph/*` routes, sources, sync and import jobs (the full list at <http://localhost:8000/docs>) |
| `api/migrations/` | the whole schema, as plain SQL migrations (`0001_core_schema.py` onward); app code never creates tables |
| `web/` | Next.js: the 3D explorer (`Graph3D.tsx`, 3d-force-graph) with the inspector and why-view, Ask, Search, and the Sources page |

## Your own Messages (iMessage / SMS)

Use the personal database (see Setup). Reads `~/Library/Messages/chat.db` read-only; each conversation-day becomes one
item. The app that runs the API or CLI (Terminal, iTerm, your editor) needs Full Disk Access (System Settings,
Privacy & Security). Press Sync now on the Messages entry, or run the same sync from the command line (the first sync
reads the last 90 days; `--since-days` changes that):

```bash
cd api && uv run igneous sync imessage && uv run igneous pipeline
```

**Never commit or demo personal data** (proposal CO-5): use the Enron DB for anything shared.

## Adding a stage or connector

- **Connector:** a class whose `sync(cursor)` yields `SourceItem`s, a `Checkpoint` whenever what came before may be
  committed, and a `Tombstone` for each item a change feed reports removed, or a final `CompletedScan` (with the
  scopes it listed) when it listed everything (`connectors/base.py`; `run_sync` redacts and stores what it yields).
  Then add its entry to `SOURCES` in `api/igneous/sources.py`, with the `build` that makes it (the CLI, the Sources
  page and the scheduler all use that one) and the steps to set it up; each value it stores in
  `source_items.source` to `source_catalog.py`; and how the spine turns its items into nodes (`graph/spine.py`'s
  `BUILDERS`; a builder that builds nothing is fine). `igneous check-graph` reports a stored value the catalog
  doesn't know.
- **Pipeline stage:** add it to `STAGES` in `api/igneous/pipeline.py`, after every stage whose output it reads, with
  the label the Sources page shows while it runs. A source stage (redact, spine, chunk, embed, extract) reads only what
  is new and commits as it goes: it finds its own work and marks it done at the version it read, in the transaction
  that writes the result, never by a timestamp (a sync can commit records stamped before the last run). See
  `rag/chunking.py`: it reads records whose `chunked_hash` differs from `content_hash` and writes back the hash it read.
  A derived stage runs inside the publishing transaction: it never commits, rolls back or bumps the graph version,
  rebuilds only the rows it owns (its extractor), stores paid model results with `llm.cache_store` as they arrive, and
  leaves corrections to the last step. `igneous pipeline --stage NAME` runs it alone, then publishes. The spine's edges
  (`spine`, `contacts`, `github`, `linkedin`) list their records in `spine_edge_support`. One rule says what keeps a
  node (`node_held`, migration 0012): the owner, a record that supports it, an edge of yours, and, for anything but a
  message, thread, event, file, folder or place, an edge with a record behind it (its `source_item_id`, its
  `edge_evidence`, or the spine's support). So set `source_item_id` or `edge_evidence` on what your stage writes from a
  record: when the record is deleted, its edges that no other record backs go, and so do the nodes that pointed at it
  or that only those edges held (a commitment it states, a topic only it is about). On a node the spine built, write
  only props it doesn't own (`SPINE_PROPS` in `graph/spine.py`): it recomputes its own from the records that remain.

## Known gaps in the baseline

- Enron has no `In-Reply-To`/`References` headers. Threads are keyed on subject + participants; `REPLIES_TO` comes from
  quoted "Original Message" headers that name exactly one message, else is inferred within the thread.
- Enron stores one email in several folders with different Message-IDs. Messages dedupe on a content fingerprint
  (`raw_meta.fingerprint`); the message stays while any copy remains.
- In a mailbox, assistants send some of the owner's mail. The owner is whoever sent most of its sent folders, so an
  executive's mailbox that an assistant ran (lay-k, skilling-j) would name the assistant if imported alone.
- Embeddings are made on this computer (`bge-small-en-v1.5`, 384-d, FastEmbed; `docs/decisions/0001-local-embeddings.md`);
  Bedrock as a second index was dropped (`docs/decisions/0002-bedrock-interop.md`). Each chunk records the model behind its vector: setting
  `IGNEOUS_EMBEDDING_MODEL` to another 384-dimensional model re-embeds every chunk on the next pipeline run, and search
  compares only vectors from the configured model meanwhile; another dimension needs a migration.

## Model calls

Q&A uses Claude Sonnet 5.5 on Amazon Bedrock. Set `AWS_BEARER_TOKEN_BEDROCK` and `AWS_REGION` (e.g. `us-east-1`).
All model access goes through `api/igneous/providers.py`: local FastEmbed embeddings and a local FastEmbed
cross-encoder reranker, which send nothing anywhere, and Claude on Bedrock for chat (`IGNEOUS_LLM_MODEL`, and
`IGNEOUS_LLM_CHEAP_MODEL` for high-volume checks). The Anthropic SDK retries a failed call `IGNEOUS_HOSTED_RETRIES`
times (2 by default). `IGNEOUS_LLM_ENABLED=false` turns off hosted model calls (search and the graph keep working;
`/ask` returns 503).

**What a model may read.** Only the sources listed in `IGNEOUS_LLM_SOURCES`: the comma-separated ids the Sources
page shows (`gmail,ai_chats`; `takeout` covers every Takeout product, `ai_chats` every AI chat import). Every hosted
call follows one rule (`api/igneous/llm.py`): a prompt carries text only from records in those sources, names only of
people and things that appear in them, and counts for everything else. Local files and Apple Notes also need
`IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED=true`. Search and Ask may also send the question itself (never a record) to the
cheap model to pick out who, where and when (below). Empty, the default, means no model call at all; Ask then says to
opt a source in. Results the model gave earlier stay cached and keep counting. The Sources page shows, per source,
whether a model may read it.

**Filters in a question.** Search and Ask read filters out of the question (`api/igneous/rag/constraints.py`):
"from", "to", "with" or "by" a person means messages they sent or received (any of their merged addresses);
"regarding", "involving", "mentioning" or "concerning" a person means records that name them; "in my calendar", "my
texts", "in my email" or "my files" limits the sources (a source word only counts when it says where to look, so
"the events that led to the crisis" limits nothing); and "in May 2001", "since 2000" or "on May 3, 2001" limits the
dates. When the rules find none and a source is opted in, the cheap model reads the question once (cached). Filters a
request sets itself win. Both pages show the filters they applied and mark the ones read from the question. Search
also takes `-word` or `-"a phrase"` to leave matches out.

**How Ask picks its evidence** (`api/igneous/rag/qa.py`). Hybrid search retrieves a pool of
`IGNEOUS_RERANKER_POOL_SIZE` passages (32 by default), graph expansion adds each hit's direct replies and the
messages nearest to it in time in its thread, and the local cross-encoder scores hits and neighbours together and
keeps the best 8. With `IGNEOUS_RERANKER_ENABLED=false`, or when its model files can't load (Q&A logs a warning),
the 8 follow retrieval order but 3 places go to graph neighbours of the top hits, so a reply that answers without
the question's words still reaches the model. An answer cites its evidence (chunk, source item and node ids); when
the model can't answer from it, Ask says so and cites nothing. A failed model call is a 502 and an unavailable
model a 503, each with a message.

To verify providers without credentials, run `cd api && uv run python scripts/provider_smoke.py`. It exercises the
local embedding and reranking models only. Add `--hosted` only with an explicit real `AWS_BEARER_TOKEN_BEDROCK`: it
then sends one paid, schema-constrained request to each hosted model (`IGNEOUS_LLM_MODEL` and
`IGNEOUS_LLM_CHEAP_MODEL`), the way every model call goes, so a model that rejects the request fails there first. The
test suite never runs it.

## MCP server (use the graph from Claude Code, Claude Desktop, Cursor)

Fourteen read-only tools: `me` (the big picture), `find` (a name to node ids), `open_node`, `why_connected`,
`timeline`, `commitments`, `sources` and `read_item` for the graph; `search` and `ask` for the text; and the
lower-level `get_node`, `neighbors`, `correspondents` and `evidence`.

`timeline(node_id, other_node_id=None, limit=50)` returns dated activity or a pair's direct relationship history,
with source item ids and no source text. A record named by its title (a message, an event, a conversation named after
its subject) keeps its name only when its source is in `IGNEOUS_LLM_SOURCES`, since tool results reach the client's
model. `commitments(node_id, status="open", limit=50)` returns existing promises
from that node's records, with who/what, status and source spans. For a person it includes messages they sent or
received; check `who` before attributing a promise. Both resolve merged identities, accept limits from 1 to 200,
and work without model credentials. Commitments also accepts `resolved` or `all` for status.

```bash
claude mcp add igneous -e AWS_REGION=us-east-1 -e AWS_BEARER_TOKEN_BEDROCK=$AWS_BEARER_TOKEN_BEDROCK \
  -- uv run --directory "$(pwd)/api" igneous mcp
```

The model credentials only matter for `ask`, which reads only the sources in `IGNEOUS_LLM_SOURCES` (`api/.env`).
MCP clients don't pass your shell's environment through, so set the credentials with `-e` as above (other tools work
without them). Postgres must be running (`make db`).

## Google (Gmail, Drive, Calendar, Contacts)

Igneous signs in to Google through an OAuth client in a Google Cloud project of your own. Create it once:

1. In the [Google Cloud console](https://console.cloud.google.com/) create a project.
2. Under **APIs & Services > Library** enable the Gmail API, Google Drive API, Google Calendar API and People API.
3. Under **APIs & Services > OAuth consent screen** choose **External**, give the app a name and your email, and add
   each Google account you will connect as a **test user**. Leave the app in Testing.
4. Under **APIs & Services > Credentials** create an **OAuth client ID** of type **Desktop app** and download its JSON.
5. Save it as `data/google/client_secret.json` (git-ignored). The Sources page says when it finds the file.

Then connect each account: **Connect a Google account** on the Sources page (Google sends the browser back to the API at
`http://localhost:8000/sources/google/callback`, which a Desktop client allows), or `cd api && uv run igneous connect
google` (it listens on a free local port; `--no-browser` prints the URL instead). Igneous asks only for read-only access
to Gmail, Drive, Calendar and Contacts, plus your email address. Click **Continue** past "Google hasn't verified this
app". In Testing mode Google expires the tokens after 7 days: connect again when a sync says the token expired.
Disconnecting an account (under Gmail on the Sources page, or `igneous disconnect google you@gmail.com`) forgets its
token; what it synced stays.

Sync with **Sync now** on each entry, or `uv run igneous sync gmail` (and `gcal`, `gcontacts`, `drive`), then
`uv run igneous pipeline`. Drive lists your files once (an interrupted listing continues where it stopped), then
follows the Changes API. Google Docs and Slides export as text and Sheets as a workbook, every sheet; other files are
downloaded only when they are a readable format of 20 MB or less (`MAX_DOCUMENT_BYTES` in `parsers.py`).

Gmail's first sync reads the last 90 days (`--since-days` sets another window). It skips spam, drafts, Promotions,
Social and Forums; `igneous sync gmail --all-mail` keeps the last three for that account, which remembers the choice
(`--no-all-mail` switches back and removes them). Mail moved to Spam or Trash, or deleted, goes at the next sync.
**Quota:** a Google Cloud project in Testing mode is cut off near 730 Gmail units/min (about 120 messages/min), far
below Google's published 15,000. The connector paces itself to `IGNEOUS_GMAIL_UNITS_PER_MIN` (default 600; raise it in
`api/.env` if your project's quota page allows more), so a large inbox takes a while but won't error out. Resuming
continues mid-page.
