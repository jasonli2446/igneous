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

Retrieval alone, measured again 2026-11-23 at commit `63994a9` on a fresh build (`results/2026-11-23.json`, no model
calls): recall@10 0.829 hybrid, 0.800 dense, 0.386 lexical; on the 20 multi-hop questions 0.65 hybrid, 0.70 dense.
Search ranks as it did on 2026-10-18 (that code scores the same on this build), so the differences from the table
come from the build.

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

## LightRAG on Igneous's graph (criterion 5)

`lightrag_demo.py` loads `igneous export lightrag` into LightRAG and answers a question from it; see its docstring.
