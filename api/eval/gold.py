"""Gold records for the eval, keyed so they survive any rebuild of the database.

A record's key is {"source", "external_id", "fingerprint"}: its source, the id its connector gave it (an Enron email's
maildir path), and for email the content fingerprint the spine merges duplicate copies on. A local file's id starts
with a hash of the folder it was read from, which differs from one computer to the next, so a file is keyed by
{"source": "fs", "path": its path inside that folder} instead. Row ids are never stored, because every build numbers
rows differently. `resolve` turns keys into the rows of the database the eval runs on, every copy of a message
included: Enron files one message in several folders, and which copy the spine keeps and indexes depends on the
mailboxes synced.
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
    """The key of each source item id. A local file's key also carries its path below the folder it was read from."""
    ids = sorted({int(i) for i in item_ids})
    rows = s.execute(text("""
        SELECT id, source, external_id, raw_meta->>'fingerprint' AS fingerprint FROM source_items
        WHERE id = ANY(:ids)"""), {"ids": ids}).mappings() if ids else []
    out = {}
    for r in rows:
        key = {"source": r["source"], "external_id": r["external_id"], "fingerprint": r["fingerprint"]}
        if r["source"] == "fs" and "/" in r["external_id"]:
            key["path"] = r["external_id"].split("/", 1)[1]
        out[r["id"]] = key
    return out


def resolve(s, keys: list[dict]) -> list[set[int]]:
    """For each key, the source item ids it names in this database: the same source and external id, the same
    fingerprint when the key has one, or for a path key the file at that path below any folder. An empty set: the
    record isn't in this database."""
    if not keys:
        return []
    rows = s.execute(text("""
        WITH k AS (SELECT * FROM jsonb_to_recordset(CAST(:keys AS jsonb))
                   AS k(i int, source text, external_id text, fingerprint text, path text))
        SELECT k.i, si.id FROM k JOIN source_items si ON si.source = k.source AND si.external_id = k.external_id
        UNION
        SELECT k.i, si.id FROM k JOIN source_items si ON si.raw_meta->>'fingerprint' = k.fingerprint
        UNION
        SELECT k.i, si.id FROM k JOIN source_items si ON si.source = k.source AND k.path IS NOT NULL
            AND right(si.external_id, length(k.path) + 1) = '/' || k.path
    """), {"keys": json.dumps([{**key, "i": i} for i, key in enumerate(keys)])}).all()
    out: list[set[int]] = [set() for _ in keys]
    for i, item_id in rows:
        out[i].add(item_id)
    return out


def _below(key: dict) -> str | None:
    return key.get("path") or key.get("external_id")


def same(a: dict, b: dict) -> bool:
    """Whether two keys name the same record: the same source and external id, the same fingerprint, or (a path key)
    a file whose path below its folder ends with the other's."""
    if (a["source"], a.get("external_id")) == (b["source"], b.get("external_id")) and a.get("external_id"):
        return True
    if a.get("fingerprint") and a.get("fingerprint") == b.get("fingerprint"):
        return True
    if a["source"] != b["source"] or not (a.get("path") or b.get("path")):
        return False
    x, y = _below(a) or "", _below(b) or ""
    return x == y or x.endswith("/" + y) or y.endswith("/" + x)
