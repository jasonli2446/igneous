"""Entity resolution for threads and projects: one Atlas, not eighteen.

Threads are found from bursts of different words, so one real project can surface many times ("Atlas platform",
"atlas-platform (client app)", "Lab Atlas / partner firm project"), and two different but related things
(the Atlas interviews and the Atlas platform they led to) look alike. Same structure as people:

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
MAX_PAIRS = 1500  # judgments are cached by thread identity, so a wide net costs once


def _threads(s: Session) -> list[dict]:
    rows = s.execute(text("""
        SELECT p.id, p.key, p.name, p.props->>'kind' AS kind, COALESCE((p.props->>'pinned')::bool, false) AS pinned,
               (p.props->>'start')::timestamptz AS a,
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
    """Name words; a course code is one identifier ("CS 401" and "cs401" match; "EE 401" is another course).
    A bare number is never a name word: it's what different courses, rooms and years share."""
    low = (name or "").lower()
    codes = re.findall(r"\b([a-z]{2,5})\s?(\d{3})\b", low)
    words = {w for w in re.findall(r"[a-z0-9]+", low) if len(w) >= 3 and not w.isdigit()}
    words -= {d for _c, d in codes} | {c for c, _d in codes}
    return words | {f"course:{c}{d}" for c, d in codes}


MENTIONS_MIN = 2  # records of one thread naming another, for the pair to be judged ("CS 401" in Igneous's proposal)


def _mentions(s: Session, ts: list[dict]) -> set[tuple[int, int]]:
    """(a, b): a's records name b outright. Only distinctive names (a course code, or 2+ words), whole words."""
    out: set[tuple[int, int]] = set()
    for b in ts:
        name = (b["name"] or "").strip()
        code = re.fullmatch(r"([A-Za-z]{2,5})\s?(\d{3})", name)
        if not code and len(name.split()) < 2:
            continue
        pat = rf"\m{code.group(1)}\s?{code.group(2)}\M" if code else r"\m" + re.escape(name) + r"\M"
        for (tid,) in s.execute(text("""
            SELECT e.dst FROM edges e JOIN nodes n ON n.id = e.src JOIN source_items si ON si.id = n.source_item_id
            WHERE e.type = 'PART_OF' AND e.extractor = 'threads' AND e.dst <> :b
              AND (si.title || ' ' || coalesce(si.body_text, '')) ~* :p
            GROUP BY e.dst HAVING count(*) >= :m"""), {"b": b["id"], "p": pat, "m": MENTIONS_MIN}):
            out.add((tid, b["id"]))
    return out


def candidate_pairs(ts: list[dict], mentions: set[tuple[int, int]] = frozenset()) -> list[tuple[dict, dict]]:
    """Pairs worth judging: shared records/people or a shared name word, with spans that overlap or nearly do,
    or one naming the other in its records (a project done for a class)."""
    out = []
    for i, a in enumerate(ts):
        for b in ts[i + 1:]:
            named = (a["id"], b["id"]) in mentions or (b["id"], a["id"]) in mentions
            if not named and a["a"] and b["a"] and a["b"] and b["b"]:
                gap = max((b["a"] - a["b"]).days, (a["a"] - b["b"]).days)
                if gap > NEAR_DAYS:
                    continue
            jr = len(a["items"] & b["items"]) / max(1, len(a["items"] | b["items"]))
            jp = len(a["people"] & b["people"]) / max(1, len(a["people"] | b["people"]))
            jn = len(_tokens(a["name"]) & _tokens(b["name"])) / max(1, len(_tokens(a["name"]) | _tokens(b["name"])))
            if max(jr, jp) >= MIN_SHARED or jn >= 0.34 or named:
                out.append((max(jr, jp, jn, 0.3 if named else 0), a, b))
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
    a, b = sorted((a, b), key=lambda t: t["key"])  # a stable A/B, so the cached direction means the same thing
    prompt = f"A: {_card(s, a)}\n\nB: {_card(s, b)}"
    # Keyed by the two threads' identities, not their exact current records: a judgment is reused while the
    # threads stay the same things, so it doesn't re-roll every time a few records shift.
    ident = "|".join(sorted([a["key"], b["key"]]))
    key = "thread-pair:v5:" + hashlib.sha256(ident.encode()).hexdigest()
    cached = s.execute(text("SELECT response FROM llm_cache WHERE cache_key = :k"), {"k": key}).scalar()
    if cached is not None:
        return cached
    try:
        system = ("Two threads were found in someone's records (projects, trips, classes, jobs). Decide how they relate. "
            "'same' ONLY if they are one and the same thing found twice: the same named project or product, the "
            "same trip, the same course. You must be able to name what they both are; sharing people, a time "
            "period, an organization or a topic is NOT enough (two trips in one summer are different trips; two "
            "projects of one lab are different projects). 'related': different things that belong together (one "
            "led to the other, same client or lab); give the relation in a few words. 'part_of': one was done "
            "within or for the other (a project built for a class, a sub-project of a larger project, an interview "
            "round of a job search); the records must show it, not just a shared topic. For 'part_of', `container` "
            "names the larger one by its exact name (the class, the larger project, the job search). "
            "'distinct': not meaningfully related. When unsure between same and related, choose related. "
            "Give a confidence 0-1 and a short reason that names the thing if 'same'.")
        schema = {"type": "object", "additionalProperties": False,
             "required": ["relation", "label", "confidence", "reason", "shared_identity", "container"],
             "properties": {"relation": {"enum": ["same", "part_of", "related", "distinct"]}, "label": {"type": "string"},
                            "container": {"type": "string"},
                            "shared_identity": {"type": "string"},
                            "confidence": {"type": "number"}, "reason": {"type": "string"}}}
        out, _ = structured(system, prompt, schema,
            max_tokens=500, model=settings.llm_cheap_model)
        if out.get("relation") == "same":
            # A merge is hard to undo: confirm a cheap-model 'same' with the stronger model before trusting it.
            out, _ = structured(system, prompt, schema, max_tokens=500)
    except LLMDisabled:
        return fallback
    except Exception:  # one failed judgment must not stop the run; unjudged pairs stay separate
        return {"relation": "distinct", "label": "", "confidence": 0.0, "reason": "not judged"}
    s.execute(text("INSERT INTO llm_cache (cache_key, response) VALUES (:k, CAST(:r AS jsonb)) "
                   "ON CONFLICT (cache_key) DO NOTHING"), {"k": key, "r": json.dumps(out)})
    return out


def _norm_name(n: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", n.lower()).strip()


def _clusters_agree(a: set[int], b: set[int], verdicts: dict, names: dict[int, str] | None = None) -> bool:
    """Merge two clusters only on direct evidence: every JUDGED pair across them says 'same' confidently, at
    least half of all cross pairs were actually judged, and every thread shares a name word with the other
    side. Unjudged pairs can't chain unrelated threads together (how an app swallowed a trip)."""
    pairs = [(min(x, y), max(x, y)) for x in a for y in b]
    judged = [verdicts[p] for p in pairs if p in verdicts]
    if not judged or len(judged) < 0.5 * len(pairs):
        return False
    if any(v.get("relation") != "same" or (v.get("confidence") or 0) < 0.75 for v in judged):
        return False
    if names:
        words = lambda ids: set().union(*(_tokens(names.get(i, "")) for i in ids))  # noqa: E731
        wa, wb = words(a), words(b)
        if any(not (_tokens(names.get(i, "")) & wb) for i in a) or any(not (_tokens(names.get(i, "")) & wa) for i in b):
            return False
    return True


def run(s: Session, log=print) -> dict:
    ts = _threads(s)
    s.execute(text("DELETE FROM edges WHERE extractor = :x"), {"x": EXTRACTOR})
    parent = {t["id"]: t["id"] for t in ts}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    related, stats = [], {"pairs": 0, "same": 0, "related": 0, "distinct": 0, "same_not_merged": 0}
    verdicts: dict[tuple[int, int], dict] = {}
    for a, b in candidate_pairs(ts, _mentions(s, ts)):
        v = judge(s, a, b)
        verdicts[(min(a["id"], b["id"]), max(a["id"], b["id"]))] = v
        stats["pairs"] += 1
        stats[v.get("relation", "distinct")] = stats.get(v.get("relation", "distinct"), 0) + 1

    def is_same(x: int, y: int) -> bool:
        v = verdicts.get((min(x, y), max(x, y)))
        return bool(v) and v.get("relation") == "same" and (v.get("confidence") or 0) >= 0.8

    members_of: dict[int, set[int]] = {t["id"]: {t["id"]} for t in ts}
    # Complete-link merging, strongest first: two clusters merge only if EVERY pair across them was judged the
    # same thing. One eager judgment can't chain unrelated threads together (how a trip got swallowed before).
    for (x, y), v in sorted(verdicts.items(), key=lambda kv: -(kv[1].get("confidence") or 0)):
        if not is_same(x, y):
            if v.get("relation") in ("related", "part_of") and (v.get("confidence") or 0) >= 0.6:
                related.append((x, y, v))
            continue
        rx, ry = find(x), find(y)
        if rx == ry:
            continue
        if _clusters_agree(members_of[rx], members_of[ry], verdicts, {t["id"]: t["name"] for t in ts}):
            parent[rx] = ry
            members_of[ry] |= members_of.pop(rx)
        else:
            stats["same_not_merged"] += 1
            related.append((x, y, {**v, "label": v.get("label") or "closely related"}))

    # Merge: each cluster keeps its biggest thread (most records), named after it; the others fold in.
    clusters: dict[int, list[dict]] = defaultdict(list)
    for t in ts:
        clusters[find(t["id"])].append(t)
    folded_into: dict[int, int] = {}
    for members in clusters.values():
        if len(members) < 2:
            continue
        # A pinned thread (gold set / your correction) always survives, so its name and stated links stay.
        keep = max(members, key=lambda t: (bool(t.get("pinned")), len(t["items"]), len(t["people"])))
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
            # Remember the merge: the gone thread's words now define the survivor, and the gone thread is not
            # rebuilt next run (so the same merge isn't re-judged and re-rolled every time).
            s.execute(text("""
                UPDATE thread_registry k SET terms = ARRAY(SELECT DISTINCT unnest(k.terms || g.terms)), updated_at = now()
                FROM thread_registry g WHERE k.key = :keep AND g.key = :gone"""), {"keep": keep["key"], "gone": t["key"]})
            s.execute(text("UPDATE thread_registry SET status = 'merged', merged_into = :keep, updated_at = now() "
                           "WHERE key = :gone AND NOT pinned"), {"keep": keep["key"], "gone": t["key"]})
    # Link related threads (after merging, so links land on survivors).
    name_of = {t["id"]: t["name"] or "" for t in ts}
    for x, y, v in related:
        ia, ib = folded_into.get(x, x), folded_into.get(y, y)
        if ia == ib:
            continue  # they ended up merged after all
        if v.get("relation") == "part_of":
            # The judge names the container (not an A/B label, which models flip): match it to one of the two.
            names = {i: _norm_name(name_of[i]) for i in (x, y)}
            box = _norm_name(v.get("container") or "")
            hit = [i for i in (x, y) if box and (names[i] == box or names[i] in box or box in names[i])]
            if len(hit) != 1:
                continue  # can't tell which is the container: no link rather than a backwards one
            parent = hit[0]
            child = y if parent == x else x
            child, parent = folded_into.get(child, child), folded_into.get(parent, parent)
            if child == parent or not s.execute(text("SELECT count(*) = 2 FROM nodes WHERE id IN (:a, :b)"),
                                                {"a": child, "b": parent}).scalar():
                continue  # one side was merged away (or into the other) earlier in this run
            s.execute(text("""
                INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
                VALUES (:a, :b, 'PART_OF', CAST(:p AS jsonb), 'hierarchy', :v, :c)
                ON CONFLICT (src, dst, type, extractor) DO UPDATE SET props = EXCLUDED.props
            """), {"a": child, "b": parent, "v": VERSION, "c": v.get("confidence"),
                   "p": json.dumps({"why": v.get("label") or "part of it", "reason": v.get("reason")})})
            stats["part_of"] = stats.get("part_of", 0) + 1
            continue
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
