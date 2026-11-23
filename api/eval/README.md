# Evaluation

Two measurements, both on the six-mailbox Enron baseline and both reproducible on a fresh build of it: Ask against a
gold question set (criterion 4), and entity resolution against labeled pairs (criterion 3). The gold records and the
labeled pairs are keyed by stable ids, never by database row ids, so they score the same on any build.

## The corpus

```bash
make enron                      # download the mailboxes into data/ (once)
make baseline EXTRACT=0         # sync lay-k skilling-j allen-p arnold-j delainey-d lavorato-j, then the pipeline
```

That is 26,259 emails (13,902 messages after duplicate copies merge). The sync takes about a minute and a half and
the spine about two minutes; embedding takes most of the time (about 45 minutes on CPU). `EXTRACT=0` makes no model
calls.

## Ask (criterion 4)

`questions.jsonl` holds 82 questions: 50 single-email, 15 multi-hop (asked in terms of email A, answered only by a
reply B in the same thread), 5 hard multi-hop and 12 unanswerable. A hard multi-hop question comes from a verified
reply pair (REPLIES_TO from quoted headers) and is kept only if dense search finds the parent email but not the reply
that holds the answer, so answering it requires following the graph edge. `make_gold.py` generated the set from the
corpus and kept a question only if its answer quote appears verbatim in its gold email. **Spot-check by hand before
the final report**: generated questions can be oddly specific.

Each answerable question names its gold email (and, for multi-hop, its bridge email A) by key: the source, the
external id (the maildir path) and the content fingerprint (`gold.py`). At run time a key resolves to every copy of
that email in the database, since Enron files one message in several folders. Every key in the set resolves on a
fresh build, and each answer quote is in its gold email there.

```bash
uv run python eval/run_eval.py --no-qa                    # retrieval only: free, no model calls
IGNEOUS_LLM_SOURCES=email uv run python eval/run_eval.py  # + Ask, full and baseline configs
uv run python eval/run_eval.py --configs full,graph,baseline   # + the graph without the cross-encoder
uv run python eval/run_eval.py --score eval/results/2026-10-18-answers.jsonl   # rescore saved answers
```

What each number means:

- **recall@10**: the share of answerable questions with a gold email among the top 10 passages, per search mode, with
  no question filters (the index alone).
- **cited_gold**: the share of answerable questions that Ask answered (`answerable: true`) citing a gold email. An
  abstention never counts, even if the model listed a gold email.
- **abstention**: the share of unanswerable questions Ask declined (`answerable: false`).
- **p50 / p95**: latency of the whole answer, retrieval and model call.

The configs: **full** is Ask as it ships (hybrid retrieval, graph expansion, the cross-encoder keeps the best 8
passages); **graph** is the same without the cross-encoder (retrieval order, 3 of the 8 places held for graph
neighbours); **baseline** is dense retrieval only, 8 passages, no graph.

Cost: Ask makes one call on the main model per question and config, 164 for full and baseline (246 with all three).
Reading filters out of the questions takes at most one cheap-model call per question the first time, cached after.
Ask, `make_gold.py` and `er_labels.py draft|review` send records to the model, so they run only when every source in
the database is opted in (`IGNEOUS_LLM_SOURCES=email` on the Enron database); otherwise they stop and say which aren't.

`results/<date>.json` records the numbers and what produced them: the commit, the database and its corpus, the
models, and the settings. `results/<date>-answers.jsonl` holds every config's answer to each question, its citations
as keys, for the blind A/B grading criterion 4 calls for and for `--score`.

### Results

Measured 2026-10-18 at commit `3478204` on the baseline (`results/2026-10-18.json`), scored with the metric above.
Ask at that commit gave the full config 12 passages (8 hits plus up to 4 graph neighbours ranked by embedding
similarity to the question) and the baseline 8, with no cross-encoder and no question filters; Ask as it ships now
has not been measured, which needs a Bedrock key (#190).

| | full (hybrid + graph) | baseline (dense, no graph) |
|---|---|---|
| answered citing a gold email, all answerable (70) | **0.871** | 0.743 (**+17% rel.**) |
| ... single-email (50) | 0.82 | 0.76 |
| ... multi-hop (15) | 1.00 | 0.93 |
| ... **hard multi-hop** (5) | **5/5** | **0/5** |
| abstains on unanswerable (12) | 1.00 | 1.00 |
| p50 / p95 latency | 2.2 s / 3.1 s | 1.8 s / 2.5 s |
| recall@10 of its search mode | 0.814 (hybrid) | 0.786 (dense) |

Lexical recall@10 was 0.414 on the same run. How to read it: the graph wins exactly where it should, on answers only
reachable through a reply link, and helps a little elsewhere. Only 5 questions passed the "needs the hop" filter, so
n=5 is small, and model answers vary by several points from run to run on the same questions, so treat differences
under about 5 points as noise. The blind A/B grading of answer quality is still to do;
`results/2026-10-18-answers.jsonl` has the pairs.

### What Ask shows the model, and how long finding it takes

Measured 2026-11-23 on the six-mailbox baseline built with main at `16494f3` (`results/2026-11-23.json`, commit
`877e045`, no model calls). Recall@10 on this build: 0.814 hybrid, 0.800 dense, 0.414 lexical; on the 20 multi-hop
questions 0.65 hybrid, 0.75 dense. **Evidence recall** is the share of the 70 answerable questions with a gold email
among the passages Ask would show the model (`qa.gather`); the seconds are for finding them, before any model call.

| Ask's evidence | evidence recall | p50 / p95 |
|---|---|---|
| hybrid + graph + cross-encoder, pool 8 | 0.871 | 1.55 s / 3.81 s |
| hybrid + graph + cross-encoder, **pool 16 (default)** | **0.886** | 3.04 s / 5.17 s |
| hybrid + graph + cross-encoder, pool 24 | 0.886 | 5.38 s / 8.00 s |
| hybrid + graph + cross-encoder, pool 32 | 0.886 | 4.53 s / 9.41 s |
| hybrid + graph, no cross-encoder (graph slots), any pool | 0.871 | 0.42 s / 1.02 s |
| dense only, no graph (the baseline) | 0.771 | 0.04 s / 0.06 s |

How the defaults follow: a pool of 16 finds every gold email 24 and 32 find, at about half their p95, so it is
`IGNEOUS_RERANKER_POOL_SIZE`'s default. The cross-encoder adds one question of 70 over the graph slots, at about seven
times the time; whether that question, and the order it gives the passages, improves the answers is for the
model-graded run (#190) to say, and `IGNEOUS_RERANKER_ENABLED=false` trades it for sub-second retrieval meanwhile. The
graph itself is worth ten questions over the baseline. These timings were taken while other jobs kept the 4-core
machine at a load of about 6 to 8: an idle machine is faster, the ratios hold.

The cross-encoder reads the first 1,000 characters of each passage (`rerank.SCORED_CHARS`): the title every chunk
opens with and the start of its text. Reading only 600, 1,000 or 1,500 characters chose evidence with the same recall
(0.886) as reading whole passages (about 1,400 characters on average), and the cross-encoder took 20 to 60% less time
at the same machine load. Rerun with
`IGNEOUS_LLM_SOURCES=email uv run python eval/run_eval.py --no-qa --pools 8,16,24,32`.

## Entity resolution (criterion 3)

`er_pairs.csv`: 240 Person pairs sampled across the resolver's score range (¼ low, ½ middle, ¼ high), each named by
the two identities' keys (an email address, or `name:<slug>` for a person model extraction found).

**Labels, two passes:**
1. *Draft*: Claude Sonnet 5.5, shown each identity's 3 most recent sent emails (`draft_label`).
2. *Review*: Claude Opus 5.5, a different and stronger model, shown more evidence (6 sent and 3 received emails and
   top correspondents) and **not shown the draft label or rationale** (`label`, `reviewer`, `review_rationale`). It
   changed 17 of 235 labels: 16 drafted "different" were the same person (Steve/Stephen Swain, Jeff/Jeffery McMahon,
   Ken/Kenneth Rice), 1 the reverse. It marked 46 pairs "unsure".

Five mailing-list pairs are `label=n/a` (lists aren't people). `reviewed=1` is reserved for human review; none is done
yet. **These are model labels, not hand labels**, so report them as such.

```bash
uv run python eval/er_labels.py score --sweep   # P/R/F1 of the resolver on this database; no model calls
uv run python eval/er_labels.py review          # independent second pass (resumable; skips reviewed rows)
```

`score` judges the resolver's real decisions, matching each labeled pair by its two keys: the **deployed clusters**,
the identities the graph shows as one person after the resolve stage, and the **pairwise decision** at a threshold
(the scores of `resolve.candidates()`, after its precision rules). Measured 2026-11-23 on the baseline built with
`EXTRACT=0`, resolved by the resolver at commit `c21abbe`. 7 of the 235 AI-reviewed pairs name a `name:` identity
that only model extraction creates; "present" leaves them out, "every labeled" counts them as not merged:

| Decision | Labels | Pairs | Precision | Recall | F1 |
|---|---|---|---|---|---|
| **Deployed clusters** | AI-reviewed, present | 228 | 0.949 | 0.943 | **0.946** |
| Deployed clusters | AI-reviewed, every labeled | 235 | 0.949 | 0.925 | 0.937 |
| Pairwise, threshold 0.5 | AI-reviewed, present | 228 | 0.943 | 0.943 | 0.943 |
| Pairwise, threshold 0.5 | AI-reviewed, every labeled | 235 | 0.943 | 0.925 | 0.934 |
| Pairwise, threshold 0.6 | AI-reviewed, present | 228 | 0.983 | 0.728 | 0.836 |
| Deployed clusters | AI-reviewed, reviewer confident, present | 186 | 0.978 | 0.957 | 0.968 |

Two addresses merge only on corroboration: correspondents they share (counted on merged identities, each weighted by
how few people it corresponds with, never you or an automated sender), the same organizational domain, or the same
local part. An exact full name alone never merges two addresses, because in personal mail every namesake shares you:
here that keeps one false pair apart (Donna Jones at Enron and at Williams) and three true ones too (Katherine Brown,
Ed Robinson and Adam Bayer, each at Enron and at another domain, with no correspondent in common). A phone or a name
extracted from text still joins on its exact name when no other bearer has it, and automated senders are never
candidates (two "Customer Service" mailboxes). The threshold is 0.5 because raising it buys little precision for much
recall. On that build the resolver scores 558 links; Larry Taylor stays separate from Liz Taylor, and Kenneth Lay is
one node (10 email identities).

## Life threads and projects

`threads_eval.py` scores the derived projects and life threads against a personal gold file kept in git-ignored
`data/eval/threads_gold.json` (it names a person's projects and people, so it never belongs in the repo); its
docstring gives the format. It runs on your own database and makes no model calls:

```bash
IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_personal \
  uv run python eval/threads_eval.py
```

The tests score a synthetic life the same way: `tests/life_threads.py` writes a year of invented records and their
gold threads (`GOLD`, in that format), and `tests/test_life_threads.py` requires every one found once, under its
parent.

## The synthetic persona

`make persona` builds `igneous_persona` from a fictional student's generated exports (`api/igneous/persona`). Its gold
set lives beside the generator (`api/igneous/persona/gold`), keyed by stable record keys (a local file by its path in
the documents folder, since its id starts with a hash of where the folder sits), and describes the default seed:

- `threads.json`: the trips, classes, job and project, each with the records that must and must not be in it, who
  took part, and the organization it sits under, in `threads_eval.py`'s format;
- `people.json`: the identities that are one person and pairs that are two (true by construction), scored like
  `er_pairs.csv` by `er_labels.py`'s scoring;
- `questions.jsonl`: Ask questions with their gold records and quotes, in `questions.jsonl`'s format.

`persona_eval.py` scores all three on the database it runs on, without model calls unless `--qa`:

```bash
IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_persona \
  uv run python eval/persona_eval.py
```

On the default seed with the model off (2026-11-23, main at `ed585c1`), after `make persona` (one pipeline run) and
after a second run:

| | One run | Two runs |
|---|---|---|
| Gold threads found (and the two roles at Northwind) | 14 of 14 | 14 of 14 |
| Found twice or more (another Project for the same thing) | 11 | 10 |
| Records that must not be in their thread, but are | 1 | 1 |
| Placed right (under its school, job or role, or under nothing) | 9 of 12 | 9 of 12 |
| Who took part: missing or wrongly there | 0 | 0 |
| Identities: clusters P / R | 1.00 / 0.94 | 1.00 / 0.94 |
| Identities: pairs ≥ 0.5, P / R | 1.00 / 0.83 | 1.00 / 0.83 |
| Ask retrieval, recall@10 over 13 answerable questions: hybrid / dense / lexical | 0.85 / 0.77 / 0.85 | the same |

- Most duplicates are a Project the entities stage named from a folder ("Lisbon 2025", "CS 341 Operating Systems",
  "tidepool") beside the thread the threads stage found for the same thing; CS 481 is also split in two threads
  ("Harborline", "Demo Day").
- Each organization is one node from the first run on: the company Mira's LinkedIn and her connections name
  (`org:lakeshore-university`), which holds the email domain its people write from (`lakeshore.example.edu`), with
  her LinkedIn link and the one her mail shows. A second run changes no organization node or link.
- Northwind Robotics shows once, with a thread for each role on Mira's LinkedIn: Software Engineering Intern
  (Jan 2025 - May 2026) and Software Engineer (since Jul 2026). The Heron field trials (June 2025) sit under the
  internship; the hiring, before either role, under the company.
- The placements are by email domain: the classes, the Heron field trials and the hiring thread go under the right
  organization, and the Lisbon and Iceland trips and Tidepool under Lakeshore University too, since most of their
  people write from the school's domain.
- The leak is a "remember Lisbon?" chat six months after the trip: the folder-named "Lisbon 2025" takes in every
  record that mentions Lisbon.
- The only identity left apart is classmate Alex Chen's LinkedIn profile: two people share the name, so the name
  alone joins neither.
- Retrieval moves by a question or two between builds of the same seed (hybrid recall@10 from 0.77 to 0.92 over
  four builds): lexical search orders equal scores by however the rows were stored, and the vector index (HNSW) is
  approximate.
- With the model off, threads keep their fallback names ("Lisbon · Jan-Mar 2025", kind "thread"). `--qa` needs the
  model.

## LightRAG on Igneous's graph (criterion 5)

`lightrag_demo.py` loads `igneous export lightrag` into LightRAG and answers a question from it; see its docstring.
