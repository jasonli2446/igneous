"""Verify every derived relationship against its evidence, instead of trusting the rule that proposed it.

Rule stages (threads, entities, organizations, resolve, history) are candidate generators: cheap, inclusive,
allowed to be wrong. This stage reads each derived edge's actual evidence and asks the model one question per
edge: does this evidence show the relationship? The answer is {verdict: keep | drop | unsure, confidence,
reason}. Nothing here is a rule about a particular kind of mistake; the same check catches a club mailing that
made someone "part of" a game, two different people merged by name, or a project filed under the wrong org.

Verdicts are stored in `edge_verdicts`, keyed by the edge's natural identity ('Type:key' of both ends, edge type,
extractor) so they survive stages that rebuild their edges every run, and by a digest of exactly what the judge read
(its instructions with the claim, the edge's description and its evidence), so an edge is judged again when its
evidence or the prompt changes and never otherwise. They are read as the graph is keyed now (`_verdicts`): a verdict
on a link to an organization by a key another organization has now is that organization's link's (graph/derived.py
`rekeyed`), and of two verdicts on one link, yours wins. Each batch is stored the moment the model answers, in a
transaction of its own, so a rebuild that fails later loses its graph write but never a paid judgment. Only the
verdicts the model returned are stored; an edge it left out is asked again next run. An 'unsure' from the cheap model
is asked once more of the strong model, once per evidence change.

Some links need no model: a person in a thread's own group chat, or at its events, took part in it (the record
lists them), so those links (props.direct) get a settled 'keep' (source 'evidence') whether or not a model may read
anything. The user's corrections are verdicts too (source 'user'; graph/corrections.py): never judged again, and the
model sees the user's most similar past corrections of the same claim as examples, so a kind of mistake corrected once
is judged differently afterwards.

`apply_verdicts` puts them on the graph: no derived link joins a pair you rejected (graph/derived.py), a link the
verifier dropped moves to `edges_dropped` (one row per natural key, deleted with its verdict), and every other verdict
rides on its link (props.verdict, reason, confidence, verified_by), where the explorer reads it.

The judge reads only what may reach a model (#179, igneous/llm.py): evidence from opted-in sources, and only edges
whose two ends a prompt may name. An edge it may see no evidence for is left as it is.
"""
import hashlib
import json
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings
from igneous.graph.derived import STATED

# Relationships derived by heuristics, and what each claims, phrased for the judge. A third key part names a variant
# by the kind of node A is.
CLAIMS = {
    ("PART_OF", "threads"): "the item is part of this thread of the person's life (trip, class, job, project...)",
    ("PART_OF", "threads", "group"): "this group chat or calendar series (A) is about this thread of the person's life "
                                     "(B): it was where the thread was planned or held, not just a chat or event "
                                     "that mentioned it once",
    ("INVOLVED_IN", "threads"): "this person took part in this thread (went on the trip, took the class, worked on "
                                "the project). Asking about it, hearing about it afterwards, or planning a different "
                                "one (another year) is NOT taking part",
    ("RELATED_TO", "threads"): "this person is connected to this thread (talked about it, helped, was told about it)",
    ("PART_OF", "entities"): "the item belongs to this project",
    ("INVOLVED_IN", "entities"): "this person works on or takes part in this project",
    ("WORKED_AT", "organizations"): "this person worked at / was a member of this organization",
    ("SAME_AS", "er"): "these two identities are the same human (judge from each one's name, identifier, who it "
                       "talks with and its topics; two addresses of one person rarely share messages)",
    ("INTRODUCED", "relationship-history"): "A and B were introduced to each other by the introducer named with the "
                                            "edge: the introducer wrote to both of them together, and A and B had not "
                                            "talked before",
    ("PART_OF", "hierarchy"): "this project / thread is part of (or run under) this larger program or organization",
    ("PART_OF", "backbone"): "this thread (a class, project or job) was done at or for this organization or school",
    ("PART_OF", "backbone", "role"): "this thread (a class, project or piece of work) was done in this role (B: a "
                                     "position or degree the person held at an organization, as their resume or "
                                     "LinkedIn lists it), not merely at the same time as it",
    ("RELATED_TO", "thread-resolution"): "these two threads are related as stated (e.g. interviews for, same client as)",
}
MAX_EVIDENCE = 6
SNIPPET = 280
BATCH = 12          # edges judged per model call (same claim)
BUDGET = 400        # model calls per run (cheap and strong); the rest wait for the next run
EXAMPLES = 4        # your past corrections shown as examples per call


def claim_of(edge: dict) -> tuple:
    """The claim an edge makes: (type, extractor), or its variant for the kind of node at A (a group chat or series
    about a thread) or at B (a role, for a thread the backbone placed under one)."""
    if (edge["type"], edge["extractor"]) == ("PART_OF", "threads") and edge["src_type"] in ("Thread", "Event"):
        return ("PART_OF", "threads", "group")
    if (edge["type"], edge["extractor"]) == ("PART_OF", "backbone") and edge.get("dst_kind") == "role":
        return ("PART_OF", "backbone", "role")
    return (edge["type"], edge["extractor"])


def system(claim: tuple) -> str:
    """The judge's instructions for one claim."""
    return ("You check relationships in someone's personal knowledge graph against the records that support them. "
            f"Claim for every edge below: A -> B means {CLAIMS[claim]}. For each edge decide from the evidence "
            "alone: keep (the records show it), drop (they don't: e.g. only a passing mention, a mass mailing, a "
            "coincidence of names), or unsure. Give a confidence 0-1 and one short reason a person would "
            "understand, naming the records ('3 emails with them planning the trip', 'only on a 40-person club "
            "newsletter'). Be strict: a relationship the records don't show is a drop. When an edge says more "
            "records are in sources not shared with you and the records shown don't settle it, answer unsure. "
            "Answer for every edge, by its number.")


SCHEMA = {"type": "object", "additionalProperties": False, "required": ["edges"], "properties": {"edges": {
    "type": "array", "items": {"type": "object", "additionalProperties": False,
                               "required": ["edge", "verdict", "confidence", "reason"],
                               "properties": {"edge": {"type": "integer"},
                                              "verdict": {"enum": ["keep", "drop", "unsure"]},
                                              "confidence": {"type": "number"},
                                              "reason": {"type": "string"}}}}}}


def _edges(s: Session) -> list[dict]:
    """Derived edges with their natural keys and evidence item ids, in a stable order."""
    claims = sorted({f"{c[0]}|{c[1]}" for c in CLAIMS})
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
        ORDER BY e.type, e.extractor, src_key, dst_key
    """), {"claims": claims}).mappings().all()
    return [dict(r) for r in rows]


def _id(s: Session, natural: str) -> int | None:
    return s.execute(text("SELECT id FROM nodes WHERE type || ':' || key = :k"), {"k": natural}).scalar()


def _profile(s: Session, natural: str, sources: frozenset[str]) -> str:
    """For "same person?": what one identity looks like on its own (name, address or number, who it talks to,
    what about), from opted-in sources. Two addresses of one human rarely share messages, so shared items are
    the wrong evidence. Only names a prompt may use (llm.nameable)."""
    from igneous.llm import nameable

    nid = _id(s, natural)
    if nid is None:
        return "(missing)"
    n = s.execute(text("SELECT key, props->>'named_by' FROM nodes WHERE id = :i"), {"i": nid}).first()
    peers = s.execute(text("""
        WITH m AS (SELECT dst AS m FROM edges WHERE src = :i AND type = 'SENT'
                   UNION SELECT src FROM edges WHERE dst = :i AND type = 'RECEIVED'),
        shown AS (SELECT m.m FROM m JOIN nodes mn ON mn.id = m.m JOIN source_items si ON si.id = mn.source_item_id
                  WHERE si.source = ANY(CAST(:src AS text[])))
        SELECT p.id, count(*) FROM shown m
        JOIN edges x ON (x.dst = m.m AND x.type = 'SENT') OR (x.src = m.m AND x.type = 'RECEIVED')
        JOIN nodes p ON p.id = CASE WHEN x.type = 'SENT' THEN x.src ELSE x.dst END
        WHERE p.id <> :i AND p.type = 'Person' GROUP BY p.id ORDER BY 2 DESC, p.id LIMIT 10
    """), {"i": nid, "src": sorted(sources)}).all()
    names = nameable(s, [nid, *(p for p, _c in peers)], sources)
    subjects = s.execute(text("""
        SELECT DISTINCT left(si.title, 70) FROM edges e JOIN nodes m ON m.id = CASE WHEN e.type = 'SENT' THEN e.dst ELSE e.src END
        JOIN source_items si ON si.id = m.source_item_id
        WHERE ((e.src = :i AND e.type IN ('SENT', 'ATTENDED')) OR (e.dst = :i AND e.type = 'RECEIVED')) AND si.title <> ''
          AND si.source = ANY(CAST(:src AS text[]))
        ORDER BY 1 LIMIT 5
    """), {"i": nid, "src": sorted(sources)}).scalars().all()
    card = " (from a contact card)" if n[1] == "contacts" else ""
    talks = [f"{names[p]} ({c})" for p, c in peers if p in names][:5]
    return (f"name '{names.get(nid, '(not shared)')}'{card}, identifier {n[0]}; talks most with: "
            + (", ".join(talks) or "nobody recorded") + "; topics: " + ("; ".join(subjects) or "none recorded"))


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
            WHERE pe.dst = :b AND pe.type = 'PART_OF' AND n.source_item_id IS NOT NULL
            ORDER BY n.source_item_id DESC LIMIT 40
        """), {"a": edge["src_id"], "b": edge["dst_id"]}).scalars())
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
            WHERE n.source_item_id IS NOT NULL ORDER BY n.source_item_id DESC LIMIT 40
        """), {"a": edge["src_id"], "b": edge["dst_id"]}).scalars())
    if not ids:
        return [], False
    rows = s.execute(text("""
        SELECT si.id, si.source, si.occurred_at, coalesce(si.title, '') AS title,
               left(coalesce(si.body_text, ''), 1200) AS body,
               (SELECT count(*) FROM jsonb_array_elements(COALESCE(si.raw_meta->'to', '[]') || COALESCE(si.raw_meta->'cc', '[]'))) AS recipients,
               EXISTS (SELECT 1 FROM source_items o WHERE o.id = ANY(:ids)
                       AND NOT (o.source = ANY(CAST(:src AS text[])))) AS unseen
        FROM source_items si WHERE si.id = ANY(:ids) AND si.source = ANY(CAST(:src AS text[]))
        ORDER BY si.occurred_at DESC NULLS LAST, si.id DESC LIMIT :n
    """), {"ids": ids, "src": sorted(sources), "n": MAX_EVIDENCE}).mappings().all()
    # No row from an opted-in source means every one of these records is unseen.
    return [dict(r) for r in rows], (rows[0]["unseen"] if rows else True)


def _snippet(item: dict, terms: list[str]) -> str:
    """The part of the item that mentions the claim, not just its first lines."""
    body = re.sub(r"\s+", " ", item["body"])
    for t in terms:
        i = body.lower().find(t.lower())
        if i >= 0:
            return body[max(0, i - SNIPPET // 2): i + SNIPPET // 2]
    return body[:SNIPPET]


def _examples(s: Session, claim: tuple, batch: list[dict], sources: frozenset[str], k: int = EXAMPLES) -> list[str]:
    """Your own corrections of this claim, the most similar to the batch first (about the same thing or person, then
    the most recent), as lines a prompt may show: both ends named (llm.nameable), with the reason you gave."""
    from igneous.graph.derived import rekeyed
    from igneous.llm import nameable

    verdicts, keyed = _verdicts(rekeyed(s))
    rows = [dict(r) for r in s.execute(text(f"""
        SELECT v.src_key, v.dst_key, v.verdict, v.reason, a.id AS a, b.id AS b FROM {verdicts} v
        JOIN nodes a ON a.type || ':' || a.key = v.src_key JOIN nodes b ON b.type || ':' || b.key = v.dst_key
        WHERE v.source = 'user' AND v.type = :t AND v.extractor = :x ORDER BY v.updated_at DESC, v.src_key LIMIT :n
    """), {"t": claim[0], "x": claim[1], "n": k * 10, **keyed}).mappings()]
    near = {e["dst_key"] for e in batch} | {e["src_key"] for e in batch}
    rows.sort(key=lambda r: (r["dst_key"] not in near and r["src_key"] not in near))
    names = nameable(s, [r["a"] for r in rows] + [r["b"] for r in rows], sources)
    return [f"  - {names[r['a']]} -> {names[r['b']]}: {r['verdict']}" + (f" ({r['reason']})" if r.get("reason") else "")
            for r in rows if r["a"] in names and r["b"] in names][:k]


def _describe_identity(s: Session, edge: dict, sources: frozenset[str]) -> str:
    return f"A: {_profile(s, edge['src_key'], sources)}\nB: {_profile(s, edge['dst_key'], sources)}"


def _describe(edge: dict, evidence: list[dict], unseen: bool = False, names: dict[int, str] | None = None) -> str:
    """One edge as the judge reads it: both ends by the names a prompt may use, the introducer of an introduction,
    and its evidence."""
    names = names or {}
    src, dst = names.get(edge["src_id"], edge["src_name"]), names.get(edge["dst_id"], edge["dst_name"])
    lines = [f"  - [{e['source']}, {e['occurred_at']:%Y-%m-%d}{f', {e['recipients']} recipients' if e['recipients'] else ''}] "
             f"{e['title'][:100]} :: {_snippet(e, [dst, src])}" for e in evidence if e.get("occurred_at")]
    head = (f"A = {edge['src_type']} '{src}'; B = {edge['dst_type']} '{dst}'"
            + (f" ({edge['dst_kind']})" if edge.get("dst_kind") else ""))
    introducer = (edge.get("props") or {}).get("introducer")
    if edge["type"] == "INTRODUCED" and introducer is not None:
        head += f"; introducer = '{names.get(int(introducer), '(not shared)')}'"
    body = "\n".join(lines) or "  (no evidence items)"
    if unseen:
        body += "\n  (more records about them are in sources not shared with you)"
    return f"{head}\nEvidence:\n{body}"


def digest(claim: tuple, desc: str) -> str:
    """What a verdict was made on: the judge's instructions for the claim and the edge as described."""
    return hashlib.sha256((system(claim) + "\n" + desc).encode()).hexdigest()


def _judge_batch(s: Session, claim: tuple, batch: list[tuple[dict, str, str]], sources: frozenset[str],
                 model: str | None) -> dict[int, dict]:
    """One model call for a batch of edges of one claim: {index in batch: verdict} for every edge the model answered.
    An answer that numbers edges outside the batch, or one edge twice, is discarded whole (its verdicts can't be
    matched to their edges); a failed call answers nothing. Unanswered edges are asked again next run."""
    from igneous.llm import LLMDisabled, structured

    examples = _examples(s, claim, [e for e, _d, _g in batch], sources)
    ex = ("The person has corrected these before (follow the same judgment):\n" + "\n".join(examples) + "\n\n"
          if examples else "")
    prompt = ex + "\n\n".join(f"### Edge {i}\n{desc}" for i, (_e, desc, _d) in enumerate(batch))
    try:
        out, _ = structured(system(claim), prompt, SCHEMA, max_tokens=400 + 160 * len(batch), model=model)
    except LLMDisabled:
        return {}
    except Exception:  # leave this batch unjudged; it's asked again next run
        return {}
    answers = out.get("edges") or []
    numbers = [a.get("edge") for a in answers]
    if len(set(numbers)) != len(numbers) or any(not isinstance(i, int) or not 0 <= i < len(batch) for i in numbers):
        return {}
    return {a["edge"]: a for a in answers if a.get("verdict") in ("keep", "drop", "unsure")}


UPSERT_VERDICT = text("""
    INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, confidence, reason, source)
    VALUES (:a, :b, :t, :x, :d, :v, :c, :r, :src)
    ON CONFLICT (src_key, dst_key, type, extractor) DO UPDATE SET evidence = EXCLUDED.evidence,
        verdict = EXCLUDED.verdict, confidence = EXCLUDED.confidence, reason = EXCLUDED.reason,
        source = EXCLUDED.source, updated_at = now()
    WHERE edge_verdicts.source <> 'user'
""")


FORGET_VERDICT = text("""DELETE FROM edge_verdicts WHERE src_key = :a AND dst_key = :b AND type = :t
                          AND extractor = :x AND source = 'evidence'""")


def _store(rows: list[dict], forget: list[dict] = ()) -> None:
    """Store verdicts now, in a transaction of its own (like llm.cache_store), never in the rebuild's: a paid judgment
    outlives a rebuild that fails or is killed later. `forget`: settled verdicts whose direct evidence is gone. Rows
    are written in natural-key order, the order a correction writes its verdicts in (`record_correction`), so the two
    never deadlock."""
    from igneous.db import SessionLocal

    writes = sorted([(_order(r), UPSERT_VERDICT, {"src": "model", **r}) for r in rows]
                    + [(_order(f), FORGET_VERDICT, f) for f in forget], key=lambda w: w[0])
    with SessionLocal() as vs:
        for _key, statement, params in writes:
            vs.execute(statement, params)
        vs.commit()


def _verdicts(moved: dict[str, str]) -> tuple[str, dict]:
    """`edge_verdicts` as the graph is keyed now, for a FROM clause, with its parameters: a verdict recorded under a
    key that stands for another now (`moved`, graph/derived.py rekeyed) is that key's, and of two verdicts on one link
    yours wins, then the latest."""
    if not moved:
        return "edge_verdicts", {}
    return """(SELECT DISTINCT ON (src_key, dst_key, type, extractor) * FROM (
                   SELECT COALESCE(ma.new, v.src_key) AS src_key, COALESCE(mb.new, v.dst_key) AS dst_key, v.type,
                          v.extractor, v.evidence, v.verdict, v.confidence, v.reason, v.source, v.updated_at
                   FROM edge_verdicts v
                   LEFT JOIN unnest(CAST(:moved_old AS text[]), CAST(:moved_new AS text[])) AS ma(old, new)
                     ON ma.old = v.src_key
                   LEFT JOIN unnest(CAST(:moved_old AS text[]), CAST(:moved_new AS text[])) AS mb(old, new)
                     ON mb.old = v.dst_key) keyed
               ORDER BY src_key, dst_key, type, extractor, source = 'user' DESC, updated_at DESC)""", {
        "moved_old": sorted(moved), "moved_new": [moved[k] for k in sorted(moved)]}


def _order(row: dict) -> tuple[str, str, str, str]:
    return (row["a"], row["b"], row["t"], row["x"])


def _key(edge: dict) -> tuple[str, str, str, str]:
    return (edge["src_key"], edge["dst_key"], edge["type"], edge["extractor"])


def _row(edge: dict, d: str, verdict: dict) -> dict:
    return {"a": edge["src_key"], "b": edge["dst_key"], "t": edge["type"], "x": edge["extractor"], "d": d,
            "v": verdict["verdict"], "c": verdict.get("confidence"), "r": (verdict.get("reason") or "")[:300]}


def run(s: Session, log=print, budget: int = BUDGET, progress=None) -> dict:
    """Settle the links with direct evidence, judge every other derived edge that has no verdict for what it would
    be judged on now (storing each batch as it arrives), then apply every verdict in the caller's transaction.
    `progress(count, total)` follows the model calls."""
    from igneous.graph.derived import rekeyed
    from igneous.llm import allowed_sources, nameable

    stats = {"judged": 0, "keep": 0, "drop": 0, "unsure": 0, "cached": 0, "calls": 0, "strong": 0, "not_shared": 0,
             "settled": 0}
    verdicts, keyed = _verdicts(rekeyed(s))
    known = {(r[0], r[1], r[2], r[3]): (r[4], r[5], r[6]) for r in s.execute(text(
        f"SELECT src_key, dst_key, type, extractor, evidence, source, verdict FROM {verdicts} v"), keyed)}
    edges = _edges(s)
    # 1. Direct evidence: the record itself lists them (a thread's group chat members, its events' attendees).
    settled, forget, rest = [], [], []
    for edge in edges:
        prior = known.get(_key(edge))
        if prior and prior[1] == "user":
            continue  # you decided; never judged again
        if (edge["props"] or {}).get("direct"):
            if prior is None or prior[:2] != ("direct", "evidence"):
                settled.append({**_row(edge, "direct", {"verdict": "keep", "confidence": 1.0,
                                                        "reason": edge["props"].get("why") or "the record lists them"}),
                                "src": "evidence"})
            stats["settled"] += 1
            continue
        if prior and prior[1] == "evidence":
            forget.append({"a": edge["src_key"], "b": edge["dst_key"], "t": edge["type"], "x": edge["extractor"]})
            known.pop(_key(edge))
        rest.append(edge)
    if settled or forget:
        _store(settled, forget)
    sources = allowed_sources()
    if not settings.llm_enabled or not sources:
        stats["applied"] = apply_verdicts(s)
        return stats
    # 2. The model, for the rest it may read: edges whose two ends a prompt may name, with opted-in evidence.
    intros = [int(e["props"]["introducer"]) for e in rest if e["type"] == "INTRODUCED"
              and (e["props"] or {}).get("introducer") is not None]
    names = nameable(s, [e["src_id"] for e in rest] + [e["dst_id"] for e in rest] + intros, sources)
    todo: dict[tuple, list[tuple[dict, str, str]]] = {}
    strong: dict[tuple, list[tuple[dict, str, str]]] = {}
    for edge in rest:
        if edge["src_id"] not in names or edge["dst_id"] not in names:
            stats["not_shared"] += 1  # a prompt may not name one of its ends (llm.nameable)
            continue
        evidence, unseen = ([], False) if edge["type"] == "SAME_AS" else _evidence(s, edge, sources)
        if unseen and not evidence:
            stats["not_shared"] += 1  # every record behind it is in a source the model may not read
            continue
        if evidence and not edge["items"]:
            # A link proposed without records: what the judge reads becomes its evidence, so the why-view shows
            # what the verdict was made on instead of "no evidence".
            s.execute(text("""INSERT INTO edge_evidence (edge_id, source_item_id, kind, detail)
                              VALUES (:e, :s, 'verified-on', '{}')"""), [{"e": edge["id"], "s": e["id"]} for e in evidence])
        claim = claim_of(edge)
        desc = (_describe_identity(s, edge, sources) if edge["type"] == "SAME_AS"
                else _describe(edge, evidence, unseen, names))
        d = digest(claim, desc)
        prior = known.get(_key(edge))
        if prior and prior[0] == f"strong:{d}":
            stats["cached"] += 1
        elif prior and prior[0] == d:
            stats["cached"] += 1
            if prior[2] == "unsure":
                strong.setdefault(claim, []).append((edge, desc, d))  # the cheap model was unsure: ask once more
        else:
            todo.setdefault(claim, []).append((edge, desc, d))
    # What people see first is judged first: who's in a thread, what sits under an org, which identities merge;
    # then the many item links. A run that hits its budget never leaves the people lists unchecked.
    order = {"INVOLVED_IN": 0, "SAME_AS": 1, "RELATED_TO": 2, "WORKED_AT": 3, "PART_OF": 4, "INTRODUCED": 5}
    planned = sum(-(-len(v) // BATCH) for v in todo.values()) + sum(-(-len(v) // BATCH) for v in strong.values())
    total = min(budget, planned)

    def call(claim, batch, model) -> dict[int, dict]:
        stats["calls"] += 1
        if progress:
            progress(stats["calls"], total)
        return _judge_batch(s, claim, batch, sources, model)

    for claim in sorted(set(todo) | set(strong), key=lambda c: (order.get(c[0], 9), c[1] != "hierarchy", c)):
        items, again = todo.get(claim, []), list(strong.get(claim, []))
        for i in range(0, len(items), BATCH):
            if stats["calls"] >= budget:
                break
            batch = items[i:i + BATCH]
            answers = call(claim, batch, settings.llm_cheap_model)
            rows = []
            for j, (edge, desc, d) in enumerate(batch):
                if j not in answers:
                    continue  # not answered: asked again next run
                rows.append(_row(edge, d, answers[j]))
                stats["judged"] += 1
                stats[answers[j]["verdict"]] += 1
                if answers[j]["verdict"] == "unsure":
                    again.append((edge, desc, d))
            if rows:
                _store(rows)
        for i in range(0, len(again), BATCH):
            if stats["calls"] >= budget:
                break
            batch = again[i:i + BATCH]
            answers = call(claim, batch, None)  # the strong model
            rows = [_row(edge, f"strong:{d}", answers[j]) for j, (edge, _desc, d) in enumerate(batch) if j in answers]
            stats["strong"] += len(rows)
            if rows:
                _store(rows)
    stats["applied"] = apply_verdicts(s)
    if log:
        log(json.dumps(stats))
    return stats


def apply_verdicts(s: Session) -> int:
    """Put every verdict on the graph as it is now, and return how many links changed:
      - your rejections: no derived link joins a pair you said is wrong, under any of their identities;
      - the verifier's drops: the link moves to `edges_dropped` (kept for the why-view and undo, one row per natural
        key), and a 'keep' of yours on a dropped link brings it back; a row whose verdict no longer drops it goes;
      - every other verdict rides on its link (props.verdict, reason, confidence, verified_by)."""
    from igneous.graph.derived import blocked_sql, rejections, rekeyed

    blocked = rejections(s).id_pairs()
    verdicts, keyed = _verdicts(rekeyed(s))
    removed = s.execute(text(f"""
        DELETE FROM edges e WHERE e.extractor <> ALL(:stated) AND {blocked_sql('e.src', 'e.dst')} RETURNING e.type
    """), {"stated": list(STATED), "blocked": blocked}).scalars().all() if blocked else []
    moved = s.execute(text(f"""
        WITH doomed AS (
            SELECT e.id, a.type || ':' || a.key AS src_key, b.type || ':' || b.key AS dst_key, v.reason
            FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst
            JOIN {verdicts} v ON v.src_key = a.type || ':' || a.key AND v.dst_key = b.type || ':' || b.key
                AND v.type = e.type AND v.extractor = e.extractor
            WHERE v.verdict = 'drop' AND v.source = 'model' AND e.extractor <> ALL(:stated)
                AND (e.type <> 'SAME_AS' OR v.confidence >= 0.8)
        ), d AS (
            DELETE FROM edges e USING doomed WHERE e.id = doomed.id
            RETURNING e.*, doomed.src_key, doomed.dst_key, doomed.reason AS _reason)
        INSERT INTO edges_dropped (id, src, dst, type, props, valid_from, valid_to, extractor, extractor_version, model,
                                   confidence, source_item_id, span_start, span_end, src_key, dst_key)
        SELECT id, src, dst, type, props || jsonb_build_object('verdict', 'drop', 'reason', _reason), valid_from,
               valid_to, extractor, extractor_version, model, confidence, source_item_id, span_start, span_end,
               src_key, dst_key FROM d
        ON CONFLICT (src_key, dst_key, type, extractor) DO UPDATE SET id = EXCLUDED.id, src = EXCLUDED.src,
            dst = EXCLUDED.dst, props = EXCLUDED.props, valid_from = EXCLUDED.valid_from, valid_to = EXCLUDED.valid_to,
            extractor_version = EXCLUDED.extractor_version, model = EXCLUDED.model,
            confidence = EXCLUDED.confidence, source_item_id = EXCLUDED.source_item_id,
            span_start = EXCLUDED.span_start, span_end = EXCLUDED.span_end
        RETURNING type
    """), {"stated": list(STATED), **keyed}).scalars().all()
    back = s.execute(text(f"""
        INSERT INTO edges (id, src, dst, type, props, valid_from, valid_to, extractor, extractor_version, model,
                           confidence, source_item_id, span_start, span_end)
        SELECT d.id, a.id, b.id, d.type, d.props - 'verdict' - 'reason', d.valid_from, d.valid_to, d.extractor,
               d.extractor_version, d.model, d.confidence, d.source_item_id, d.span_start, d.span_end
        FROM edges_dropped d
        JOIN {verdicts} v ON (v.src_key, v.dst_key, v.type, v.extractor) = (d.src_key, d.dst_key, d.type, d.extractor)
        JOIN nodes a ON a.type || ':' || a.key = d.src_key JOIN nodes b ON b.type || ':' || b.key = d.dst_key
        WHERE v.source = 'user' AND v.verdict = 'keep'
          AND (d.source_item_id IS NULL OR EXISTS (SELECT 1 FROM source_items si WHERE si.id = d.source_item_id))
          {f"AND NOT {blocked_sql('a.id', 'b.id')}" if blocked else ""}
        ON CONFLICT DO NOTHING RETURNING type
    """), {"blocked": blocked, **keyed}).scalars().all()
    # An archived link goes once its verdict (as the graph is keyed now) no longer drops it.
    s.execute(text(f"""
        DELETE FROM edges_dropped d WHERE NOT EXISTS (
            SELECT 1 FROM {verdicts} v
            WHERE (v.src_key, v.dst_key, v.type, v.extractor) = (d.src_key, d.dst_key, d.type, d.extractor)
              AND v.verdict = 'drop' AND v.source = 'model' AND (d.type <> 'SAME_AS' OR v.confidence >= 0.8))
    """), keyed)
    # Identities you said are not one person, still in one cluster (a trusted link joins them): split them now.
    together = s.execute(text("""
        SELECT EXISTS (SELECT 1 FROM edge_verdicts v
            JOIN nodes a ON a.type || ':' || a.key = v.src_key JOIN nodes b ON b.type || ':' || b.key = v.dst_key
            WHERE v.type = 'SAME_AS' AND v.verdict = 'drop' AND v.source = 'user' AND a.id <> b.id
              AND COALESCE((SELECT canonical_id FROM canonical WHERE node_id = a.id), a.id)
                  = COALESCE((SELECT canonical_id FROM canonical WHERE node_id = b.id), b.id))
    """)).scalar()
    if together or "SAME_AS" in [*removed, *moved, *back]:
        from igneous.graph.resolve import refresh_canonical
        refresh_canonical(s)
    return len(removed) + len(moved) + len(back) + s.execute(text(f"""
        UPDATE edges e SET props = e.props || jsonb_build_object('verdict', v.verdict, 'reason', v.reason,
                                                                  'confidence', v.confidence, 'verified_by', v.source)
        FROM nodes a, nodes b, {verdicts} v
        WHERE a.id = e.src AND b.id = e.dst AND v.src_key = a.type || ':' || a.key
          AND v.dst_key = b.type || ':' || b.key AND v.type = e.type AND v.extractor = e.extractor
          AND e.extractor <> ALL(:stated)
          AND (e.props->>'verdict' IS DISTINCT FROM v.verdict OR e.props->>'reason' IS DISTINCT FROM v.reason)
    """), {"stated": list(STATED), **keyed}).rowcount


def record_correction(s: Session, src_key: str, dst_key: str, type_: str, extractor: str, verdict: str,
                      reason: str | None = None, override_id: int | None = None) -> None:
    """A user's correction is a verdict that wins forever and teaches the judge (few-shot): 'drop' rejects the pair
    (graph/derived.py), 'keep' confirms the link."""
    s.execute(text("""
        INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, confidence, reason, source)
        VALUES (:a, :b, :t, :x, :evidence, :v, 1.0, :r, 'user')
        ON CONFLICT (src_key, dst_key, type, extractor) DO UPDATE SET verdict = EXCLUDED.verdict,
            evidence = EXCLUDED.evidence, reason = EXCLUDED.reason, source = 'user', confidence = 1.0,
            updated_at = now()
    """), {"a": src_key, "b": dst_key, "t": type_, "x": extractor, "v": verdict, "r": reason,
           "evidence": f"override:{override_id}" if override_id is not None else "user"})
