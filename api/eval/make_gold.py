"""Draft the evaluation gold set from the corpus itself, then keep only questions we can verify.

Each answerable question is generated from ONE source email and must quote its answer verbatim from that
email (checked here); the gold evidence is that email's id. Unanswerable questions are about plausible
corpus subjects with facts the corpus doesn't contain. Review eval/questions.jsonl by hand before trusting it.

    uv run python eval/make_gold.py --n 60
"""
import argparse
import json
import random
import re
from pathlib import Path

from sqlalchemy import text

from igneous import llm
from igneous.db import SessionLocal
from igneous.rag.chunking import strip_quoted

OUT = Path(__file__).with_name("questions.jsonl")

SYSTEM = """You write evaluation questions for a search system over a person's email archive.

Given ONE email, write a question a user might really ask whose answer is in this email. The question must:
- be answerable from this email alone, and specific enough that this email is the obvious source;
- name people, projects, or subjects the way a colleague would (not "the email" or "this message");
- not copy the subject line.

`answer_quote` must be copied character-for-character from the email body: the shortest span that answers it.
If the email is boilerplate (disclaimers, newsletters, automated notices, jokes), set usable to false."""

SCHEMA = {
    "type": "object",
    "properties": {
        "usable": {"type": "boolean"},
        "question": {"type": "string"},
        "answer_quote": {"type": "string"},
        "kind": {"type": "string", "enum": ["lookup", "who", "when", "why"]},
    },
    "required": ["usable", "question", "answer_quote", "kind"],
    "additionalProperties": False,
}

MULTIHOP = """You write evaluation questions that need TWO emails from one thread to answer.

Email A raises something; email B (a reply from someone else) settles it. Write a question that:
- names the subject the way email A does, so a search for the question finds A;
- can only be answered with information that appears in B and NOT in A.

`answer_quote` must be copied character-for-character from email B. If B adds nothing A lacks, set usable
to false."""

UNANSWERABLE = """Write {n} questions a user might ask about this Enron email archive that it CANNOT answer:
plausible-sounding but about facts that no work email would contain (personal preferences, events after
2002, internal numbers never mentioned). Mention real names from this list so they look in-scope: {names}."""

UN_SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {"type": "string"}}},
    "required": ["questions"],
    "additionalProperties": False,
}


def needs_hop(question: str, parent_id: int, reply_id: int) -> bool:
    """A hard multi-hop question: search finds the parent (A) but not the reply (B) holding the answer.
    Only then does answering require following the graph edge from A to B."""
    from igneous.rag.search import hybrid

    with SessionLocal() as s:
        got = [h["source_item_id"] for h in hybrid(s, question, k=10, mode="dense")]
    return parent_id in got and reply_id not in got


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip().lower()


def main(n: int, n_unanswerable: int, n_multihop: int, seed: int, hard: bool = False, append: bool = False):
    with SessionLocal() as s:
        llm.require_whole_corpus(s)  # it sends sampled records to the model
        rows = s.execute(text("""
            SELECT si.id, si.title, si.body_text FROM nodes n JOIN source_items si ON si.id = n.source_item_id
            WHERE n.type = 'Message' AND length(si.body_text) BETWEEN 400 AND 4000
              AND si.title !~* '^(re|fw|fwd):' AND si.title <> ''
              AND si.title !~* 'demand ken lay donate'      -- 1,100 near-identical petitions: one is plenty
              AND si.body_text !~* 'unsubscribe|click here|newsletter|disclaimer|survey results'
        """)).all()
        names = s.execute(text("""
            SELECT n.name FROM nodes n JOIN edges e ON e.src = n.id AND e.type = 'SENT'
            WHERE n.type = 'Person' GROUP BY n.name ORDER BY count(*) DESC LIMIT 15
        """)).scalars().all()

    random.Random(seed).shuffle(rows)
    existing = [json.loads(line) for line in OUT.read_text().splitlines()] if append and OUT.exists() else []
    out, tried = [], 0
    for r in rows:
        if append or len(out) >= n:
            break
        tried += 1
        body = strip_quoted(r.body_text)
        if len(body) < 300:
            continue
        g, _ = llm.structured(SYSTEM, f"Subject: {r.title}\n\n{body[:4000]}", SCHEMA, max_tokens=800)
        if not g["usable"] or norm(g["answer_quote"]) not in norm(body) or len(g["answer_quote"]) < 3:
            continue
        out.append({"id": f"q{len(out) + 1:03d}", "question": g["question"], "kind": g["kind"],
                    "answerable": True, "gold_source_items": [r.id], "answer_quote": g["answer_quote"]})
        print(f"{len(out):3d}/{n}  {g['question'][:90]}")

    with SessionLocal() as s:
        pairs = s.execute(text("""
            SELECT pa.id AS a_id, pa.title AS a_title, pa.body_text AS a_body, ch.id AS b_id, ch.body_text AS b_body
            FROM edges r
            JOIN nodes c ON c.id = r.src JOIN source_items ch ON ch.id = c.source_item_id
            JOIN nodes p ON p.id = r.dst JOIN source_items pa ON pa.id = p.source_item_id
            WHERE r.type = 'REPLIES_TO' AND r.extractor = 'spine-quoted'
              AND length(pa.body_text) > 250 AND length(ch.body_text) > 250
        """)).all() if hard else s.execute(text("""
            WITH m AS (
                SELECT e.dst AS thread, si.id, si.title, si.body_text, si.occurred_at, snd.src AS sender
                FROM edges e JOIN nodes n ON n.id = e.src JOIN source_items si ON si.id = n.source_item_id
                JOIN edges snd ON snd.dst = n.id AND snd.type = 'SENT'
                WHERE e.type = 'IN_THREAD' AND length(si.body_text) > 250)
            SELECT DISTINCT ON (a.thread) a.id AS a_id, a.title AS a_title, a.body_text AS a_body,
                   b.id AS b_id, b.body_text AS b_body
            FROM m a JOIN m b ON a.thread = b.thread AND b.occurred_at > a.occurred_at AND b.sender <> a.sender
            ORDER BY a.thread, b.occurred_at
        """)).all()
    random.Random(seed).shuffle(pairs)
    n_multi = 0
    for p in pairs:
        if n_multi >= n_multihop:
            break
        a, b = strip_quoted(p.a_body)[:3000], strip_quoted(p.b_body)[:3000]
        if len(a) < 150 or len(b) < 150:
            continue
        g, _ = llm.structured(MULTIHOP, f"Email A (subject: {p.a_title}):\n{a}\n\n---\n\nEmail B:\n{b}",
                              SCHEMA, max_tokens=800)
        if not g["usable"] or norm(g["answer_quote"]) not in norm(b) or norm(g["answer_quote"]) in norm(a):
            continue
        if hard and not needs_hop(g["question"], p.a_id, p.b_id):
            continue
        n_multi += 1
        out.append({"id": f"q{len(existing) + len(out) + 1:03d}", "question": g["question"],
                    "kind": "multihop-hard" if hard else "multihop",
                    "answerable": True, "gold_source_items": [p.b_id], "bridge_source_items": [p.a_id],
                    "answer_quote": g["answer_quote"]})
        print(f"  multihop {n_multi}/{n_multihop}  {g['question'][:90]}")

    if append:
        OUT.write_text("".join(json.dumps(q) + "\n" for q in existing + out))
        print(f"appended {len(out)} questions -> {OUT} ({len(existing) + len(out)} total)")
        return
    un, _ = llm.structured("You write evaluation questions.", UNANSWERABLE.format(n=n_unanswerable,
                           names=", ".join(names)), UN_SCHEMA, max_tokens=2000)
    for q in un["questions"][:n_unanswerable]:
        out.append({"id": f"q{len(out) + 1:03d}", "question": q, "kind": "unanswerable", "answerable": False,
                    "gold_source_items": [], "answer_quote": None})

    OUT.write_text("".join(json.dumps(q) + "\n" for q in out))
    print(f"wrote {len(out)} questions ({len(out) - n_unanswerable} answerable incl. {n_multi} multihop) -> {OUT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--unanswerable", type=int, default=12)
    ap.add_argument("--multihop", type=int, default=15)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--hard", action="store_true", help="multi-hop only from verified replies search can't reach")
    ap.add_argument("--append", action="store_true", help="add to questions.jsonl instead of replacing it")
    a = ap.parse_args()
    main(a.n, a.unanswerable, a.multihop, a.seed, a.hard, a.append)
