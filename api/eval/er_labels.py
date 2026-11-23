"""Entity-resolution labels and P/R/F1 (criterion 3).

    uv run python eval/er_labels.py draft --n 240   # sample pairs across the score range, model-proposed labels
    uv run python eval/er_labels.py review          # an independent second label from a stronger model
    uv run python eval/er_labels.py score [--sweep] # P/R/F1 of the resolver on this database

Pairs are two Person identities named by their keys (an email address, or `name:<slug>` for a person model
extraction found), so the labels apply to any build of the corpus. `draft` writes eval/er_pairs.csv with a `label`
the model proposes from each identity's mail, `reviewed=0`, and `score`, the resolver's score when the pair was
sampled. These are PROPOSALS: criterion 3 requires human-verified labels, so a teammate should review the rows (set
label, set reviewed=1) before the number goes in the report.

`score` judges the resolver's real decisions on the database it runs on: the deployed clusters (what the graph shows
after the resolve stage) and the pairwise decision at a threshold (the scores of `candidates()`, after its precision
rules). It uses human-reviewed rows if any exist, else the AI-reviewed ones, else all rows, and says which. A pair
naming an identity this database doesn't have is reported both ways: left out, and counted as not merged.
"""
import argparse
import csv
import random
import sys
from pathlib import Path

from sqlalchemy import text

from igneous import llm
from igneous.db import SessionLocal
from igneous.graph.resolve import THRESHOLD, candidates

OUT = Path(__file__).with_name("er_pairs.csv")
FIELDS = ["a_key", "b_key", "a_name", "b_name", "score", "shared_peers", "label", "reviewed", "rationale",
          "draft_label", "reviewer", "review_rationale"]
SWEEP = (0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9)
REVIEW_MODEL = "us.anthropic.claude-opus-5-5"  # a different, stronger model than the drafter (settings.llm_model)

REVIEW_SYSTEM = """You audit an entity-resolution dataset. Decide whether two email identities from one company's
archive are the SAME human. You see each identity's name, address, several emails they sent or received, and
the people they correspond with. Judge only from this evidence.

- Same: one human with two addresses or name forms (nicknames, middle initials, personal vs. work address).
- Different: two humans sharing a surname; a person vs. a shared, role, or distribution mailbox; two different
  automated senders.
- Set `confident` false when the evidence can't settle it; still give your best judgment in `same`."""
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"same": {"type": "boolean"}, "confident": {"type": "boolean"}, "rationale": {"type": "string"}},
    "required": ["same", "confident", "rationale"],
    "additionalProperties": False,
}

SYSTEM = """Decide whether two email identities from one company's archive are the SAME person.
You get each identity's display name, address, and a few emails they sent (with signatures).
Same person: one human using two addresses or name forms. Different: two humans who share a surname, or a
person vs. a shared/role mailbox. If the evidence is thin, judge from names and roles, and say so."""
SCHEMA = {
    "type": "object",
    "properties": {"same": {"type": "boolean"}, "rationale": {"type": "string"}},
    "required": ["same", "rationale"],
    "additionalProperties": False,
}


def profile(s, pid: int) -> str:
    rows = s.execute(text("""
        SELECT si.title, left(si.body_text, 500) AS body FROM edges e
        JOIN nodes m ON m.id = e.dst JOIN source_items si ON si.id = m.source_item_id
        WHERE e.src = :id AND e.type = 'SENT' ORDER BY si.occurred_at DESC LIMIT 3
    """), {"id": pid}).all()
    if not rows:  # receive-only addresses: show what they were sent instead
        rows = s.execute(text("""
            SELECT si.title, left(si.body_text, 300) AS body FROM edges e
            JOIN nodes m ON m.id = e.src JOIN source_items si ON si.id = m.source_item_id
            WHERE e.dst = :id AND e.type = 'RECEIVED' ORDER BY si.occurred_at DESC LIMIT 2
        """), {"id": pid}).all()
    return "\n---\n".join(f"Subject: {r.title}\n{r.body}" for r in rows) or "(no mail)"


def rich_profile(s, pid: int) -> str:
    """More evidence than the drafter saw: up to 6 sent and 3 received mails plus top correspondents."""
    sent = s.execute(text("""
        SELECT si.title, si.occurred_at::date AS d, left(si.body_text, 600) AS body FROM edges e
        JOIN nodes m ON m.id = e.dst JOIN source_items si ON si.id = m.source_item_id
        WHERE e.src = :id AND e.type = 'SENT' ORDER BY si.occurred_at DESC LIMIT 6
    """), {"id": pid}).all()
    recv = s.execute(text("""
        SELECT si.title, si.occurred_at::date AS d, left(si.body_text, 300) AS body FROM edges e
        JOIN nodes m ON m.id = e.src JOIN source_items si ON si.id = m.source_item_id
        WHERE e.dst = :id AND e.type = 'RECEIVED' ORDER BY si.occurred_at DESC LIMIT 3
    """), {"id": pid}).all()
    peers = s.execute(text("""
        SELECT p.name, count(*) FROM edges snd JOIN edges rcv ON rcv.src = snd.dst JOIN nodes p ON p.id = rcv.dst
        WHERE snd.src = :id AND snd.type = 'SENT' AND rcv.type = 'RECEIVED' AND rcv.dst <> :id
        GROUP BY p.name ORDER BY 2 DESC LIMIT 8
    """), {"id": pid}).all()
    parts = [f"SENT {r.d} | {r.title}\n{r.body}" for r in sent] + [f"RECEIVED {r.d} | {r.title}\n{r.body}" for r in recv]
    return ("Top correspondents: " + (", ".join(f"{n} ({c})" for n, c in peers) or "none") + "\n\n"
            + ("\n---\n".join(parts) or "(no mail)"))


def review(workers: int = 4):
    """Independent second label for every pair: different model, richer evidence, and the reviewer is NOT shown
    the draft label or rationale. The reviewed label replaces the draft; draft_label keeps the original."""
    from concurrent.futures import ThreadPoolExecutor

    rows = list(csv.DictReader(OUT.open()))
    todo = [r for r in rows if r.get("reviewer", "") == "" and r["label"] in ("0", "1")]
    with SessionLocal() as s:
        llm.require_whole_corpus(s)  # it sends each identity's mail to the model
        ids = person_ids(s)
        absent = [r for r in todo if r["a_key"] not in ids or r["b_key"] not in ids]
        if absent:
            print(f"skipping {len(absent)} pairs naming an identity this database doesn't have", file=sys.stderr)
        todo = [r for r in todo if r not in absent]
        prompts = {id(r): (f"Identity A: {r['a_name']} <{r['a_key']}>\n{rich_profile(s, ids[r['a_key']])}\n\n=====\n\n"
                           f"Identity B: {r['b_name']} <{r['b_key']}>\n{rich_profile(s, ids[r['b_key']])}")
                   for r in todo}

    def one(r):
        out, _ = llm.structured(REVIEW_SYSTEM, prompts[id(r)], REVIEW_SCHEMA, max_tokens=1500, effort="medium",
                                model=REVIEW_MODEL)
        return r, out

    def save():
        with OUT.open("w", newline="") as fh:
            w = csv.DictWriter(fh, FIELDS, lineterminator="\n")
            w.writeheader()
            w.writerows({k: r.get(k, "") for k in FIELDS} for r in rows)

    flips = 0
    with ThreadPoolExecutor(workers) as pool:
        for i, (r, out) in enumerate(pool.map(one, todo), 1):
            r["draft_label"] = r.get("draft_label") or r["label"]
            new = str(int(out["same"]))
            flips += new != r["label"]
            r["label"], r["reviewer"] = new, f"{REVIEW_MODEL}{'' if out['confident'] else ' (unsure)'}"
            r["review_rationale"] = out["rationale"][:400]
            if i % 20 == 0:
                save()  # resumable: finished reviews survive a crash; reruns skip rows with a reviewer
                print(f"  {i}/{len(todo)}  flips so far {flips}", file=sys.stderr)
    save()
    unsure = sum("(unsure)" in r.get("reviewer", "") for r in rows)
    print(f"reviewed {len(todo)} pairs: {flips} labels changed, {unsure} marked unsure -> {OUT}")


def draft(n: int, seed: int):
    with SessionLocal() as s:
        llm.require_whole_corpus(s)  # it sends each identity's mail to the model
        pairs = candidates(s)
        # Stratify by score so labels cover clear positives, clear negatives, and the hard middle.
        bands = {"low": [p for p in pairs if p[2] < 0.4], "mid": [p for p in pairs if 0.4 <= p[2] < 0.85],
                 "high": [p for p in pairs if p[2] >= 0.85]}
        rnd = random.Random(seed)
        want = {"low": n // 4, "mid": n // 2, "high": n - n // 4 - n // 2}
        sample = [p for band, ps in bands.items() for p in rnd.sample(ps, min(len(ps), want[band]))]
        rows = []
        for i, (a, b, sc, f) in enumerate(sample, 1):
            prompt = (f"Identity A: {a.name} <{a.key}>\n{profile(s, a.id)}\n\n=====\n\n"
                      f"Identity B: {b.name} <{b.key}>\n{profile(s, b.id)}")
            out, _ = llm.structured(SYSTEM, prompt, SCHEMA, max_tokens=500)
            rows.append({"a_key": a.key, "b_key": b.key, "a_name": a.name, "b_name": b.name, "score": sc,
                         "shared_peers": f["shared_peers"], "label": int(out["same"]), "reviewed": 0,
                         "rationale": out["rationale"][:300]})
            if i % 20 == 0:
                print(f"  {i}/{len(sample)}", file=sys.stderr)
    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} pairs -> {OUT} ({sum(r['label'] for r in rows)} proposed same)")



def person_ids(s) -> dict[str, int]:
    """The node id of every Person key in this database."""
    return dict(s.execute(text("SELECT key, id FROM nodes WHERE type = 'Person'")).all())


def labeled(rows: list[dict]) -> tuple[list[dict], str]:
    """The rows to score, and which labels they carry: human-reviewed if any, else AI-reviewed, else drafts.
    `n/a` rows (not a person pair, e.g. mailing lists) are never scored."""
    rows = [r for r in rows if r["label"] in ("0", "1")]
    if rows and all(r.get("reviewer") == "generator" for r in rows):
        return rows, "ground truth (the synthetic persona's, by construction)"
    if reviewed := [r for r in rows if r["reviewed"] == "1"]:
        return reviewed, "human-reviewed"
    if ai := [r for r in rows if r.get("reviewer")]:
        return ai, f"AI-reviewed by {REVIEW_MODEL} (not human)"
    return rows, "model-proposed (NOT yet reviewed)"


def decisions(s) -> tuple[dict[frozenset, float], dict[str, int] | None]:
    """The resolver's score for every pair it considers, after its name rules (`candidates()`), and the identity
    head of every Person key once resolution has run (None before it has)."""
    pairs = {frozenset((a.key, b.key)): sc for a, b, sc, _ in candidates(s)}
    resolved = s.execute(text("""SELECT EXISTS (SELECT 1 FROM canonical c JOIN nodes n ON n.id = c.node_id
                                                WHERE n.type = 'Person')""")).scalar()
    heads = dict(s.execute(text("""SELECT n.key, COALESCE(c.canonical_id, n.id) FROM nodes n
                                   LEFT JOIN canonical c ON c.node_id = n.id WHERE n.type = 'Person'""")).all())
    return pairs, heads if resolved else None


def prf(rows: list[dict], same) -> dict:
    tp = sum(1 for r in rows if same(r) and r["label"] == "1")
    fp = sum(1 for r in rows if same(r) and r["label"] == "0")
    fn = sum(1 for r in rows if not same(r) and r["label"] == "1")
    p, rec = tp / max(1, tp + fp), tp / max(1, tp + fn)
    return {"pairs": len(rows), "precision": round(p, 3), "recall": round(rec, 3),
            "f1": round(2 * p * rec / max(1e-9, p + rec), 3), "tp": tp, "fp": fp, "fn": fn}


def score(thresholds=(THRESHOLD,), rows: list[dict] | None = None) -> dict:
    """P/R/F1 of the deployed clusters and of the pairwise decision at each threshold: on the pairs whose identities
    are both in this database ("present"), on every labeled pair with the others counted as not merged ("every
    labeled"), and on the present pairs the AI reviewer was confident about."""
    rows, which = labeled(rows if rows is not None else list(csv.DictReader(OUT.open())))
    with SessionLocal() as s:
        ids = person_ids(s)
        pairs, heads = decisions(s)
    present = [r for r in rows if r["a_key"] in ids and r["b_key"] in ids]
    subsets = {"present": present, "every labeled": rows}
    if which.startswith("AI"):
        subsets["reviewer confident"] = [r for r in present if "(unsure)" not in r["reviewer"]]

    def clustered(r: dict) -> bool:
        return r["a_key"] in ids and r["b_key"] in ids and heads[r["a_key"]] == heads[r["b_key"]]

    out = {"labels": which, "pairs_labeled": len(rows), "pairs_not_in_database": len(rows) - len(present), "results": []}
    for subset, use in subsets.items():
        if heads is not None:
            out["results"].append({"decision": "clusters", "subset": subset, **prf(use, clustered)})
        for t in thresholds:
            out["results"].append({"decision": f"pairs >= {t}", "subset": subset,
                                   **prf(use, lambda r, t=t: pairs.get(frozenset((r["a_key"], r["b_key"])), 0.0) >= t)})
    return out


def show(report: dict) -> None:
    print(f"{report['pairs_labeled']} {report['labels']} pairs")
    if report["pairs_not_in_database"]:
        print(f"{report['pairs_not_in_database']} pairs name an identity this database doesn't have (people that model "
              "extraction finds, keyed name:...): left out of 'present', counted as not merged in 'every labeled'")
    if not any(r["decision"] == "clusters" for r in report["results"]):
        print("no clusters yet: run `igneous pipeline` (its resolve stage) to score what the graph shows")
    for r in report["results"]:
        print(f"  {r['decision']:13s} {r['subset']:18s} {r['pairs']:4d} pairs  precision {r['precision']:.3f} "
              f"recall {r['recall']:.3f} F1 {r['f1']:.3f}  (tp {r['tp']} fp {r['fp']} fn {r['fn']})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft", help="sample pairs and propose labels (model calls)")
    d.add_argument("--n", type=int, default=240)
    d.add_argument("--seed", type=int, default=3)
    sub.add_parser("review", help="an independent second label for every pair (model calls)")
    sc = sub.add_parser("score", help="P/R/F1 of the resolver on this database (no model calls)")
    sc.add_argument("--threshold", type=float, default=THRESHOLD, help="the pairwise decision's threshold")
    sc.add_argument("--sweep", action="store_true", help=f"the pairwise decision at {', '.join(map(str, SWEEP))}")
    a = ap.parse_args()
    if a.cmd == "draft":
        draft(a.n, a.seed)
    elif a.cmd == "review":
        review()
    else:
        show(score(SWEEP if a.sweep else (a.threshold,)))
