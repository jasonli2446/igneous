# Final demo (~10 min): Igneous, the explorer, and the MCP server

Runs on your own data (`igneous_personal`) or the Enron corpus (`igneous`). The prompts below are written so
they work on any graph that has trips, projects and an owner. Swap in your own names where marked `<...>`.

## Before you start (5 min, once)

1. **Freeze the graph.** Don't press Rebuild or run the pipeline during the demo: a rebuild re-creates
   trips and their members, and a view taken mid-rebuild is half empty. Finish any rebuild first.
2. **Start the stack.**
   ```bash
   make db                          # Postgres
   make api                         # :8000 (runs with --reload)
   make web                         # :3000
   ```
3. **Register the MCP server once** (user scope, so every new Claude Code session has it). Use the venv
   binary, not `uv run`: it starts in about 2 s, so the session's first tool call never races the connection.
   ```bash
   claude mcp add igneous --scope user \
     -e IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_personal \
     -e AWS_REGION=us-east-1 -e AWS_BEARER_TOKEN_BEDROCK="$AWS_BEARER_TOKEN_BEDROCK" \
     -- "$(pwd)/api/.venv/bin/igneous" mcp
   claude mcp list | grep igneous     # -> ✔ Connected
   ```
   Drop `IGNEOUS_DATABASE_URL` to demo on Enron. The Bedrock variables only matter for `ask` and summaries.
4. **Quiet other MCP servers.** Claude Code waits for every configured server at startup. A server that
   times out (for example a stale `figma` entry) shows as one failed "waiting for MCP servers" step at the
   start of the session. Remove or disable it before recording.
5. **Warm it.** Open one Claude Code session and ask "Using igneous, who am I?" once. That fills the big
   picture cache (the first call ranks every relationship, ~10 s; after that, instant).

## 1. What Igneous is (1–1.5 min)

> Everything about your life is already written down: email, texts, calendar, Drive, GitHub, AI chats. But
> none of those apps know about each other. "Who did I work with on that project, and what did we decide?"
> has no single place to ask.
>
> Igneous builds one personal knowledge graph out of all of it, locally. It reads every source read-only,
> works out who's who, and finds the real structure of your life: who you're close to, the group chats
> that matter, and the trips, classes, jobs and projects you've had, placed under the schools and
> workplaces on your resume. Every connection can show you the records it came from.
>
> Three ideas: **everything is inferred** (no categories to set up), **every link has evidence** (an AI
> checker reviews each one against its records), and **you can correct it** (corrections stick through
> every rebuild and teach the checker).

## 2. The explorer (4–5 min)

| Do | Say |
|---|---|
| Open **Sources** | Setup lives in the UI: each source says what it reads and how to connect it. Gmail, Calendar, iMessage, Drive, GitHub, local files, plus exports: ChatGPT, Claude, Gemini (Takeout), LinkedIn. Rebuild runs the whole pipeline. |
| Open **Graph** | It opens on you. The first ring is only what matters most: your schools and workplaces, your sustained projects, and your closest people. Closeness is recency-weighted, so it reflects your life now. One-off emails, newsletters and bots are never nodes. |
| Click your **university** | It opens to the labs and the classes under it. Click a **lab** (e.g. `<xLab>`): its projects. Nobody configured this; it comes from the resume plus which threads involve the school's domain and dates. |
| Click a **trip** (e.g. `<Seattle trip>`) | Who went comes from the trip's own group chat and events, not from everyone who mentioned it. Someone who asked "how was Seattle?" isn't on it. |
| Click a **person** | Their world: the people they're close to, the group chats they're in, projects and events. A single text never shows on its own; it shows as the chat or person it's with. Someone they only know through one project sits under that project. |
| Click a **line** between two nodes | Why they're connected: the kind of link, who made it (a rule, the AI checker, or you), its reason, and the exact records. Open any record. |
| **This is wrong** on a link | It disappears in place with Undo. That's a permanent verdict: never rebuilt, and used as an example for the checker. |
| **+ Connect to…** / **Same person as…** | Add what it missed in plain words, or merge two identities. Stored as yours, never second-guessed. |
| Search a new centre (top bar) | Re-centres on them and opens their connections. The pill top-left says whose graph you're on, one click back. |

## 3. The MCP server (3–4 min)

> The same graph is an MCP server, so any agent can use it as memory: Claude Code, Claude Desktop, Cursor.
> One command to register, read-only by design. The tools are the explorer's own functions, so the agent
> sees exactly what the UI shows.

| Tool | What it does |
|---|---|
| `me` | The big picture: your work/school history and the most meaningful things in your life now |
| `find` | Name to node: people (all addresses/numbers), projects, trips, classes, orgs, chats |
| `open_node` | What something is made of: a trip's travellers and chat, an org's projects, a person's world |
| `why_connected` | Why two things are linked, with the reason and the records behind it |
| `sources`, `read_item` | The records behind a node, and the full text of one |
| `correspondents` | Someone's real relationships, closest first |
| `search` | Keyword + semantic search over every record (skips browsing history unless asked) |
| `ask` | A cited answer from the text, or "can't answer" |

**Does it show its steps?** Yes. In an interactive Claude Code session every tool call shows as a line
(`igneous - open_node (node_id: …)`) with its result collapsed under it; press `ctrl+o` to expand one. That's
the demo: the audience watches it resolve a name, open the node, follow a link, and read the record before it
answers. Run it in the **interactive** session (`claude`), not `claude -p`, so the steps are visible.

### Prompts (open a fresh `claude` session; tested end to end)

Pick 3–4. Each needs no setup beyond registration; each was run in a fresh session.

1. **Overview** (start here):
   > Using igneous, give me a quick overview of my life right now: where I work and study, what I'm working on, and who I'm closest to.

   Expect: `me` → `open_node` → `correspondents`. Lists the work/school history with dates, the current
   projects, and the closest people.

2. **Who went, and how do you know** (shows evidence):
   > Using igneous: who went on my `<trip>`, and how do you know? Show the evidence.

   Expect: `find` → `open_node` (travellers + the trip's group chat) → `why_connected` or `sources` →
   `read_item`. Names the travellers and cites the chat and records.

3. **Structure** (shows the backbone):
   > Using igneous: what's under `<lab or employer>`, and who did I work with on `<project>`?

   Expect: `find` → `open_node` on the org (its projects) → `open_node` on the project (its people).

4. **Explain a link** (shows trust):
   > Using igneous: why is `<person>` connected to `<project>`? Cite the records.

   Expect: `find` ×2 → `why_connected` (the link, its kind, the checker's verdict and reason, the records) →
   `read_item`.

5. **Brief me** (shows synthesis):
   > Using igneous: brief me on `<class or project>`: what it is, who's in it, and the latest activity.

   Expect: `find` → `open_node` → `sources` → `read_item`; a short brief with dates and citations.

6. **Refuses to make things up**:
   > Using igneous: what did `<person>` say about `<something that never happened>`?

   Expect: `ask` returns "can't answer" with no invented citations.

**Timing (measured, fresh sessions):** 50–100 s per prompt, 4–18 tool calls. Prompts 1 and 3 are the
fastest. Prompt 4 can run long if the agent keeps reading records; say "two records is enough" in the prompt
to keep it short.

## 4. Capabilities to mention

- **Sources:** 14+, multiple Google accounts with duplicate detection; phone numbers, emails and contact
  cards fold into one person; your resume and LinkedIn are the backbone.
- **Structure found, not configured:** trips, classes, jobs and projects from activity bursts, course codes,
  repos and recurring events; a persistent registry keeps them stable across rebuilds; classes and projects
  sit under the school or lab they belong to; course codes are whole identifiers (CSDS 395 is not ECSE 395).
- **People done right:** who took part comes from group chats and attendance; people judged relative to a
  thread's core (a passing mention next to people who talk about it constantly is dropped); someone you only
  know through one project sits under it; automated senders and bots are never people.
- **Trust:** an AI checker reviews each derived link against its evidence and keeps what it read; your
  corrections are permanent, undoable and teach it; high-volume checks run on a cheap model, merges and
  naming on a stronger one.
- **Eval (Enron, Oct 18):** hybrid recall@10 0.81 vs 0.41 lexical; cited the gold source 90% (baseline
  77%); abstained on 12/12 unanswerable; p50 2.2 s. Entity resolution P 0.93 / R 0.96.
- **Privacy:** local, read-only, per-source opt-in before any model sees content, secrets encrypted or
  git-ignored. The MCP server is read-only.

## If something goes wrong

| Symptom | Fix |
|---|---|
| First tool call says "waiting for MCP servers" | Another configured server is slow; igneous still connects. Remove the slow one before recording. |
| A trip or org opens with almost nothing | A rebuild is running or just crashed. Let it finish (or re-run `threads`, `backbone`). |
| `ask` returns "ask is unavailable" | The MCP env is missing the Bedrock token; re-run the `claude mcp add` with `-e AWS_BEARER_TOKEN_BEDROCK=...`. |
| `me` takes ~10 s | First call after a graph change; it's cached after that. Warm it before recording. |
