"""Verify every derived relationship against its evidence, instead of trusting the rule that proposed it.

Rule stages (threads, entities, organizations, resolve, history) are candidate generators: cheap, inclusive,
allowed to be wrong. This stage reads each derived edge's actual evidence and asks the model one question per
edge: does this evidence show the relationship? The answer is {verdict: keep | drop | unsure, confidence,
reason}. Nothing here is a rule about a particular kind of mistake; the same check catches a club mailing that
made someone "part of" a game, two different people merged by name, or a project filed under the wrong org.

Verdicts are stored in `edge_verdicts`, keyed by the edge's natural identity ('Type:key' of both ends, edge
type, extractor) so they survive stages that rebuild their edges every run, and by a hash of the evidence, so
an edge is re-judged only when its evidence changes. After each run the verdict is copied onto the live edge
(props.verdict / reason / confidence); traversal hides 'drop' and shows 'unsure' as a suggestion.

The user's corrections are verdicts too (source='user'): an unlink in the inspector writes 'drop', and the
model sees the user's most similar past corrections as examples, so a kind of mistake corrected once is
judged differently afterwards.

The judge reads only what may reach a model (#179, igneous/llm.py): evidence from opted-in sources, and only
edges whose two ends appear in such records. An edge it may see no evidence for is left as it is.
"""
import hashlib
import json
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings

VERSION = "1"
# Relationships derived by heuristics, and what they claim, phrased for the judge.
CLAIMS = {
    ("PART_OF", "threads"): "the item is part of this thread of the person's life (trip, class, job, project...)",
    ("INVOLVED_IN", "threads"): "this person took part in this thread (went on the trip, took the class, worked on "
                                "the project). Asking about it, hearing about it afterwards, or planning a different "
                                "one (another year) is NOT taking part",
    ("RELATED_TO", "threads"): "this person is connected to this thread (talked about it, helped, was told about it)",
    ("PART_OF", "entities"): "the item belongs to this project",
    ("INVOLVED_IN", "entities"): "this person works on or takes part in this project",
    ("WORKED_AT", "organizations"): "this person worked at / was a member of this organization",
    ("SAME_AS", "er"): "these two identities are the same human (judge from each one's name, identifier, who it "
                       "talks with and its topics; two addresses of one person rarely share messages)",
    ("INTRODUCED", "relationship-history"): "this person introduced the two people to each other",
    ("PART_OF", "hierarchy"): "this project / thread is part of (or run under) this larger program or organization",
    ("PART_OF", "backbone"): "this thread (a class, project or job) was done at or for this organization or school",
    ("RELATED_TO", "thread-resolution"): "these two threads are related as stated (e.g. interviews for, same client as)",
}
MAX_EVIDENCE = 6
SNIPPET = 280
BATCH = 12          # edges judged per model call (same claim type)
BUDGET = 400        # model calls per run; the rest wait for the next run


def ensure_table(s: Session) -> None:
    s.execute(text("""
        CREATE TABLE IF NOT EXISTS edge_verdicts (
            src_key    text NOT NULL,
            dst_key    text NOT NULL,
            type       text NOT NULL,
            extractor  text NOT NULL,
            evidence   text NOT NULL,          -- hash of the evidence the verdict was made on
            verdict    text NOT NULL CHECK (verdict IN ('keep', 'drop', 'unsure')),
            confidence real,
            reason     text,
            source     text NOT NULL DEFAULT 'model',   -- 'model' or 'user' (a correction; never overwritten)
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (src_key, dst_key, type, extractor)
        )"""))


def _edges(s: Session, pending_only: bool = True) -> list[dict]:
    """Derived edges with their natural keys and evidence (source, date, title, snippet per item)."""
    claims = [f"{t}|{x}" for t, x in CLAIMS]
    rows = s.execute(text("""
        SELECT e.id, e.type, e.extractor, e.props, a.id AS src_id, b.id AS dst_id,
               a.type || ':' || a.key AS src_key, a.name AS src_name,
               a.type AS src_type, b.type || ':' || b.key AS dst_key, b.name AS dst_name, b.type AS dst_type,
               b.props->>'kind' AS dst_kind,
               (SELECT array_agg(x ORDER BY x) FROM (
                   SELECT DISTINCT ev.source_item_id AS x FROM edge_evidence ev WHERE ev.edge_id = e.id
                   UNION SELECT e.source_item_id WHERE e.source_item_id IS NOT NULL) q) AS items
        FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst
        WHERE e.type || '|' || e.extractor = ANY(:claims)
    """), {"claims": claims}).mappings().all()
    return [dict(r) for r in rows]


def _profile(s: Session, natural: str, sources: frozenset[str]) -> str:
    """For "same person?": what one identity looks like on its own (name, address or number, who it talks to,
    what about), from opted-in sources. Two addresses of one human rarely share messages, so shared items are
    the wrong evidence."""
    nid = _id(s, natural)
    if nid is None:
        return "(missing)"
    n = s.execute(text("SELECT name, key, props->>'named_by' FROM nodes WHERE id = :i"), {"i": nid}).first()
    peers = s.execute(text("""
        WITH m AS (SELECT dst AS m FROM edges WHERE src = :i AND type = 'SENT'
                   UNION SELECT src FROM edges WHERE dst = :i AND type = 'RECEIVED'),
        shown AS (SELECT m.m FROM m JOIN nodes mn ON mn.id = m.m JOIN source_items si ON si.id = mn.source_item_id
                  WHERE si.source = ANY(CAST(:src AS text[])))
        SELECT p.name, count(*) FROM shown m
        JOIN edges x ON (x.dst = m.m AND x.type = 'SENT') OR (x.src = m.m AND x.type = 'RECEIVED')
        JOIN nodes p ON p.id = CASE WHEN x.type = 'SENT' THEN x.src ELSE x.dst END
        WHERE p.id <> :i AND p.type = 'Person' GROUP BY p.name ORDER BY 2 DESC LIMIT 5
    """), {"i": nid, "src": sorted(sources)}).all()
    subjects = s.execute(text("""
        SELECT DISTINCT left(si.title, 70) FROM edges e JOIN nodes m ON m.id = CASE WHEN e.type = 'SENT' THEN e.dst ELSE e.src END
        JOIN source_items si ON si.id = m.source_item_id
        WHERE ((e.src = :i AND e.type IN ('SENT', 'ATTENDED')) OR (e.dst = :i AND e.type = 'RECEIVED')) AND si.title <> ''
          AND si.source = ANY(CAST(:src AS text[]))
        LIMIT 5
    """), {"i": nid, "src": sorted(sources)}).scalars().all()
    card = " (from a contact card)" if n[2] == "contacts" else ""
    return (f"name '{n[0]}'{card}, identifier {n[1]}; talks most with: "
            + (", ".join(f"{p} ({c})" for p, c in peers) or "nobody recorded")
            + "; topics: " + ("; ".join(subjects) or "none recorded"))


def _evidence(s: Session, edge: dict, sources: frozenset[str]) -> tuple[list[dict], bool]:
    """What the judge reads, and whether records it may not see also exist. Item evidence when the edge has it;
    otherwise the items that connect both ends (shared messages/events for people, or the thread's items the
    person appears in). Only items from opted-in sources are shown."""
    ids = [i for i in (edge["items"] or []) if i]
    if not ids and edge["dst_type"] == "Project":
        # A membership with no recorded evidence: the project's own items this person is on (not anything they
        # share with its members elsewhere, which is how an unrelated work meeting got read as evidence).
        ids = list(s.execute(text("""
            WITH a AS (SELECT :a AS id UNION SELECT node_id FROM canonical WHERE canonical_id = :a)
            SELECT DISTINCT n.source_item_id FROM edges pe JOIN nodes n ON n.id = pe.src
            JOIN edges x ON (x.dst = n.id AND x.type IN ('SENT', 'ATTENDED') AND x.src IN (SELECT id FROM a))
                         OR (x.src = n.id AND x.type = 'RECEIVED' AND x.dst IN (SELECT id FROM a))
            WHERE pe.dst = :b AND pe.type = 'PART_OF' AND n.source_item_id IS NOT NULL LIMIT 40
        """), {"a": _id(s, edge["src_key"]), "b": _id(s, edge["dst_key"])}).scalars())
    if not ids:
        ids = list(s.execute(text("""
            WITH a AS (SELECT :a AS id UNION SELECT node_id FROM canonical WHERE canonical_id = :a),
            b AS (SELECT :b AS id UNION SELECT node_id FROM canonical WHERE canonical_id = :b),
            ia AS (SELECT dst AS m FROM edges WHERE src IN (SELECT id FROM a) AND type IN ('SENT', 'ATTENDED')
                   UNION SELECT src FROM edges WHERE dst IN (SELECT id FROM a) AND type = 'RECEIVED'),
            ib AS (SELECT dst AS m FROM edges WHERE src IN (SELECT id FROM b) AND type IN ('SENT', 'ATTENDED')
                   UNION SELECT src FROM edges WHERE dst IN (SELECT id FROM b) AND type = 'RECEIVED'
                   UNION SELECT src FROM edges WHERE dst IN (SELECT id FROM b) AND type = 'PART_OF')
            SELECT DISTINCT n.source_item_id FROM ia JOIN ib USING (m) JOIN nodes n ON n.id = ia.m
            WHERE n.source_item_id IS NOT NULL LIMIT 40
        """), {"a": _id(s, edge["src_key"]), "b": _id(s, edge["dst_key"])}).scalars())
    if not ids:
        return [], False
    rows = s.execute(text("""
        SELECT si.id, si.source, si.occurred_at, coalesce(si.title, '') AS title,
               left(coalesce(si.body_text, ''), 1200) AS body,
               (SELECT count(*) FROM jsonb_array_elements(COALESCE(si.raw_meta->'to', '[]') || COALESCE(si.raw_meta->'cc', '[]'))) AS recipients,
               EXISTS (SELECT 1 FROM source_items o WHERE o.id = ANY(:ids)
                       AND NOT (o.source = ANY(CAST(:src AS text[])))) AS unseen
        FROM source_items si WHERE si.id = ANY(:ids) AND si.source = ANY(CAST(:src AS text[]))
        ORDER BY si.occurred_at DESC NULLS LAST LIMIT :n
    """), {"ids": ids, "src": sorted(sources), "n": MAX_EVIDENCE}).mappings().all()
    # No row from an opted-in source means every one of these records is unseen.
    return [dict(r) for r in rows], (rows[0]["unseen"] if rows else True)


def _id(s: Session, natural: str) -> int | None:
    t, k = natural.split(":", 1)
    return s.execute(text("SELECT id FROM nodes WHERE type = :t AND key = :k"), {"t": t, "k": k}).scalar()


def _snippet(item: dict, terms: list[str]) -> str:
    """The part of the item that mentions the claim, not just its first lines."""
    body = re.sub(r"\s+", " ", item["body"])
    for t in terms:
        i = body.lower().find(t.lower())
        if i >= 0:
            return body[max(0, i - SNIPPET // 2): i + SNIPPET // 2]
    return body[:SNIPPET]


def _examples(s: Session, claim: tuple[str, str], sources: frozenset[str], k: int = 4) -> list[dict]:
    """The user's own corrections of this kind of relationship, most recent first: few-shot examples. Only
    corrections whose two ends appear in opted-in records."""
    from igneous.llm import visible_nodes

    rows = [dict(r) for r in s.execute(text("""
        SELECT v.src_key, v.dst_key, v.verdict, v.reason, a.id AS a, b.id AS b FROM edge_verdicts v
        JOIN nodes a ON a.type = split_part(v.src_key, ':', 1)
                    AND a.key = substr(v.src_key, length(split_part(v.src_key, ':', 1)) + 2)
        JOIN nodes b ON b.type = split_part(v.dst_key, ':', 1)
                    AND b.key = substr(v.dst_key, length(split_part(v.dst_key, ':', 1)) + 2)
        WHERE v.source = 'user' AND v.type = :t AND v.extractor = :x ORDER BY v.updated_at DESC LIMIT :n
    """), {"t": claim[0], "x": claim[1], "n": k * 5}).mappings()]
    seen = visible_nodes(s, [r["a"] for r in rows] + [r["b"] for r in rows], sources)
    return [r for r in rows if r["a"] in seen and r["b"] in seen][:k]


def _describe_identity(s: Session, edge: dict, sources: frozenset[str]) -> tuple[str, str]:
    head = f"A: {_profile(s, edge['src_key'], sources)}\nB: {_profile(s, edge['dst_key'], sources)}"
    return head, hashlib.sha256(head.encode()).hexdigest()


def _describe(edge: dict, evidence: list[dict], unseen: bool = False) -> tuple[str, str]:
    terms = [edge["dst_name"], edge["src_name"]]
    lines = [f"  - [{e['source']}, {e['occurred_at']:%Y-%m-%d}{f', {e['recipients']} recipients' if e['recipients'] else ''}] "
             f"{e['title'][:100]} :: {_snippet(e, terms)}" for e in evidence if e.get("occurred_at")]
    head = (f"A = {edge['src_type']} '{edge['src_name']}'; B = {edge['dst_type']} '{edge['dst_name']}'"
            + (f" ({edge['dst_kind']})" if edge.get("dst_kind") else ""))
    body = "\n".join(lines) or "  (no evidence items)"
    if unseen:
        body += "\n  (more records about them are in sources not shared with you)"
    digest = hashlib.sha256((head + body).encode()).hexdigest()
    return f"{head}\nEvidence:\n{body}", digest


def _judge_batch(s: Session, claim: tuple[str, str], batch: list[tuple[dict, str, str]],
                 sources: frozenset[str]) -> list[dict]:
    from igneous.llm import LLMDisabled, structured

    examples = _examples(s, claim, sources)
    ex = ""
    if examples:
        ex = "The person has corrected these before (follow the same judgment):\n" + "\n".join(
            f"  - {e['src_key'].split(':', 1)[-1]} -> {e['dst_key'].split(':', 1)[-1]}: {e['verdict']}"
            + (f" ({e['reason']})" if e.get("reason") else "") for e in examples) + "\n\n"
    prompt = ex + "\n\n".join(f"### Edge {i}\n{desc}" for i, (_e, desc, _d) in enumerate(batch))
    try:
        out, _ = structured(
            "You check relationships in someone's personal knowledge graph against the records that support them. "
            f"Claim for every edge below: A -> B means {CLAIMS[claim]}. For each edge decide from the evidence "
            "alone: keep (the records show it), drop (they don't: e.g. only a passing mention, a mass mailing, a "
            "coincidence of names), or unsure. Give a confidence 0-1 and one short reason a person would "
            "understand, naming the records ('3 emails with them planning the trip', 'only on a 40-person club "
            "newsletter'). Be strict: a relationship the records don't show is a drop. When an edge says more "
            "records are in sources not shared with you and the records shown don't settle it, answer unsure.",
            prompt,
            {"type": "object", "additionalProperties": False, "required": ["edges"], "properties": {"edges": {
                "type": "array", "items": {"type": "object", "additionalProperties": False,
                                           "required": ["edge", "verdict", "confidence", "reason"],
                                           "properties": {"edge": {"type": "integer"},
                                                          "verdict": {"enum": ["keep", "drop", "unsure"]},
                                                          "confidence": {"type": "number"},
                                                          "reason": {"type": "string"}}}}}},
            max_tokens=400 + 160 * len(batch), model=settings.llm_cheap_model)
    except LLMDisabled:
        return []
    except Exception:  # leave this batch unjudged; it's retried next run
        return []
    by_i = {r["edge"]: r for r in out.get("edges", [])}
    return [by_i.get(i, {"verdict": "unsure", "confidence": 0.0, "reason": "not judged"}) for i in range(len(batch))]


def apply_verdicts(s: Session) -> int:
    """Copy stored verdicts onto the live (possibly just rebuilt) edges, and take dropped ones out of the graph.

    A dropped edge moves to `edges_dropped` (kept for the why-view and audit) rather than being flagged, so every
    query in the system ignores it without having to know about verdicts."""
    ensure_table(s)
    s.execute(text("CREATE TABLE IF NOT EXISTS edges_dropped (LIKE edges INCLUDING DEFAULTS)"))
    moved = s.execute(text("""
        WITH d AS (
            DELETE FROM edges e USING nodes a, nodes b, edge_verdicts v
            WHERE a.id = e.src AND b.id = e.dst AND v.src_key = a.type || ':' || a.key
              AND v.dst_key = b.type || ':' || b.key AND v.type = e.type AND v.extractor = e.extractor
              AND v.verdict = 'drop'
              -- Undoing an identity merge needs a confident drop: a weak doubt must not split one person in two.
              AND (e.type <> 'SAME_AS' OR v.confidence >= 0.8)
            RETURNING e.*, v.reason AS _reason)
        INSERT INTO edges_dropped SELECT id, src, dst, type, props || jsonb_build_object('verdict', 'drop', 'reason', _reason),
               valid_from, valid_to, extractor, extractor_version, model, confidence, source_item_id, span_start, span_end
        FROM d
    """)).rowcount
    # "These two aren't related" covers every kind of derived link between them, either direction: rejecting
    # Igneous-RELATED_TO-Epic must also stop a later Igneous-PART_OF-Epic guess.
    moved += s.execute(text("""
        WITH d AS (
            DELETE FROM edges e USING nodes a, nodes b, edge_verdicts v
            WHERE a.id = e.src AND b.id = e.dst AND a.type = 'Project' AND b.type = 'Project'
              AND v.source = 'user' AND v.verdict = 'drop'
              AND ((v.src_key = a.type || ':' || a.key AND v.dst_key = b.type || ':' || b.key)
                   OR (v.src_key = b.type || ':' || b.key AND v.dst_key = a.type || ':' || a.key))
              AND e.extractor NOT IN ('user', 'profile')
            RETURNING e.*, v.reason AS _reason)
        INSERT INTO edges_dropped SELECT id, src, dst, type, props || jsonb_build_object('verdict', 'drop', 'reason', _reason),
               valid_from, valid_to, extractor, extractor_version, model, confidence, source_item_id, span_start, span_end
        FROM d ON CONFLICT DO NOTHING
    """)).rowcount
    return moved + s.execute(text("""
        UPDATE edges e SET props = e.props || jsonb_build_object('verdict', v.verdict, 'reason', v.reason,
                                                                  'confidence', v.confidence, 'verified_by', v.source)
        FROM nodes a, nodes b, edge_verdicts v
        WHERE a.id = e.src AND b.id = e.dst AND v.src_key = a.type || ':' || a.key
          AND v.dst_key = b.type || ':' || b.key AND v.type = e.type AND v.extractor = e.extractor
          AND (e.props->>'verdict' IS DISTINCT FROM v.verdict OR e.props->>'reason' IS DISTINCT FROM v.reason)
    """)).rowcount


def run(s: Session, log=print, budget: int = BUDGET) -> dict:
    """Judge every derived edge that has no verdict for its current evidence; then apply all verdicts."""
    from igneous.llm import allowed_sources, visible_nodes

    ensure_table(s)
    stats = {"judged": 0, "keep": 0, "drop": 0, "unsure": 0, "cached": 0, "calls": 0, "not_shared": 0}
    sources = allowed_sources()
    if not settings.llm_enabled or not sources:
        stats["applied"] = apply_verdicts(s)
        s.commit()
        return stats
    known = {(r[0], r[1], r[2], r[3]): (r[4], r[5]) for r in s.execute(text(
        "SELECT src_key, dst_key, type, extractor, evidence, source FROM edge_verdicts"))}
    todo: dict[tuple[str, str], list[tuple[dict, str, str]]] = {}
    edges = _edges(s)
    visible = visible_nodes(s, [e["src_id"] for e in edges] + [e["dst_id"] for e in edges], sources)
    for edge in edges:
        k = (edge["src_key"], edge["dst_key"], edge["type"], edge["extractor"])
        prior = known.get(k)
        if prior and prior[1] == "user":
            continue  # the user decided; never re-judged
        if edge["src_id"] not in visible or edge["dst_id"] not in visible:
            stats["not_shared"] += 1  # its ends appear only in sources the model may not read
            continue
        evidence, unseen = ([], False) if edge["type"] == "SAME_AS" else _evidence(s, edge, sources)
        if unseen and not evidence:
            stats["not_shared"] += 1  # every record behind it is in a source the model may not read
            continue
        if evidence and not edge["items"]:
            # A link proposed without records: what the judge reads becomes its evidence, so the why-view shows
            # what the verdict was made on instead of "no evidence".
            for e in evidence:
                s.execute(text("INSERT INTO edge_evidence (edge_id, source_item_id, kind, detail) "
                               "VALUES (:e, :s, 'verified-on', '{}')"), {"e": edge["id"], "s": e["id"]})
        desc, digest = (_describe_identity(s, edge, sources) if edge["type"] == "SAME_AS"
                        else _describe(edge, evidence, unseen))
        if prior and prior[0] == digest:
            stats["cached"] += 1
            continue
        todo.setdefault((edge["type"], edge["extractor"]), []).append((edge, desc, digest))
    # What people see first is judged first: who's in a thread, what sits under an org, which identities merge;
    # then the many item links. A run that hits its budget never leaves the people lists unchecked.
    order = {"INVOLVED_IN": 0, "SAME_AS": 1, "RELATED_TO": 2, "WORKED_AT": 3, "PART_OF": 4, "INTRODUCED": 5}
    for claim, items in sorted(todo.items(), key=lambda kv: (order.get(kv[0][0], 9), kv[0][1] != "hierarchy")):
        for i in range(0, len(items), BATCH):
            if stats["calls"] >= budget:
                break
            batch = items[i:i + BATCH]
            results = _judge_batch(s, claim, batch, sources)
            stats["calls"] += 1
            for (edge, _desc, digest), r in zip(batch, results, strict=False):
                s.execute(text("""
                    INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, confidence, reason)
                    VALUES (:a, :b, :t, :x, :d, :v, :c, :r)
                    ON CONFLICT (src_key, dst_key, type, extractor) DO UPDATE SET evidence = EXCLUDED.evidence,
                        verdict = EXCLUDED.verdict, confidence = EXCLUDED.confidence, reason = EXCLUDED.reason,
                        updated_at = now()
                    WHERE edge_verdicts.source <> 'user'
                """), {"a": edge["src_key"], "b": edge["dst_key"], "t": edge["type"], "x": edge["extractor"],
                       "d": digest, "v": r["verdict"], "c": r.get("confidence"), "r": (r.get("reason") or "")[:300]})
                stats["judged"] += 1
                stats[r["verdict"]] = stats.get(r["verdict"], 0) + 1
            s.commit()
    stats["applied"] = apply_verdicts(s)
    s.commit()
    if log:
        log(json.dumps(stats))
    return stats


def record_correction(s: Session, src_key: str, dst_key: str, type_: str, extractor: str, verdict: str,
                      reason: str | None = None) -> None:
    """A user's correction is a verdict that wins forever and teaches the judge (few-shot)."""
    ensure_table(s)
    s.execute(text("""
        INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, confidence, reason, source)
        VALUES (:a, :b, :t, :x, 'user', :v, 1.0, :r, 'user')
        ON CONFLICT (src_key, dst_key, type, extractor) DO UPDATE SET verdict = EXCLUDED.verdict,
            reason = EXCLUDED.reason, source = 'user', confidence = 1.0, updated_at = now()
    """), {"a": src_key, "b": dst_key, "t": type_, "x": extractor, "v": verdict, "r": reason})
