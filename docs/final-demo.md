# Final demo (~10 min): Igneous, the explorer, and the MCP server

Runs on the synthetic persona (`igneous_persona`), a fictional student's two years in every source the Sources page
reads, and on the Enron corpus (`igneous`) for Ask at scale. Never on `igneous_personal`: personal data is never
shown in a demo (CLAUDE.md, CO-5).

## The persona

Mira Castellanos studies computer science at Lakeshore University (fictional, like everything here) from August 2024
to her graduation in May 2026. Her records: a mailbox, a Google Takeout export (chat, tasks, Meet, Maps, My Activity,
Chrome), LinkedIn and ChatGPT exports, and a documents folder with two resumes, class notes and a git checkout.
What is in them, so you know what to click:

- **Classes**, each named by its code: CS 341, ENVS 210 (fall 2024), CS 375, CS 362 (spring 2025), CS 452 with its
  Raft team chat (fall 2025), and the CS 481 capstone, Harborline (spring 2026).
- **The job**: Northwind Robotics, part-time from January 2025, full-time in summer 2025 (the Heron field trials on
  Lake Avalon) and from July 2026. Ruth Adeyemi, her advisor, introduced her to Owen Brandt, the CTO.
- **Two trips**: Lisbon over spring break 2025 with Dana, Sofia and Rafael; Iceland's Ring Road in June 2026 with
  Dana and Theo (Dana introduced Mira to her cousin Ama in Reykjavik).
- **The side project**: Tidepool, beach water-quality alerts, built with Rafael; Priya introduced Mira to Hannah
  Brooks, whose city team piloted it in 2026.
- **People**: Dana writes from a school and a personal address; two different people are named Alex Chen (a CS 452
  teammate and a Northwind firmware engineer).

## Before you start (once, about 20 minutes)

1. **Build the persona.** `make db`, then `make persona`: it generates the exports into `data/persona`, creates the
   `igneous_persona` database, imports every export through its source on the Sources page, and builds the graph
   (embedding is most of the time). `make persona CLEAN=1` rebuilds it from scratch.
2. **Point the API at it** in `api/.env`:
   ```bash
   IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_persona
   IGNEOUS_LLM_SOURCES=email,fs,takeout,linkedin,ai_chats   # the sources the persona came through
   IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED=true                 # its documents (resumes, notes) too
   ```
3. **Let the model read it** (needs `AWS_BEARER_TOKEN_BEDROCK` in the API's shell): start the API and press
   **Rebuild** on the Sources page once (or run `cd api && uv run igneous pipeline`). The model names the threads
   ("Lisbon trip", "CS 452") and says what kind each is, reads the resumes for where Mira studied and worked, and
   checks every derived link. Without a key the graph still builds and every section below works, but threads keep
   their fallback names ("Lisbon · Jan-Mar 2025", kind "thread", all under Projects) and classes are not yet placed
   under the school.
4. **Freeze the graph.** Don't press Rebuild or run the pipeline during the demo: the explorer keeps the previous
   graph while a rebuild runs, then switches to the new one when it publishes, and trips and their members can change
   under you. Finish any rebuild first.
5. **Start the stack.**
   ```bash
   make api                         # :8000 (runs with --reload)
   make web                         # :3000
   ```
6. **Register the MCP server once** (user scope, so every new Claude Code session has it). Use the venv binary, not
   `uv run`: it starts in about 2 s, so the session's first tool call never races the connection.
   ```bash
   claude mcp add igneous --scope user \
     -e IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_persona \
     -e IGNEOUS_LLM_SOURCES=email,fs,takeout,linkedin,ai_chats -e IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED=true \
     -e AWS_REGION=us-east-1 -e AWS_BEARER_TOKEN_BEDROCK="$AWS_BEARER_TOKEN_BEDROCK" \
     -- "$(pwd)/api/.venv/bin/igneous" mcp
   claude mcp list | grep igneous     # -> ✔ Connected
   ```
   The Bedrock variables only matter for `ask` and summaries. For Ask at scale (prompt 7), register a second server,
   `igneous-enron`, the same way with `IGNEOUS_DATABASE_URL` left out (the Enron database `igneous`, built by
   `make demo`) and `IGNEOUS_LLM_SOURCES=email`.
7. **Quiet other MCP servers.** Claude Code waits for every configured server at startup. A server that times out
   (for example a stale `figma` entry) shows as one failed "waiting for MCP servers" step at the start of the session.
   Remove or disable it before recording.
8. **Warm it.** Open one Claude Code session and ask "Using igneous, who am I?" once. That fills the big picture
   cache (the first call ranks every relationship; after that, instant).

## 1. What Igneous is (1–1.5 min)

> Everything about your life is already written down: email, chats, calendar, documents, AI chats, exports from the
> services you use. But none of those apps know about each other. "Who did I work with on that project, and what did
> we decide?" has no single place to ask.
>
> Igneous builds one personal knowledge graph out of all of it, locally. It reads every source read-only, works out
> who's who, and finds the real structure of a life: who you're close to, the group chats that matter, and the
> trips, classes, jobs and projects you've had, placed under the schools and workplaces on your resume. Every
> connection can show you the records it came from.
>
> Today's life is invented: Mira, a student whose two years were generated in the exact formats Google, LinkedIn and
> OpenAI export, so we can show every source without showing anyone's real data.
>
> Three ideas: **everything is inferred** (no categories to set up), **every link has evidence** (an AI checker
> reviews each one against its records), and **you can correct it** (corrections stick through every rebuild and
> teach the checker).

## 2. The explorer (4–5 min)

| Do | Say |
|---|---|
| Open **Sources** | Setup lives in the UI: each source says what it reads and how to connect it. The persona came in through five of them: its mailbox, Google Takeout, LinkedIn, ChatGPT and Local files; the Demo group says how it was made. Rebuild runs the whole pipeline. |
| Open **Graph** | It opens on Mira. The first ring is only what matters most: her school and workplace, her sustained projects and trips, and her closest people. Closeness is recency-weighted, so it reflects her life now. One-off emails, newsletters and bots are never nodes. |
| Click **Lakeshore University** | It opens to her classes: CS 341 through the CS 481 capstone. Nobody configured this; it comes from her resume and LinkedIn plus which threads involve the school's people and dates. |
| Click **Northwind Robotics** | The job: the Heron field trials, and the colleagues she wrote and chatted with. |
| Click the **Lisbon trip** | Who went comes from the trip's own group chat, not from everyone who mentioned it: Dana, Sofia and Rafael. Theo, who never went, isn't on it, and a "remember Lisbon?" message six months later isn't part of it. |
| Click **Dana Kimura** | One person, three identities: her school address, her personal address and her LinkedIn profile. And the two Alex Chens stay two people. |
| Click a **line** between two nodes | Why they're connected: the kind of link, who made it (a rule, the AI checker, or you), its reason, and the exact records. Open any record. |
| **This is wrong** on a link | It disappears in place with Undo. That's a permanent verdict: never rebuilt, and used as an example for the checker. |
| **+ Connect to…** / **Same person as…** | Add what it missed in plain words, or merge two identities. Stored as yours, never second-guessed. |
| Search a new centre (top bar), e.g. **Priya Raman** | Re-centres on her and opens her connections. The pill top-left says whose graph you're on, one click back. |

## 3. The MCP server (3–4 min)

> The same graph is an MCP server, so any agent can use it as memory: Claude Code, Claude Desktop, Cursor. One
> command to register, read-only by design. The tools are the explorer's own functions, so the agent sees exactly what
> the UI shows.

| Tool | What it does |
|---|---|
| `me` | The big picture: work/school history and the most meaningful things in life now |
| `find` | Name to node: people (all addresses), projects, trips, classes, orgs, chats |
| `open_node` | What something is made of: a trip's travellers and chat, an org's projects, a person's world |
| `why_connected` | Why two things are linked, with the reason and the records behind it |
| `sources`, `read_item` | The records behind a node, and the full text of one |
| `correspondents` | Someone's real relationships, closest first |
| `search` | Keyword + semantic search over every record (skips browsing history unless asked) |
| `ask` | A cited answer from the text, or "can't answer" |

**Does it show its steps?** Yes. In an interactive Claude Code session every tool call shows as a line
(`igneous - open_node (node_id: …)`) with its result collapsed under it; press `ctrl+o` to expand one. That's the
demo: the audience watches it resolve a name, open the node, follow a link, and read the record before it answers.
Run it in the **interactive** session (`claude`), not `claude -p`, so the steps are visible.

### Prompts (open a fresh `claude` session)

Pick 3–4. Each needs no setup beyond registration. Speak as Mira: the server's "me" is her.

1. **Overview** (start here):
   > Using igneous, give me a quick overview of my life right now: where I work and study, what I'm working on, and who I'm closest to.

   Expect: `me` → `open_node` → `correspondents`. Lakeshore University and Northwind Robotics with dates, Tidepool and
   the recent trips, and Dana, Rafael and Priya among the closest people.

2. **Who went, and how do you know** (shows evidence):
   > Using igneous: who went on my Lisbon trip, and how do you know? Show the evidence.

   Expect: `find` → `open_node` (travellers + the trip's group chat) → `why_connected` or `sources` → `read_item`.
   Dana, Sofia and Rafael, cited via the "Lisbon spring break" chat and the bookings.

3. **Structure** (shows the backbone):
   > Using igneous: what's under Northwind Robotics, and who did I work with on the Heron field trials?

   Expect: `find` → `open_node` on the org (its projects) → `open_node` on the project (Jonah, Priya, Alex Chen).

4. **Explain a link** (shows trust):
   > Using igneous: why is Dana Kimura connected to the Lisbon trip? Cite the records. Two records is enough.

   Expect: `find` ×2 → `why_connected` (the link, its kind, the checker's verdict and reason, the records) →
   `read_item`.

5. **Brief me** (shows synthesis):
   > Using igneous: brief me on CS 452: what it is, who's in it, and the latest activity.

   Expect: `find` → `open_node` → `sources` → `read_item`: the Raft labs, Jordan and Alex, the 46/50 on Lab 3, the
   final demo on December 10.

6. **Refuses to make things up**:
   > Using igneous: what did Owen Brandt say about the Tokyo office?

   Expect: `find` and `search` come back empty for Tokyo, and it says there's no record of it, with no invented
   citations.

7. **Ask at scale** (the Enron corpus, `igneous-enron`):
   > Using igneous-enron, ask: what did Phillip Allen say about the gas price forecast?

   Expect: `ask` with citations from the Enron mailboxes, or "can't answer" when the records don't say.

Adding "two records is enough" to prompt 4 keeps it short; without it the agent may keep reading. Time each prompt
in a fresh session before recording: times depend on the model and on the warm cache (step 8).

## 4. Capabilities to mention

- **Sources:** 14+, multiple Google accounts with duplicate detection; addresses, LinkedIn profiles and contact
  cards fold into one person; the resume and LinkedIn are the backbone.
- **Structure found, not configured:** trips, classes, jobs and projects from activity bursts, course codes, repos and
  recurring events; a persistent registry keeps them stable across rebuilds; classes and projects sit under the
  school or employer they belong to; course codes are whole identifiers (CS 341 is not ENVS 341).
- **People done right:** who took part comes from group chats and attendance; people judged relative to a thread's
  core (a passing mention next to people who talk about it constantly is dropped); automated senders and bots are
  never people.
- **Trust:** an AI checker reviews each derived link against its evidence and keeps what it read; your corrections are
  permanent, undoable and teach it; high-volume checks run on a cheap model, merges and naming on a stronger one.
- **Eval:** the persona is also a gold set (`api/eval/persona_eval.py`: its threads, the identities that are one
  person, and Ask questions with their records), and the Enron baseline measures Ask and entity resolution at scale
  (`api/eval/README.md`: hybrid recall@10 0.81 vs 0.41 lexical; Ask cited the gold source on 87% of answerable
  questions; entity resolution P 0.95 / R 0.93, never on a name alone).
- **Privacy:** local, read-only, per-source opt-in before any model sees content, secrets encrypted or git-ignored.
  The MCP server is read-only. The demo's data is invented.

## If something goes wrong

| Symptom | Fix |
|---|---|
| `make persona` says the database exists | Use it as it is, or rebuild from scratch: `make persona CLEAN=1`. |
| Sync now on the persona says it never mixes with other records | The API points at another database. Set `IGNEOUS_DATABASE_URL` to `igneous_persona` and restart the API. |
| Threads are named like "Lisbon · Jan-Mar 2025" and all sit under Projects | The model hasn't read the persona: set `AWS_BEARER_TOKEN_BEDROCK` and the opt-ins (step 2), then Rebuild once. |
| First tool call says "waiting for MCP servers" | Another configured server is slow; igneous still connects. Remove the slow one before recording. |
| A trip or org opens with almost nothing | Its records arrived after the last build. Press Rebuild (or run `igneous pipeline`); the graph switches over when it publishes. |
| `ask` returns "ask is unavailable" | The MCP env is missing the Bedrock token; re-run the `claude mcp add` with `-e AWS_BEARER_TOKEN_BEDROCK=...`. |
| `ask` says no source is opted in | The MCP env has no `IGNEOUS_LLM_SOURCES`; re-run the `claude mcp add` with the opt-ins from step 6. |
| `me` takes a few seconds | First call after a graph change; it's cached after that. Warm it before recording. |
