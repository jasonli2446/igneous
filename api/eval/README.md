# Evaluation

`questions.jsonl` is the gold set (77 questions): 50 single-email, 15 multi-hop (asked in terms of email A,
answered only by a reply B in the same thread), 12 unanswerable. Generated from the corpus by
`make_gold.py` and filtered so every answer quote appears verbatim in its gold email. **Spot-check by hand
before the final report** — generated questions can be oddly specific.

```bash
uv run python eval/run_eval.py --no-qa   # retrieval only, free, ~15 s
uv run python eval/run_eval.py           # + Q&A for both configs, ~150 model calls, ~6 min
```

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

`er_pairs.csv`: 240 Person pairs sampled across the resolver's score range (¼ low, ½ middle, ¼ high), each
labeled from the two identities' own mail. **Labels are model-proposed (`reviewed=0`)**; criterion 3 calls for
hand labels, so review them (fix `label`, set `reviewed=1`) before quoting the number. Five mailing-list pairs
are `label=n/a`: lists aren't people, so they're out of scope for Person ER.

```bash
uv run python eval/er_labels.py score --sweep   # P/R/F1 per threshold, rescoring with the current resolver
```

At the chosen threshold (0.6, the precision-favoring point that clears the target), on 235 in-scope pairs:
**precision 0.914, recall 0.801, F1 0.854**. That's a thin margin over 0.85, on unreviewed labels.
