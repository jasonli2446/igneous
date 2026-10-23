"""Run the gold set through retrieval and Q&A; report the criterion-4 numbers.

    uv run python eval/run_eval.py              # retrieval (free) + Q&A (costs model calls)
    uv run python eval/run_eval.py --no-qa      # retrieval only

Retrieval: recall@10 = share of answerable questions whose gold source item is among the top-10 chunks,
for each mode. Q&A: abstention rate on unanswerable questions, citation hit rate on answerable ones, and
p50/p95 latency for the full pipeline vs. the dense-only, no-graph baseline.
"""
import argparse
import json
import statistics
import time
from datetime import date
from pathlib import Path

from igneous import llm
from igneous.db import SessionLocal
from igneous.rag import qa
from igneous.rag.search import hybrid

HERE = Path(__file__).parent
CONFIGS = {  # name -> (search mode, graph expansion)
    "full": ("hybrid", True),
    "baseline": ("dense", False),
}


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p * (len(xs) - 1)))] if xs else 0.0


def recall(s, questions, mode: str, k: int = 10) -> float:
    hits = 0
    for q in questions:
        got = {h["source_item_id"] for h in hybrid(s, q["question"], k=k, mode=mode)}
        hits += bool(got & set(q["gold_source_items"]))
    return hits / max(1, len(questions))


def main(run_qa: bool, limit: int | None):
    questions = [json.loads(line) for line in (HERE / "questions.jsonl").read_text().splitlines()][:limit]
    ans = [q for q in questions if q["answerable"]]
    unans = [q for q in questions if not q["answerable"]]
    report = {"date": date.today().isoformat(), "n_answerable": len(ans), "n_unanswerable": len(unans)}

    with SessionLocal() as s:
        if run_qa:
            # Ask reads only opted-in sources: with the corpus not opted in, every answer would be an abstention.
            llm.require_whole_corpus(s)
        report["recall@10"] = {m: round(recall(s, ans, m), 3) for m in ("hybrid", "dense", "lexical")}
        multi = [q for q in ans if q["kind"].startswith("multihop")]
        report["recall@10_multihop"] = {m: round(recall(s, multi, m), 3) for m in ("hybrid", "dense")}
        print("recall@10", report["recall@10"], "multihop", report["recall@10_multihop"])
        if run_qa:
            rows = []
            for name, (mode, graph) in CONFIGS.items():
                lat, cited_gold, abstained = [], 0, 0
                by_kind: dict[str, list[int]] = {}
                for q in questions:
                    t = time.time()
                    r = qa.answer(s, q["question"], use_graph=graph, mode=mode)
                    lat.append(time.time() - t)
                    if q["answerable"]:
                        hit = bool({c["source_item_id"] for c in r["citations"]} & set(q["gold_source_items"]))
                        cited_gold += hit
                        by_kind.setdefault(q["kind"] if q["kind"].startswith("multihop") else "single", []).append(hit)
                    else:
                        abstained += not r["answerable"]
                    rows.append({"config": name, "id": q["id"], "answerable": r["answerable"], "answer": r["answer"],
                                 "cited": [c["source_item_id"] for c in r["citations"]]})
                report[name] = {
                    "cited_gold": round(cited_gold / max(1, len(ans)), 3),
                    "abstention": round(abstained / max(1, len(unans)), 3),
                    "p50_s": round(statistics.median(lat), 2), "p95_s": round(pct(lat, 0.95), 2),
                    **{f"cited_gold_{k}": round(sum(v) / len(v), 3) for k, v in sorted(by_kind.items())},
                }
                print(name, report[name])
            # Answers side by side, for the blind A/B grading the proposal calls for.
            (HERE / "results").mkdir(exist_ok=True)
            (HERE / "results" / f"{report['date']}-answers.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in rows))

    out = HERE / "results" / f"{report['date']}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"-> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-qa", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    main(not a.no_qa, a.limit)
