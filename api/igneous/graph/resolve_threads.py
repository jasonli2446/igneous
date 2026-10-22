"""Entity resolution for threads and projects: one ETA, not eighteen.

Threads are found from bursts of different words, so one real project can surface many times ("ETA platform",
"eta-platform (DealRelay)", "xLab ETA / Promise Partners project"), and two different but related things
(the ETA interviews and the ETA platform they led to) look alike. Same structure as people:

  1. Candidate pairs, cheaply and inclusively: threads that share records or people, or name each other, within
     overlapping or adjacent time spans.
  2. One cached judgment per pair from what each thread is (name, kind, span, people, sample records):
     same (merge) | related (link, with the relation in words) | distinct.
  3. `same` pairs merge by union-find: the survivor keeps the best name, gains every record and person, and the
     merged-away keys are remembered so a rebuild folds them again. `related` writes a RELATED_TO edge with the
     reason, which the verifier checks like any other derived edge.
"""
import hashlib
import json
import re
from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings

EXTRACTOR = "thread-resolution"
VERSION = "1"
MIN_SHARED = 0.15        # Jaccard of records or people for a pair to be judged
NEAR_DAYS = 45           # spans within this many days of each other count as overlapping
MAX_PAIRS = 300


def _threads(s: Session) -> list[dict]:
    rows = s.execute(text("""
        SELECT p.id, p.key, p.name, p.props->>'kind' AS kind, (p.props->>'start')::timestamptz AS a,
               (p.props->>'end')::timestamptz AS b,
               array_remove(array_agg(DISTINCT n.source_item_id), NULL) AS items,
               array_remove(array_agg(DISTINCT ip.src), NULL) AS people
        FROM nodes p
        LEFT JOIN edges e ON e.dst = p.id AND e.type = 'PART_OF' AND e.extractor = 'threads'
        LEFT JOIN nodes n ON n.id = e.src
        LEFT JOIN edges ip ON ip.dst = p.id AND ip.type = 'INVOLVED_IN'
        WHERE p.type = 'Project' AND p.props->>'derived' = 'threads' AND NOT COALESCE((p.props->>'umbrella')::bool, false)
        GROUP BY p.id
    """)).mappings().all()
    return [{**r, "items": set(r["items"] or []), "people": set(r["people"] or [])} for r in rows]


def _tokens(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(w) >= 3}


def candidate_pairs(ts: list[dict]) -> list[tuple[dict, dict]]:
    """Pairs worth judging: shared records/people or a shared name word, with spans that overlap or nearly do."""
    out = []
    for i, a in enumerate(ts):
        for b in ts[i + 1:]:
            if a["a"] and b["a"] and a["b"] and b["b"]:
                gap = max((b["a"] - a["b"]).days, (a["a"] - b["b"]).days)
                if gap > NEAR_DAYS:
                    continue
            jr = len(a["items"] & b["items"]) / max(1, len(a["items"] | b["items"]))
            jp = len(a["people"] & b["people"]) / max(1, len(a["people"] | b["people"]))
            jn = len(_tokens(a["name"]) & _tokens(b["name"])) / max(1, len(_tokens(a["name"]) | _tokens(b["name"])))
            if max(jr, jp) >= MIN_SHARED or jn >= 0.34:
                out.append((max(jr, jp, jn), a, b))
    out.sort(key=lambda x: -x[0])
    return [(a, b) for _, a, b in out[:MAX_PAIRS]]


def _card(s: Session, t: dict) -> str:
    people = s.execute(text("SELECT name FROM nodes WHERE id = ANY(:i) LIMIT 6"), {"i": list(t["people"])}).scalars().all()
    titles = s.execute(text("""
        SELECT left(title, 90) FROM source_items WHERE id = ANY(:i) AND coalesce(title, '') <> ''
        ORDER BY occurred_at DESC NULLS LAST LIMIT 6"""), {"i": list(t["items"])}).scalars().all()
    span = f"{t['a']:%Y-%m-%d} to {t['b']:%Y-%m-%d}" if t["a"] and t["b"] else "unknown span"
    return (f"'{t['name']}' ({t['kind'] or 'thread'}, {span}, {len(t['items'])} records)\n"
            f"   people: {', '.join(people) or 'none recorded'}\n   records: " + " | ".join(titles))


def judge(s: Session, a: dict, b: dict) -> dict:
    """same | related | distinct, with the relation in words. Cached; without the model, only exact-name
    duplicates merge and nothing is linked."""
    from igneous.llm import LLMDisabled, structured

    fallback = {"relation": "same" if (a["name"] or "").lower() == (b["name"] or "").lower() else "distinct",
                "label": "", "confidence": 0.5, "reason": "same name"}
    if not settings.llm_enabled:
        return fallback
    prompt = f"A: {_card(s, a)}\n\nB: {_card(s, b)}"
    key = "thread-pair:v1:" + hashlib.sha256(prompt.encode()).hexdigest()
    cached = s.execute(text("SELECT response FROM llm_cache WHERE cache_key = :k"), {"k": key}).scalar()
    if cached is not None:
        return cached
    try:
        out, _ = structured(
            "Two threads were found in someone's records (projects, trips, classes, jobs). Decide how they relate. "
            "'same': they are the same thing under different names or found twice (the same project, product, "
            "trip, course). 'related': different things that belong together (one led to the other, one is part "
            "of the other, a project and its interviews, two projects of the same client); give the relation in "
            "a few words ('interviews for', 'part of', 'same client as'). 'distinct': unrelated. Give a confidence "
            "0-1 and a short reason.",
            prompt,
            {"type": "object", "additionalProperties": False, "required": ["relation", "label", "confidence", "reason"],
             "properties": {"relation": {"enum": ["same", "related", "distinct"]}, "label": {"type": "string"},
                            "confidence": {"type": "number"}, "reason": {"type": "string"}}},
            max_tokens=200)
    except LLMDisabled:
        return fallback
    s.execute(text("INSERT INTO llm_cache (cache_key, response) VALUES (:k, CAST(:r AS jsonb)) "
                   "ON CONFLICT (cache_key) DO NOTHING"), {"k": key, "r": json.dumps(out)})
    return out


def run(s: Session, log=print) -> dict:
    ts = _threads(s)
    s.execute(text("DELETE FROM edges WHERE extractor = :x"), {"x": EXTRACTOR})
    parent = {t["id"]: t["id"] for t in ts}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    related, stats = [], {"pairs": 0, "same": 0, "related": 0, "distinct": 0}
    for a, b in candidate_pairs(ts):
        v = judge(s, a, b)
        stats["pairs"] += 1
        rel = v.get("relation", "distinct")
        if rel == "same" and (v.get("confidence") or 0) >= 0.7:
            parent[find(a["id"])] = find(b["id"])
            stats["same"] += 1
        elif rel == "related" and (v.get("confidence") or 0) >= 0.6:
            related.append((a, b, v))
            stats["related"] += 1
        else:
            stats["distinct"] += 1

    # Merge: each cluster keeps its biggest thread (most records), named after it; the others fold in.
    clusters: dict[int, list[dict]] = defaultdict(list)
    for t in ts:
        clusters[find(t["id"])].append(t)
    folded_into: dict[int, int] = {}
    for members in clusters.values():
        if len(members) < 2:
            continue
        keep = max(members, key=lambda t: (len(t["items"]), len(t["people"])))
        for t in members:
            if t["id"] == keep["id"]:
                continue
            folded_into[t["id"]] = keep["id"]
            # Move its record and people edges onto the survivor (no duplicates), then remove it.
            s.execute(text("""
                INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence, source_item_id)
                SELECT e.src, :keep, e.type, e.props, e.extractor, e.extractor_version, e.confidence, e.source_item_id
                FROM edges e WHERE e.dst = :gone AND e.type IN ('PART_OF', 'INVOLVED_IN')
                ON CONFLICT (src, dst, type, extractor) DO NOTHING
            """), {"keep": keep["id"], "gone": t["id"]})
            s.execute(text("""
                UPDATE nodes SET props = props || jsonb_build_object('aliases',
                    COALESCE(props->'aliases', '[]'::jsonb) || to_jsonb(CAST(:alias AS text)))
                WHERE id = :keep"""), {"keep": keep["id"], "alias": t["name"]})
            s.execute(text("DELETE FROM nodes WHERE id = :gone"), {"gone": t["id"]})
    # Link related threads (after merging, so links land on survivors).
    for a, b, v in related:
        ia, ib = folded_into.get(a["id"], a["id"]), folded_into.get(b["id"], b["id"])
        if ia == ib:
            continue  # they ended up merged after all
        s.execute(text("""
            INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
            VALUES (:a, :b, 'RELATED_TO', CAST(:p AS jsonb), :x, :v, :c)
            ON CONFLICT (src, dst, type, extractor) DO UPDATE SET props = EXCLUDED.props
        """), {"a": min(ia, ib), "b": max(ia, ib), "x": EXTRACTOR, "v": VERSION, "c": v.get("confidence"),
               "p": json.dumps({"why": v.get("label") or "related", "reason": v.get("reason")})})
    stats["merged_away"] = len(folded_into)
    s.commit()
    if log:
        log(json.dumps(stats))
    return stats
