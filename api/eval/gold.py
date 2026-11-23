"""Gold records for the eval, keyed so they survive any rebuild of the database.

A record's key is {"source", "external_id", "fingerprint"}: its source, the id its connector gave it (an Enron email's
maildir path), and for email the content fingerprint the spine merges duplicate copies on. Row ids are never stored,
because every build numbers rows differently. `resolve` turns keys into the rows of the database the eval runs on,
every copy of a message included: Enron files one message in several folders, and which copy the spine keeps and
indexes depends on the mailboxes synced.
"""
import json
from pathlib import Path

from sqlalchemy import text

QUESTIONS = Path(__file__).with_name("questions.jsonl")


def load(path: Path = QUESTIONS) -> list[dict]:
    """The questions: id, question, kind (lookup, who, when, why, multihop, multihop-hard, unanswerable),
    answerable, gold (keys of the records that hold the answer), bridge (multi-hop: the record the question is
    asked in terms of) and answer_quote (copied verbatim from the gold record)."""
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def keys_of(s, item_ids) -> dict[int, dict]:
    """The key of each source item id."""
    ids = sorted({int(i) for i in item_ids})
    rows = s.execute(text("""
        SELECT id, source, external_id, raw_meta->>'fingerprint' AS fingerprint FROM source_items
        WHERE id = ANY(:ids)"""), {"ids": ids}).mappings() if ids else []
    return {r["id"]: {"source": r["source"], "external_id": r["external_id"], "fingerprint": r["fingerprint"]}
            for r in rows}


def resolve(s, keys: list[dict]) -> list[set[int]]:
    """For each key, the source item ids it names in this database: the same source and external id, or the same
    fingerprint when the key has one. An empty set: the record isn't in this database."""
    if not keys:
        return []
    rows = s.execute(text("""
        WITH k AS (SELECT * FROM jsonb_to_recordset(CAST(:keys AS jsonb))
                   AS k(i int, source text, external_id text, fingerprint text))
        SELECT k.i, si.id FROM k JOIN source_items si ON si.source = k.source AND si.external_id = k.external_id
        UNION
        SELECT k.i, si.id FROM k JOIN source_items si ON si.raw_meta->>'fingerprint' = k.fingerprint
    """), {"keys": json.dumps([{**key, "i": i} for i, key in enumerate(keys)])}).all()
    out: list[set[int]] = [set() for _ in keys]
    for i, item_id in rows:
        out[i].add(item_id)
    return out


def same(a: dict, b: dict) -> bool:
    """Whether two keys name the same record: the same source and external id, or the same fingerprint."""
    if (a["source"], a["external_id"]) == (b["source"], b["external_id"]):
        return True
    return bool(a.get("fingerprint")) and a.get("fingerprint") == b.get("fingerprint")
