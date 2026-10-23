# Evaluation

`questions.jsonl` is the gold set (82 questions): 50 single-email, 20 multi-hop (asked in terms of email A,
answered only by a reply B in the same thread; 5 of them hard), 12 unanswerable. Generated from the corpus by
`make_gold.py` and filtered so every answer quote appears verbatim in its gold email. **Spot-check by hand
before the final report** — generated questions can be oddly specific.

```bash
uv run python eval/run_eval.py --no-qa   # retrieval only, free, ~15 s
uv run python eval/run_eval.py           # + Q&A for both configs, ~150 model calls, ~6 min
```

Q&A, `make_gold.py` and `er_labels.py draft|review` send records to the model, so they run only when every
source in the database is opted in: `IGNEOUS_LLM_SOURCES=email` on the Enron database. Otherwise they stop
and say which sources aren't.

Results land in `results/<date>.json`; `results/<date>-answers.jsonl` has both configs' answers side by side
for the blind A/B grading in criterion 4.

## Results (2026-10-18, 6 mailboxes, 13.9k messages)

82 questions: 50 single-email, 15 multi-hop, **5 hard multi-hop**, 12 unanswerable. "Hard" questions come from
verified reply pairs (REPLIES_TO from quoted headers) and are kept only if dense search finds the parent email
but **not** the reply holding the answer. Answering them requires following the graph edge.

| | full (hybrid + graph) | baseline (dense, no graph) |
|---|---|---|
| answer cites gold email, all answerable | **0.900** | 0.771 (**+16.7% rel.**) |
| ... single-email | 0.86 | 0.80 |
| ... multi-hop | 1.00 | 0.93 |
| ... **hard multi-hop** | **5/5** | **0/5** |
| abstains on unanswerable | 1.00 | 1.00 |
| p50 / p95 latency | 2.2 s / 3.1 s | 1.8 s / 2.5 s |

How to read it: the graph wins exactly where it should (answers only reachable through a reply link) and
helps a little elsewhere. But only 5 corpus questions passed the "needs the hop" filter, so on this corpus most
questions don't need the graph, and n=5 is small. Model answers also vary run to run (the baseline moved
0.77-0.85 across runs on the same questions), so treat differences under ~5 points as noise. Criterion 4's
blind A/B grading of answer quality is still to do; `results/<date>-answers.jsonl` has the pairs.

## Entity resolution (criterion 3)

`er_pairs.csv`: 240 Person pairs sampled across the resolver's score range (¼ low, ½ middle, ¼ high).

**Labels, two passes:**
1. *Draft*: Claude Sonnet 5.5, shown each identity's 3 most recent sent emails (`draft_label`).
2. *Review*: Claude Opus 5.5, a different and stronger model, shown more evidence (6 sent + 3 received emails
   and top correspondents) and **not shown the draft label or rationale** (`label`, `reviewer`,
   `review_rationale`). It changed 17 of 235 labels: 16 drafted "different" were the same person
   (Steve/Stephen Swain, Jeff/Jeffery McMahon, Ken/Kenneth Rice), 1 the reverse. It marked 46 pairs "unsure".

Five mailing-list pairs are `label=n/a` (lists aren't people). `reviewed=1` is reserved for human review;
none is done yet. **These are model labels, not hand labels**, so report them as such.

```bash
uv run python eval/er_labels.py review          # independent second pass (resumable; skips reviewed rows)
uv run python eval/er_labels.py score --sweep   # P/R/F1 per threshold, rescoring with the current resolver
```

| Threshold | Labels | Pairs | Precision | Recall | F1 |
|---|---|---|---|---|---|
| **0.5 (current)** | AI-reviewed | 235 | 0.938 | 0.944 | **0.941** |
| 0.5 | AI-reviewed, reviewer confident | 189 | 0.971 | 0.957 | 0.964 |
| 0.6 (previous) | AI-reviewed | 235 | 0.969 | 0.770 | 0.858 |
| 0.6 | draft only | 235 | 0.914 | 0.801 | 0.854 |

The draft labels undercounted true duplicates, which made recall look worse than it is. Re-labeling moved the
best threshold from 0.6 to 0.5 at almost no precision cost. Structural guards still hold at 0.5: Larry Taylor
stays separate from Liz Taylor, and Kenneth Lay resolves to one node (12 identities).
