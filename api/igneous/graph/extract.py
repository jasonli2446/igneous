"""Stage two: model-derived MENTIONS (people, organizations) and ABOUT (topics), each with span provenance.

The model returns the exact quoted text for each mention; offsets are computed here by searching the source,
because models are unreliable at counting characters. A mention whose quote isn't found verbatim is dropped,
so every stored edge points at real text (criterion 2).
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import llm
from igneous.config import settings

EXTRACTOR = "llm-extract"
# Version two adds commitment and decision claims. It deliberately reprocesses prior
# opted-in items, while the graph writes remain idempotent by source-item/quote key.
VERSION = "2"
MIN_CHARS = 200     # short notes ("Thanks!", "See attached") aren't worth a call
MAX_CHARS = 6000
WORKERS = 4

SYSTEM = """You extract a knowledge graph from one email. Return:
- people: individuals named in the BODY (not just the From/To header) with what they are doing in it.
- organizations: companies, agencies, or groups named in the body.
- topics: 1-4 short noun phrases for what the email is about (e.g. "California price caps").
- commitments: explicit promises or obligations in the message. `who` is the person making the commitment
  when stated (otherwise "sender"), `what` is a concise description, and `by_when` is an explicit deadline
  or null. Never turn a question, wish, or vague intention into a commitment.
- decisions: explicit choices that have been made; `what` is the chosen outcome.

For every person, organization, commitment, and decision, `quote` must be copied EXACTLY from the body,
character for character, as the shortest evidence span. Do not include the sender's signature. Skip generic
references ("everyone", "the team") and do not infer facts not stated in the body."""

SCHEMA = {
    "type": "object",
    "properties": {
        "people": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string", "description": "full name as best known"},
            "quote": {"type": "string"},
            "role": {"type": "string", "description": "what they are doing in this email, a few words"},
        }, "required": ["name", "quote", "role"], "additionalProperties": False}},
        "organizations": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "quote": {"type": "string"},
        }, "required": ["name", "quote"], "additionalProperties": False}},
        "topics": {"type": "array", "items": {"type": "string"}},
        "commitments": {"type": "array", "items": {"type": "object", "properties": {
            "who": {"type": "string"}, "what": {"type": "string"},
            "by_when": {"type": ["string", "null"]}, "quote": {"type": "string"},
        }, "required": ["who", "what", "by_when", "quote"], "additionalProperties": False}},
        "decisions": {"type": "array", "items": {"type": "object", "properties": {
            "what": {"type": "string"}, "quote": {"type": "string"},
        }, "required": ["what", "quote"], "additionalProperties": False}},
    },
    "required": ["people", "organizations", "topics", "commitments", "decisions"],
    "additionalProperties": False,
}


def locate(body: str, quote: str) -> tuple[int, int] | None:
    """Exact match first, then whitespace-insensitive (email bodies wrap lines mid-name)."""
    if not quote.strip():
        return None
    i = body.find(quote)
    if i >= 0:
        return i, i + len(quote)
    pattern = r"\s+".join(re.escape(w) for w in quote.split())
    m = re.search(pattern, body, flags=re.IGNORECASE)
    return (m.start(), m.end()) if m else None


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _topic_key(name: str) -> str:
    # Keep topic vocabulary stable from the first extraction; broader duplicate detection lives in topics.py.
    from igneous.graph.topics import normalize_key

    return normalize_key(name)


def extract_one(body: str) -> tuple[dict, dict]:
    return llm.structured(SYSTEM, f"Email body:\n\n{body}", SCHEMA, max_tokens=3000)


UPSERT_NODE = text("""
INSERT INTO nodes (type, key, name, props) VALUES (:type, :key, :name, CAST(:props AS jsonb))
ON CONFLICT (type, key) DO UPDATE SET name = nodes.name RETURNING id
""")
INSERT_EDGE = text("""
INSERT INTO edges (src, dst, type, props, valid_from, extractor, extractor_version, model, confidence,
                   source_item_id, span_start, span_end)
VALUES (:src, :dst, :type, CAST(:props AS jsonb), :at, :extractor, :version, :model, :conf, :sid, :s0, :s1)
ON CONFLICT (src, dst, type, extractor) DO NOTHING
""")


def store(s: Session, item, body: str, out: dict) -> dict:
    """Write one item's extraction. Mentions attach to the Message node; people resolve to an existing Person
    by exact name when there is exactly one (cheap ER; the real resolver is #entity-resolution)."""
    stats = {"mentions": 0, "dropped": 0, "topics": 0, "commitments": 0, "decisions": 0, "dropped_claims": 0}
    common = {"at": item.occurred_at, "extractor": EXTRACTOR, "version": VERSION, "model": settings.llm_model,
              "sid": item.id}
    for kind, typ in (("people", "Person"), ("organizations", "Organization")):
        for m in out[kind]:
            span = locate(body, m["quote"])
            if span is None:
                stats["dropped"] += 1
                continue
            name = _norm(m["name"])
            target = None
            if typ == "Person":
                ids = s.execute(text("SELECT id FROM nodes WHERE type = 'Person' AND lower(name) = lower(:n) LIMIT 2"),
                                {"n": name}).scalars().all()
                target = ids[0] if len(ids) == 1 else None
            if target is None:
                target = s.execute(UPSERT_NODE, {"type": typ, "key": f"name:{_key(name)}", "name": name,
                                                 "props": json.dumps({"origin": EXTRACTOR})}).scalar_one()
            s.execute(INSERT_EDGE, {**common, "src": item.node_id, "dst": target, "type": "MENTIONS",
                                    "props": json.dumps({"role": m.get("role"), "quote": m["quote"]}),
                                    "conf": 0.9 if body[span[0]:span[1]] == m["quote"] else 0.7,
                                    "s0": span[0], "s1": span[1]})
            stats["mentions"] += 1
    for t in out["topics"][:4]:
        name = _norm(t)
        if not name:
            continue
        tid = s.execute(UPSERT_NODE, {"type": "Topic", "key": _topic_key(name), "name": name,
                                      "props": json.dumps({"origin": EXTRACTOR})}).scalar_one()
        # ABOUT applies to the whole item, so its span is the whole body.
        s.execute(INSERT_EDGE, {**common, "src": item.node_id, "dst": tid, "type": "ABOUT", "props": "{}",
                                "conf": 0.8, "s0": 0, "s1": len(body)})
        stats["topics"] += 1
    from igneous.graph.history import store_claims
    for key, value in store_claims(s, item, body, out, settings.llm_model).items():
        stats[key] += value
    return stats


def run(s: Session, limit: int = 200, log=print) -> int:
    """Extract the newest opted-in source items. Empty ``IGNEOUS_LLM_SOURCES`` means no automatic model calls."""
    sources = llm.allowed_sources()
    if not sources:
        log("  extraction skipped: set IGNEOUS_LLM_SOURCES to opt in a source")
        return 0
    rows = s.execute(text("""
        SELECT si.id, si.body_text, si.occurred_at, n.id AS node_id
        FROM nodes n JOIN source_items si ON si.id = n.source_item_id
        WHERE n.type = 'Message' AND si.source = ANY(CAST(:sources AS text[])) AND length(si.body_text) >= :min
          -- automated Gmail (newsletters, notifications) isn't worth a model call
          AND NOT (si.raw_meta->'labels' ?| ARRAY['CATEGORY_PROMOTIONS', 'CATEGORY_SOCIAL', 'CATEGORY_UPDATES',
                                                  'CATEGORY_FORUMS'])
          AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.src = n.id AND e.extractor = :ex
                                                 AND e.extractor_version = :v)
          AND NOT (si.raw_meta ? ('extracted_v' || :v))
        ORDER BY si.occurred_at DESC NULLS LAST LIMIT :limit
    """), {"sources": sorted(sources), "min": MIN_CHARS, "ex": EXTRACTOR, "v": VERSION, "limit": limit}).all()
    if not rows:
        return 0
    from igneous.rag.chunking import QUOTE_START

    def own_text(body: str) -> str:
        # A prefix of the stored body_text (no strip), so offsets computed on it index body_text exactly.
        m = QUOTE_START.search(body)
        return (body[: m.start()] if m else body)[:MAX_CHARS]

    bodies = {r.id: own_text(r.body_text) for r in rows}
    totals = {"mentions": 0, "dropped": 0, "topics": 0, "commitments": 0, "decisions": 0,
              "dropped_claims": 0, "input_tokens": 0, "output_tokens": 0, "failed": 0}
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {r.id: pool.submit(extract_one, bodies[r.id].strip()) for r in rows
                   if len(bodies[r.id].strip()) >= MIN_CHARS}
        for r in rows:
            f = futures.get(r.id)
            if f is not None:
                try:
                    out, usage = f.result()
                except Exception as e:  # one bad item shouldn't sink the batch
                    totals["failed"] += 1
                    log(f"  item {r.id}: {type(e).__name__}: {str(e)[:100]}")
                    continue
                for k, v in store(s, r, bodies[r.id], out).items():
                    totals[k] += v
                for k in ("input_tokens", "output_tokens"):
                    totals[k] += usage[k]
            # Mark processed even when nothing was found, so it isn't retried every run.
            s.execute(text("UPDATE source_items SET raw_meta = raw_meta || CAST(:m AS jsonb) WHERE id = :id"),
                      {"m": json.dumps({f"extracted_v{VERSION}": True}), "id": r.id})
            s.commit()
    # Conservative and cached: only explicit later confirmation can resolve a derived promise.
    from igneous.graph.history import refresh_commitment_status
    totals["resolved"] = refresh_commitment_status(s)
    s.commit()
    log(f"  {totals}")
    return len(rows)
