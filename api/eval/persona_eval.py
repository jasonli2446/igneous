"""The synthetic persona's gold set, scored on a database `make persona` built: its threads (threads_eval.py), the
identities that are one person (er_labels.py's scoring), and Ask's retrieval of the records that answer its questions
(run_eval.py's recall@10); with --qa, Ask itself. The gold lives beside the generator (igneous/persona/gold), keyed
by stable record keys, and describes the default seed.

    IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_persona \\
      uv run python eval/persona_eval.py [--qa]

Without --qa it makes no model calls. --qa asks every question through Ask, which reads only opted-in sources: opt
in the persona's (IGNEOUS_LLM_SOURCES=email,fs,takeout,linkedin,ai_chats with IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED=true)
and set AWS_BEARER_TOKEN_BEDROCK.
"""
import argparse
import json
from itertools import combinations

import er_labels
import gold
import threads_eval

from igneous.db import SessionLocal
from igneous.persona import load_gold

MODES = ("hybrid", "dense", "lexical")


def people_pairs(people: dict) -> list[dict]:
    """The gold identities as labeled pairs, in er_labels' row shape: every two identities of one person (1), and the
    pairs said to be different people (0)."""
    rows = []
    for person in people["people"]:
        for a, b in combinations(person["identities"], 2):
            rows.append({"a_key": a, "b_key": b, "label": "1", "reviewed": "1", "reviewer": "generator"})
    for a, b in people["distinct"]:
        rows.append({"a_key": a, "b_key": b, "label": "0", "reviewed": "1", "reviewer": "generator"})
    return rows


def ask(questions: list[dict], run_qa: bool) -> dict:
    import run_eval

    with SessionLocal() as s:
        answerable = [q for q in questions if q["answerable"]]
        resolved = gold.resolve(s, [k for q in answerable for k in q["gold"]])
        golds, at = {}, 0
        for q in answerable:
            golds[q["id"]] = set().union(*resolved[at:at + len(q["gold"])])
            at += len(q["gold"])
        scored = [q for q in answerable if golds[q["id"]]]
        out = {"answerable": len(scored), "gold_not_in_database": sorted(q["id"] for q in answerable
                                                                         if not golds[q["id"]]),
               "recall@10": {m: run_eval.recall(s, scored, golds, m) for m in MODES}}
        if run_qa:
            from igneous import llm

            llm.require_whole_corpus(s)
            asked = scored + [q for q in questions if not q["answerable"]]
            out.update(run_eval.qa_metrics(asked, run_eval.ask_all(s, asked, ["full"])))
    return out


def main(run_qa: bool) -> dict:
    threads = threads_eval.score(load_gold("threads.json"))
    people = er_labels.score(rows=people_pairs(load_gold("people.json")))
    report = {"threads": threads, "people": people, "ask": ask(load_gold("questions.jsonl"), run_qa)}
    for r in threads["threads"]:
        flag = "OK " if r["found"] and not r["duplicates"] and not r["exclude_leaks"] else "-- "
        print(f"{flag}{r['name']:24s} found={r['found']!s:34.34s} records={r['records']:4d} "
              f"include={r['include_recall']!s:5s} leaks={r['exclude_leaks']} parent={r.get('parent_ok', '-')!s:5s}"
              + (f" MISSING={r['participants_missing']}" if r.get("participants_missing") else "")
              + (f" WRONGLY_WENT={r['wrongly_went']}" if r.get("wrongly_went") else ""))
    print("threads", json.dumps(threads["summary"]))
    er_labels.show(people)
    print("ask", json.dumps(report["ask"]))
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--qa", action="store_true", help="also ask every question through Ask (hosted model calls)")
    main(ap.parse_args().qa)
