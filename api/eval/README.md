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

## Baseline results (2026-10-18, 6 mailboxes, 13.9k messages)

| | full (hybrid + graph) | baseline (dense, no graph) |
|---|---|---|
| recall@10, all answerable | 0.877 | 0.846 |
| answer cites gold email | **0.892** | 0.846 |
| ... multi-hop | **1.000** | 0.933 |
| abstains on unanswerable | 1.00 | 1.00 |
| p50 / p95 latency | 2.1 s / 3.0 s | 1.9 s / 2.6 s |

Caveats: dense recall on the multi-hop set is already high (replies restate the subject), so these are easier
than real multi-hop questions and the graph gain is modest. Latency excludes cold-start model loading.
Criterion 4 asks for ≥10% over baseline under blind grading; that grading hasn't been done yet.

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
