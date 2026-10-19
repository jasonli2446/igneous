"""Entity-resolution labels and P/R/F1 (criterion 3).

    uv run python eval/er_labels.py draft --n 240   # sample pairs across the score range, model-proposed labels
    uv run python eval/er_labels.py score           # P/R/F1 of the resolver against eval/er_pairs.csv

`draft` writes eval/er_pairs.csv with a `label` column the model proposes from each person's sent mail and
signature lines, plus `reviewed=0`. These are PROPOSALS: criterion 3 requires human-verified labels, so a
teammate should review the rows (set label, set reviewed=1) before the number goes in the report. `score`
reports on reviewed rows if any exist, else on all rows, and says which.
"""
import argparse
import csv
import random
import sys
from pathlib import Path

from sqlalchemy import text

from igneous import llm
from igneous.db import SessionLocal
from igneous.graph.resolve import THRESHOLD, candidates, load, unresolvable
from igneous.graph.resolve import score as resolve_score

OUT = Path(__file__).with_name("er_pairs.csv")
FIELDS = ["a_id", "b_id", "a_key", "b_key", "a_name", "b_name", "score", "shared_peers", "label", "reviewed",
          "rationale", "draft_label", "reviewer", "review_rationale"]
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
        prompts = {id(r): (f"Identity A: {r['a_name']} <{r['a_key']}>\n{rich_profile(s, int(r['a_id']))}\n\n=====\n\n"
                           f"Identity B: {r['b_name']} <{r['b_key']}>\n{rich_profile(s, int(r['b_id']))}") for r in todo}

    def one(r):
        out, _ = llm.structured(REVIEW_SYSTEM, prompts[id(r)], REVIEW_SCHEMA, max_tokens=1500, effort="medium",
                                model=REVIEW_MODEL)
        return r, out

    def save():
        with OUT.open("w", newline="") as fh:
            w = csv.DictWriter(fh, FIELDS)
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
            rows.append({"a_id": a.id, "b_id": b.id, "a_key": a.key, "b_key": b.key, "a_name": a.name,
                         "b_name": b.name, "score": sc, "shared_peers": f["shared_peers"],
                         "label": int(out["same"]), "reviewed": 0, "rationale": out["rationale"][:300]})
            if i % 20 == 0:
                print(f"  {i}/{len(sample)}", file=sys.stderr)
    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} pairs -> {OUT} ({sum(r['label'] for r in rows)} proposed same)")


def rescore(rows: list[dict]) -> None:
    """Score each labeled pair with the CURRENT resolver, so tuning shows up without re-labeling."""
    with SessionLocal() as s:
        by_id = {p.id: p for p in load(s)}
    for r in rows:
        a, b = by_id.get(int(r["a_id"])), by_id.get(int(r["b_id"]))
        r["score"] = 0.0 if a is None or b is None or unresolvable(a) or unresolvable(b) else resolve_score(a, b)[0]


def score(threshold: float, rows: list[dict] | None = None):
    rows = rows if rows is not None else list(csv.DictReader(OUT.open()))
    rows = [r for r in rows if r["label"] in ("0", "1")]  # "n/a" = not a person pair (e.g. mailing lists)
    reviewed = [r for r in rows if r["reviewed"] == "1"]
    ai = [r for r in rows if r.get("reviewer")]
    if reviewed:
        use, which = reviewed, "human-reviewed"
    elif ai:
        use, which = ai, f"AI-reviewed by {REVIEW_MODEL} (not human)"
    else:
        use, which = rows, "model-proposed (NOT yet reviewed)"
    tp = sum(1 for r in use if float(r["score"]) >= threshold and r["label"] == "1")
    fp = sum(1 for r in use if float(r["score"]) >= threshold and r["label"] == "0")
    fn = sum(1 for r in use if float(r["score"]) < threshold and r["label"] == "1")
    p = tp / max(1, tp + fp)
    rec = tp / max(1, tp + fn)
    f1 = 2 * p * rec / max(1e-9, p + rec)
    print(f"{len(use)} {which} pairs @ threshold {threshold}: precision {p:.3f} recall {rec:.3f} F1 {f1:.3f}"
          f"  (tp {tp} fp {fp} fn {fn})")
    return p, rec, f1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--n", type=int, default=240)
    d.add_argument("--seed", type=int, default=3)
    sub.add_parser("review")
    sc = sub.add_parser("score")
    sc.add_argument("--threshold", type=float, default=THRESHOLD)
    sc.add_argument("--sweep", action="store_true")
    a = ap.parse_args()
    if a.cmd == "draft":
        draft(a.n, a.seed)
    elif a.cmd == "review":
        review()
    else:
        rows = list(csv.DictReader(OUT.open()))
        rescore(rows)
        for t in ((0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9) if a.sweep else (a.threshold,)):
            score(t, rows)
