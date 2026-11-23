# Handoff: finishing Igneous

Read this first, then `CLAUDE.md` and `README.md`. GitHub issues are the source of truth for the work itself. This file
covers what the issues don't: the order to take them in, how to stand up a working environment, conventions the
recent work settled on, and what only the owner can provide. Delete this file in the pull request that finishes the
last item.

## How the owner wants the work done

- Build toward the final state of each thing. Fix a problem at its cause, never with a workaround that a later change
  would have to undo, and don't build what the end state doesn't need.
- A change isn't done until every place it affects is updated: callers, the CLI, the API, the MCP server, the web
  client, the Sources page and `GET /sources`, `api/.env.example`, docs, tests and check-graph invariants.
- Docs and comments describe the code as it is, not its history.
- Don't defer work an issue already lays out, or work that follows from it. Don't wait for approval: open a pull
  request, verify it, merge it (squash), and close the issue with what you checked. CLAUDE.md has the process.
- When something only the owner can provide blocks you, say exactly what you need and how they can give it, and carry
  on with everything else.

## State of main

The schema head is migration `0014`. The pull requests merged since #199 reshaped most of the code. Read their
descriptions before changing an area (`git log --oneline` from #199 on). The ones that set the contracts you'll work
within:

| Area | Pull request | Contract |
|---|---|---|
| Pipeline | #212 | `pipeline.STAGES` / `run`. Source stages commit as they go. Derived stages publish in one transaction (`publishing`). |
| Connectors | #214 | One registry (`sources.SOURCES`) for the CLI, the Sources page and the scheduler. Every item is redacted on storage under versioned rules (`redact` stage). |
| Spine | #223 | Per-record change markers. Provenance (`spine_node_support` / `spine_edge_support`). A deleted record takes only what it contributed (`node_held`). |
| Explorer API | #224 | `access.py` origin/host guard. Every reader uses `graph/identity.py` and `graph/visibility.py`. The empty graph is a 200. Edges carry `origin` / `stated`. |
| Ask | #215, #226 | `qa.gather` is exactly what the model sees. Evidence choice was measured (`api/eval/README.md`). |
| Corrections | #229 | Corrections are intents (`graph/corrections.py`, `graph/derived.py`). The thread registry has a lifecycle. Direct evidence settles a link. Suggestions. |
| Persona | #233 | `make persona` builds a fictional person's graph through the real importers, with gold in `api/igneous/persona/gold`. |

Two databases exist only on a machine where they were built: `igneous` (the Enron allen-p demo) and
`igneous_baseline` (six Enron mailboxes). Rebuild them with the commands below. Never put personal data in either.

## Order of work

1. **Environment, then the model key** (sections below). Everything after step 6 that measures answers needs the key.
2. **Triage the old open issues against main.** Many were finished by the work above but never closed. For each, check
   its "Done when" on main. Close it with what you checked, or comment with exactly what remains and do that.
   - Likely done or nearly done: #101, #125, #129, #130, #141, #147, #148, #156, #157, #172.
   - Partly done: #99, #128, #174.
   - #147's scorecard needs the owner's personal gold file (see the end of this file).
3. **#210, the MCP server.** Same privacy rules as every model path, validated tools, `igneous mcp install` / `check`.
   It also completes #174, and parts of #99.
4. **#241, the web client.** Use the correction and suggestion contract from #229; finish the Ask, Search and Sources
   pages.
5. **What the persona surfaced.** These block #222's "Done when" (the persona's gold hierarchy, no duplicates):
   - #234, backbone placement ignores thread kind;
   - #227, an organization keyed two ways;
   - #237, entities duplicate the threads stage's projects;
   - #239, one class becomes two threads;
   - #238, one role per organization;
   - #228, replies split from their root's thread; the cause is in that issue's comments;
   - #235, Takeout chat names and Calendar;
   - #236, contacts from a vCard export;
   - #240, search results vary between builds.

   Then close #222 with `eval/persona_eval.py` showing every gold thread found and placed, with no duplicates.
6. **Identity and names.** #231 (the owner's second address) and #232 (quoted X-From names).
7. **Issues the former teammates held.** They have finished with the project, so these are yours. Reassign or unassign
   with a comment.
   - #100, the write path: `POST /facts`, and MCP `remember` / `link` behind `IGNEOUS_MCP_WRITE`. Store facts as #229's
     intents.
   - #92, the research agent: reuse #210's policy filter for everything it reads.
   - Then #193, #94, #170, #154, #131 and #59, the scale test. #59 needs the baseline database.
8. **With the key** (see the next section):
   - #190: re-measure Ask on the baseline, run `api/eval/run_eval.py --configs full,graph,baseline`. Also decide the
     reranker question #226 left open: compare `cited_gold` for full vs. graph.
   - Pull request #195 (#191, LightRAG export of the derived graph) was left open by its author. Merge main into it,
     do the live check it was waiting for, then merge or supersede it.
   - Build the persona with the model, opted in to its sources, and check thread names and the verifier on it.
9. **#242, the docs pass**, once the code has settled: the README with the architecture, one demo document, and every
   claim checked.
10. **Course deliverables:** #60–#65 (demo environment, report, poster, presentation) and #30 (human entity-resolution
    labels). These need the owner's input, listed at the end of this file.

## The model key (Amazon Bedrock)

Hosted calls go through `api/igneous/providers.py`, which needs `AWS_BEARER_TOKEN_BEDROCK` (and `AWS_REGION`,
default `us-east-1`). Look for an existing key before asking:

1. The process environment: `env | grep -c AWS_BEARER_TOKEN_BEDROCK`. Never print the value.
2. `api/.env` and `data/`, both git-ignored.
3. The session's credential store. In a cloud session with a credential tool, request a key for
   `bedrock-runtime.us-east-1.amazonaws.com`. The SDK refuses to start without the variable, so set it to a
   placeholder and let the proxy supply the real key.
4. Otherwise, ask the owner. They have said they will get one from the team.

Nothing in the repo or in a pull request may contain the key. A demo that needs the model opts in only the sources
it shows (`IGNEOUS_LLM_SOURCES`).

## Environment

In a cloud container without Docker:

```bash
sudo apt-get install -y postgresql-16 postgresql-16-pgvector
sudo pg_ctlcluster 16 main start
sudo -u postgres psql -c "CREATE ROLE igneous LOGIN SUPERUSER PASSWORD 'igneous'"
sudo -u postgres createdb -O igneous igneous
cd api && uv sync --locked && cd ../web && npm ci && cd ..
```

The tests create and drop their own databases, so the role needs to be able to. With Docker, `make db` replaces the
Postgres lines.

Databases to build:

| Database | Commands | Time |
|---|---|---|
| `igneous`, the allen-p demo | `make enron` (downloads about 450 MB once), then `make demo EXTRACT=0` | about 5 min |
| `igneous_persona` | `make persona CLEAN=1` | about 3 min |
| `igneous_baseline`, six mailboxes | see below | about 1 hour, nearly all embedding on CPU |

For the baseline, run `make enron` first, then:

```bash
createdb -h localhost -U igneous igneous_baseline
IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_baseline make baseline EXTRACT=0
```

`make check` runs everything CI runs, and `make audit` runs the dependency audit.

## Conventions the recent work settled on

- **Migrations on parallel branches.** Branches written at the same time all start from the same head. Whichever
  merges second renumbers its migration: file name, `revision`, `down_revision` and docstring.
  `tests/test_migrations.py` checks one head and a full downgrade/upgrade round trip.
- **CLI output in CI.** Typer forces a Rich terminal under GitHub Actions, so a test that matches CLI output compares
  `click.unstyle(output)`. Run the suite once with `GITHUB_ACTIONS=true` before pushing.
- **Stale Next types.** After removing or renaming a Next.js route, `rm -rf web/.next` before `tsc`. Stale
  `.next/dev/types` otherwise fail the type check.
- **Parallel agents.** Give each agent its own git worktree, its own database copy
  (`pg_dump igneous | psql igneous_<name>`), and its own API and web ports. Nothing but the person building them
  writes `igneous` or `igneous_baseline`.
- **Threads in tests.** Code that commits from other threads or processes gets the `committed_db` fixture, not `db`.
  `test_imports.py::test_an_ai_export_upload_imports_in_the_background_and_its_copy_goes` failed once under heavy
  machine load (the job row was gone while the test polled it) and never again in many runs. If it recurs, find out
  why.
- **Measuring.** `api/eval/run_eval.py --no-qa --pools …` measures Ask's evidence with no model call. Record the
  conditions (machine load) next to any timing.

## What only the owner can provide

- The Bedrock key, if step 4 of "The model key" finds none.
- For #63–#65 (report, poster, presentation): the course's requirements and the project proposal. The proposal also
  defines the codes the docs cite (CO-1, CO-2, CO-5, FD-1, criteria 1–6). Also confirm whether #61 (the progress video
  due 22 October) was delivered, so it can be closed.
- #30: human labels for the entity-resolution pairs, or a decision to stand on the AI-reviewed labels
  (`api/eval/README.md` explains them).
- #147's scorecard: running `api/eval/threads_eval.py` against the personal database with the owner's local gold file.
- Live checks that need the owner's accounts or a Mac:
  - a fresh Google connection (Gmail, Drive, Calendar, Contacts) through the Sources page, as #101's "Done when"
    asks;
  - the macOS-only sources (Messages, Contacts, Notes) and the scheduled sync.
