# Igneous

A creation layer for personal knowledge graphs (CSDS 395). Python/FastAPI + Postgres/pgvector in `api/`,
Next.js 16 + 3d-force-graph in `web/`. Setup and layout: `README.md`. Plan: epic #84.

## GitHub is the source of truth for work: keep it current as you go

Two people push to this repo in parallel (Jason and Aadhav). Unsynced work has already caused overlap, so:

**Before starting anything**
1. `git fetch --all` and read what landed: `git log --oneline HEAD..origin/main` and
   `gh pr list --state all --limit 15`. Rebase onto `origin/main` before building on stale code.
2. Find the issue for the work: `gh issue list --state open`. If none exists, **create one first**, with
   labels (`area:*`, `phase:*`, `P1`/`P2`), a milestone, a task checklist and a "Done when" line, linked as a
   sub-issue of its epic (`gh api -X POST repos/jasonli2446/igneous/issues/<epic>/sub_issues -F sub_issue_id=<id>`).
3. If the issue is assigned to someone else, or another open PR touches the same files, stop and say so
   before writing code.

**While working**
- Work on a branch named for the issue, never directly on `main`.
- When you finish a task-list item, tick it in the issue body (`gh issue edit`).
- When you find something that changes the plan (a bug in shared code, a design problem, a decision for the
  user), comment on the issue. If it's new work, open a new issue instead of burying it.
- Commit messages reference the issue (`... (#85)`).

**When finishing**
- Open a PR (`gh pr create`) that says what changed, what you verified and how, and anything the other
  person needs to know (shared-code changes, data migrations, removed or unrendered features). Use
  `Closes #N` only for issues the PR fully completes.
- Close an issue only after verifying its "Done when" criterion, with a comment stating what was checked.
  Partial work gets a progress comment and stays open.
- Never close or edit an issue someone else owns without saying so in the issue.

## Data and privacy (proposal constraints CO-1, CO-2, CO-5)

- **Two databases.** `igneous` is the Enron demo corpus: shareable, used for demos and eval.
  `igneous_personal` holds real personal data (Gmail, Calendar, iMessage, Contacts) and must **never** be
  committed, exported, pasted into issues or PRs, or shown in a demo. Select it with
  `IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_personal`.
- When inspecting personal data, print counts and shapes, not message contents or names.
- Every source is **read-only**. Model calls go through `api/igneous/llm.py` only.
- Secrets (OAuth client JSON, tokens) live in git-ignored `data/` or encrypted in Postgres. Never in the repo,
  issues, PRs or logs.

## Gotchas already paid for

- The API server must run with `--reload`, or verification hits stale code.
- A new `@theme` block in `globals.css` needs a dev-server restart (`rm -rf web/.next`).
- Don't statically import `components/Graph3D.tsx` from a page: it pulls three.js into SSR
  ("window is not defined"). Shared constants live in `web/src/lib/graph-style.ts`.
- `web/AGENTS.md`: Next.js 16 differs from training data; read `node_modules/next/dist/docs/` first.
- The shell is zsh: no `declare -A`, no `read -a`. Use Python for loops over structured data.
- psql doesn't substitute `-v` variables inside `-c`; pass SQL on stdin.
- Gmail quota on this Testing-mode Google project is about 730 units/min. The connector paces itself.
- Changing a connector's `external_id` format duplicates existing rows on resync. Ship a migration with it
  (see PR #83 for the gcal case).

## Checks before any PR

`cd api && uv run ruff check . && uv run pytest -q` and `cd web && npx next typegen && npx tsc --noEmit && npm run build`.
For UI changes, verify in the browser with `playwright-cli`.
