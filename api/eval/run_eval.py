"""Run the gold set through retrieval and Ask, and record the numbers with what produced them.

    uv run python eval/run_eval.py --no-qa      # retrieval only: free, no model
    uv run python eval/run_eval.py              # + Ask for each config (hosted model calls; see eval/README.md)
    uv run python eval/run_eval.py --configs full,graph,baseline   # also the graph without the cross-encoder
    uv run python eval/run_eval.py --score eval/results/2026-10-18-answers.jsonl   # rescore saved answers only
    uv run python eval/run_eval.py --no-qa --pools 8,16,32     # what Ask would show the model, per pool size

Retrieval: recall@10 is the share of answerable questions with a gold record among the top 10 passages, per search
mode, with no question filters and no opt-in (the index alone). Evidence, per config (and per reranker pool size for
the configs that use the graph): the share of answerable questions with a gold record among the passages Ask would
show the model (`qa.gather`), and how long finding them took; it needs the corpus opted in and calls no model (a
question-filter reading the cheap model gave earlier is reused, no new one is made). Ask, per config: cited_gold is the share of
answerable questions answered (answerable: true) with a citation of a gold record, so an abstention never counts;
abstention is the share of unanswerable questions answered with answerable: false; p50/p95 is the latency of the
whole answer.

Gold records are keyed by stable ids (gold.py) and resolved against the database this runs on; a question whose gold
isn't in it is reported and left out. results/<date>.json records the commit, the corpus, the models and the settings
behind the numbers; results/<date>-answers.jsonl keeps every answer with its citations as keys, for blind A/B grading
and for --score.
"""
import argparse
import json
import statistics
import subprocess
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import gold
from sqlalchemy import text

from igneous import llm
from igneous.config import settings
from igneous.db import SessionLocal, engine
from igneous.rag import qa
from igneous.rag.search import hybrid

HERE = Path(__file__).parent
RESULTS = HERE / "results"
MODES = ("hybrid", "dense", "lexical")
CONFIGS = {  # how Ask runs in each config
    "full": {"mode": "hybrid", "use_graph": True, "reranker": True},
    "graph": {"mode": "hybrid", "use_graph": True, "reranker": False},  # the graph without the cross-encoder
    "baseline": {"mode": "dense", "use_graph": False, "reranker": False},  # dense retrieval only
}
DEFAULT_CONFIGS = ("full", "baseline")


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p * (len(xs) - 1)))] if xs else 0.0


def kind_of(q: dict) -> str:
    return q["kind"] if q["kind"].startswith("multihop") else "single"


def qa_metrics(questions: list[dict], rows: list[dict]) -> dict:
    """Ask's numbers per config from answer rows ({config, id, answerable, cited: [keys], seconds?, error?})."""
    by_id = {q["id"]: q for q in questions}
    out = {}
    for config in dict.fromkeys(r["config"] for r in rows):
        hits: dict[str, list[bool]] = {}
        abstained, unanswerable, errors, latency = 0, 0, 0, []
        for r in (r for r in rows if r["config"] == config and r["id"] in by_id):
            q = by_id[r["id"]]
            errors += bool(r.get("error"))
            if "seconds" in r:
                latency.append(r["seconds"])
            if not q["answerable"]:
                unanswerable += 1
                abstained += not r.get("error") and not r["answerable"]
                continue
            hit = bool(r["answerable"]) and any(gold.same(c, g) for c in r["cited"] for g in q["gold"])
            for kind in ("all", kind_of(q)):
                hits.setdefault(kind, []).append(hit)
        out[config] = {
            "cited_gold": round(sum(hits.get("all", [])) / max(1, len(hits.get("all", []))), 3),
            **{f"cited_gold_{k}": round(sum(v) / len(v), 3) for k, v in sorted(hits.items()) if k != "all"},
            "abstention": round(abstained / max(1, unanswerable), 3),
            "answerable_questions": len(hits.get("all", [])), "unanswerable_questions": unanswerable,
            "errors": errors,
            **({"p50_s": round(statistics.median(latency), 2), "p95_s": round(pct(latency, 0.95), 2)} if latency else {}),
        }
    return out


def provenance(s, configs: list[str]) -> dict:
    """What produced the numbers: the code, the corpus, the models and the settings."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True).stdout.strip()
    chunks = s.execute(text("SELECT count(*) AS chunks, count(embedding) AS embedded FROM chunks")).one()
    return {
        "commit": git("rev-parse", "HEAD") or None, "uncommitted_changes": bool(git("status", "--porcelain")),
        "database": engine.url.database,
        "corpus": {"source_items": dict(s.execute(text("SELECT source, count(*) FROM source_items GROUP BY 1")).all()),
                   "chunks": chunks.chunks, "embedded": chunks.embedded},
        "models": {"answer": settings.llm_model, "question_filters": settings.llm_cheap_model,
                   "embedding": settings.embedding_model, "reranker": settings.reranker_model},
        "settings": {"recall_k": 10, "context_chunks": qa.CONTEXT_CHUNKS, "graph_slots": qa.GRAPH_SLOTS,
                     "expand_per_hit": qa.EXPAND_PER_HIT, "reranker_pool_size": settings.reranker_pool_size,
                     "llm_sources": sorted(llm.allowed_sources())},
        "configs": {name: CONFIGS[name] for name in configs},
    }


def recall(s, questions: list[dict], golds: dict[str, set[int]], mode: str, k: int = 10) -> float:
    hits = 0
    for q in questions:
        got = {h["source_item_id"] for h in hybrid(s, q["question"], k=k, mode=mode)}
        hits += bool(got & golds[q["id"]])
    return round(hits / max(1, len(questions)), 3)


@contextmanager
def reranker(on: bool):
    saved = settings.reranker_enabled
    settings.reranker_enabled = saved and on
    try:
        yield
    finally:
        settings.reranker_enabled = saved


@contextmanager
def pool(size: int):
    saved = settings.reranker_pool_size
    settings.reranker_pool_size = size
    try:
        yield
    finally:
        settings.reranker_pool_size = saved


@contextmanager
def no_model_calls():
    saved = settings.llm_enabled
    settings.llm_enabled = False
    try:
        yield
    finally:
        settings.llm_enabled = saved


def evidence(s, questions: list[dict], golds: dict[str, set[int]], configs: list[str], pools: list[int]) -> dict:
    """Per config, and per pool size for the configs that use the graph: evidence_recall, the share of `questions`
    with a gold record among the passages Ask would show the model, and the p50/p95 seconds finding them took."""
    out = {}
    for name in configs:
        config = CONFIGS[name]
        for size in pools if config["use_graph"] else [settings.reranker_pool_size]:
            label = f"{name}@{size}" if config["use_graph"] else name
            found, seconds = 0, []
            with reranker(config["reranker"]), pool(size), no_model_calls():
                for q in questions:
                    ev = qa.gather(s, q["question"], use_graph=config["use_graph"], mode=config["mode"])
                    seconds.append(ev.retrieve_s)
                    found += bool({p["source_item_id"] for p in ev.passages} & golds[q["id"]])
            out[label] = {"evidence_recall": round(found / max(1, len(questions)), 3),
                          "p50_s": round(pct(seconds, 50), 2), "p95_s": round(pct(seconds, 95), 2)}
            print("evidence", label, out[label])
    return out


def ask_all(s, questions: list[dict], configs: list[str]) -> list[dict]:
    rows = []
    for name in configs:
        config = CONFIGS[name]
        with reranker(config["reranker"]):
            for q in questions:
                t = time.time()
                try:
                    r = qa.answer(s, q["question"], use_graph=config["use_graph"], mode=config["mode"])
                except llm.ProviderError as e:  # one failed call is recorded, not fatal to the run
                    rows.append({"config": name, "id": q["id"], "answerable": False, "answer": "", "cited": [],
                                 "seconds": round(time.time() - t, 2), "error": str(e)})
                    continue
                cited = gold.keys_of(s, [c["source_item_id"] for c in r["citations"]])
                rows.append({"config": name, "id": q["id"], "answerable": r["answerable"], "answer": r["answer"],
                             "cited": [cited[c["source_item_id"]] for c in r["citations"]],
                             "seconds": round(time.time() - t, 2)})
        print(name, qa_metrics(questions, [r for r in rows if r["config"] == name])[name])
    return rows


def main(run_qa: bool, configs: list[str], limit: int | None, score: Path | None, pools: list[int]):
    questions = gold.load()[:limit]
    if score:
        rows = [json.loads(line) for line in score.read_text().splitlines() if line.strip()]
        print(json.dumps(qa_metrics(questions, rows), indent=2))
        return
    report: dict = {"date": date.today().isoformat()}
    with SessionLocal() as s:
        if run_qa:
            # Ask reads only opted-in sources: with the corpus not opted in, every answer would be an abstention.
            llm.require_whole_corpus(s)
        report["provenance"] = provenance(s, configs if run_qa else [])
        answerable = [q for q in questions if q["answerable"]]
        resolved = gold.resolve(s, [k for q in answerable for k in q["gold"]])
        golds, at = {}, 0
        for q in answerable:
            golds[q["id"]] = set().union(*resolved[at:at + len(q["gold"])])
            at += len(q["gold"])
        missing = sorted(q["id"] for q in answerable if not golds[q["id"]])
        scored = [q for q in answerable if golds[q["id"]]]
        report["questions"] = {"answerable": len(scored), "unanswerable": len(questions) - len(answerable),
                               "gold_not_in_database": missing}
        if missing:
            print(f"left out {len(missing)} questions whose gold record isn't in this database: {', '.join(missing)}")
        report["recall@10"] = {m: recall(s, scored, golds, m) for m in MODES}
        multi = [q for q in scored if q["kind"].startswith("multihop")]
        report["recall@10_multihop"] = {m: recall(s, multi, golds, m) for m in ("hybrid", "dense")}
        print("recall@10", report["recall@10"], "multihop", report["recall@10_multihop"])
        if llm.allowed_sources():
            report["evidence"] = evidence(s, scored, golds, list(CONFIGS), pools)
        else:
            print("evidence: skipped, nothing is opted in (set IGNEOUS_LLM_SOURCES=email for the Enron corpus)")
        if run_qa:
            asked = scored + [q for q in questions if not q["answerable"]]
            rows = ask_all(s, asked, configs)
            report.update(qa_metrics(asked, rows))
            # Every config's answer to each question, for the blind A/B grading the proposal calls for.
            RESULTS.mkdir(exist_ok=True)
            (RESULTS / f"{report['date']}-answers.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in rows))

    out = RESULTS / f"{report['date']}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"-> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-qa", action="store_true", help="retrieval only; no model calls")
    ap.add_argument("--configs", default=",".join(DEFAULT_CONFIGS), help=f"Ask configs to run, of {', '.join(CONFIGS)}")
    ap.add_argument("--limit", type=int, help="only the first N questions")
    ap.add_argument("--score", type=Path, help="rescore a saved answers file against the gold set and exit")
    ap.add_argument("--pools", default=str(settings.reranker_pool_size),
                    help="reranker pool sizes to measure the evidence at, comma-separated")
    a = ap.parse_args()
    chosen = [c.strip() for c in a.configs.split(",") if c.strip()]
    if unknown := sorted(set(chosen) - set(CONFIGS)):
        ap.error(f"unknown config {', '.join(unknown)}; choose from {', '.join(CONFIGS)}")
    try:
        sizes = sorted({int(x) for x in a.pools.split(",") if x.strip()})
    except ValueError:
        ap.error("--pools takes whole numbers, comma-separated")
    if not sizes or min(sizes) < 1:
        ap.error("--pools needs at least one size of 1 or more")
    main(not a.no_qa, chosen, a.limit, a.score, sizes)
