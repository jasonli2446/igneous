# Demo script (progress report video, ~10 min)

Everything below has been run against the baseline corpus (6 Enron mailboxes, 26k emails -> 13.9k unique
messages). Each step names the thing it proves, so the narration maps to the success criteria.

## Setup (before recording)

```bash
# api/.env needs IGNEOUS_LLM_SOURCES=email: Ask, extraction and the LightRAG export read only opted-in sources
make enron && make baseline EXTRACT=500     # ingest, graph, embed (~45 min), extract 500, resolve
make api    # terminal 1
make web    # terminal 2 -> http://localhost:3000
```

Have a terminal ready in `api/` for the CLI parts.

## 1. The problem and the idea (1 min)

Slide: email, files, calendar each search only their own silo; the questions people ask are relational
(who, what led to it, when). Igneous is the **creation layer**: it builds the graph; any engine can query it.

## 2. Ingestion is incremental and idempotent (1 min) — criterion 1

```bash
uv run igneous sync email --mailbox allen-p     # -> inserted 0, updated 0 (cursor says nothing new)
psql ... -c "DELETE FROM sync_state"; uv run igneous sync email --mailbox allen-p   # -> all "unchanged"
```

Say: Enron stores each email in several folders with different IDs. 47% were copies; content fingerprints
collapse them (26,259 files -> 13,902 messages).

## 3. The graph, and entity resolution (2 min) — criteria 2, 3, 6

Explorer -> search `kenneth lay` -> **one** result, "Kenneth Lay · 9 aliases". Click it.

- People view: his real network. Lay, Skilling, Whalley, Frevert, Lavorato, Fastow. Edge width = messages.
- Inspector: SAME_AS 5. Say: Ken Lay appeared under 10 identities (ken.lay@, kenneth_lay@enron.net, …);
  resolution folded them into one. It refuses to merge on name alone (seven different "David"s), and
  it won't let a bare `taylor@` address bridge Liz Taylor and Larry Taylor.
- Double-click Jeff Skilling to expand.

## 4. Evidence: why does the graph believe this? (1.5 min) — criterion 2

Still on Kenneth Lay, scroll the inspector's **Evidence** list: each extracted MENTIONS fact shows the
highlighted sentence it came from ("Mr. **Ken Lay**"), with confidence and model version. Say: every
model-derived fact stores its source span; we verify the quote exists in the text before storing it.

## 5. Ask (2 min) — criterion 4

Ask page:
1. "Why was Ken Lay asked to donate his stock sale proceeds?" -> cited answer; click a citation chip and it
   scrolls to the evidence card.
2. "What was Jeff Skilling's favorite pizza topping?" -> **amber "not answerable"**: it abstains rather than
   guessing.
3. Toggle graph expansion off and re-ask 1: baseline mode.

Numbers slide (from `api/eval/README.md`, the 2026-10-18 run): 82-question gold set; abstains 100% on unanswerable;
cites the gold email 90% vs. 77% baseline (100% vs. 93% on multi-hop, 5/5 vs. 0/5 on hard multi-hop); p95 3.1 s.

## 6. The creation layer: other engines read our graph (1.5 min) — criterion 5

```bash
uv run igneous export lightrag /tmp/igneous_kg.json
uv run --with lightrag-hku python eval/lightrag_demo.py /tmp/igneous_kg.json "What did Mike Grigsby say about UBS?"
```

Point at: **"LLM calls during load: 0"**. LightRAG didn't re-extract anything; it answered from our
entities, resolved people, and topics.

Then Claude Code with the MCP server: "Who does Kenneth Lay email most?" -> uses `find_entity` +
`correspondents`.

## 7. What's next (1 min)

Google connectors (Gmail/Drive/Calendar), human-reviewed ER labels, scale test at 100k, Bedrock (see
docs/decisions/0002), community detection in the explorer.

## Fallbacks

- If `/ask` is slow on first call, the embedding model is loading; ask once before recording.
- If the explorer canvas is blank, the API isn't running (`make api`).
