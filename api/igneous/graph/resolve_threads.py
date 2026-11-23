"""Entity resolution for threads and projects: one Atlas, not eighteen.

Threads are found from bursts of different words, so one real project can surface many times ("Atlas platform",
"atlas-platform (client app)", "Lab Atlas / partner firm project"), and two different but related things
(the Atlas interviews and the Atlas platform they led to) look alike. Same structure as people:

  1. Candidate pairs, cheaply and inclusively: threads that share records or people, or name each other, within
     overlapping or adjacent time spans.
  2. One cached judgment per pair from what each thread is (name, kind, span, people, sample records, as a prompt may
     show them): same | part_of (one was done within the other) | related (with the relation in words) | distinct.
     Without the model, or when either thread has no record a model may read, only threads with the same name are
     the same, and nothing is linked. A shared course code within one term is one thread whatever a model says: two
     threads most of whose records name the same code, with spans that overlap or nearly do, are the same (a class
     and its capstone project or its demo day, found under their own names), and no model is asked.
  3. Complete-link merging: two clusters merge only when every judged pair across them says 'same' confidently and
     every thread shares a name word (or the course code most of its records name) with the other side. Each cluster
     keeps a pinned thread first, then its class (a thread whose own term is the course code most of its records
     name), then its biggest; the others fold into it (their links move there, their names become its aliases) and
     become its facets in the registry, so later runs build their records into it without judging the pair again. A
     class's project or demo day folded into it is its part (merged_by 'part'), not the class: a name you gave it
     doesn't name the class (graph/derived.py `merges`). A 'part_of' pair writes PART_OF child -> container
     (extractor 'hierarchy'), a 'related' one RELATED_TO with the reason; the verifier checks both like any other
     derived edge.
"""
import hashlib
import json
import re
from collections import Counter, defaultdict

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings
from igneous.graph.derived import Writer, norm

EXTRACTOR = "thread-resolution"
VERSION = "1"
MIN_SHARED = 0.15        # Jaccard of records or people for a pair to be judged
NEAR_DAYS = 45           # spans within this many days of each other count as overlapping
MAX_PAIRS = 1500         # judgments are cached by thread identity, so a wide net costs once
SAME = 0.8               # a 'same' this confident merges


def _threads(s: Session) -> list[dict]:
    rows = s.execute(text("""
        SELECT p.id, p.key, p.name, p.props->>'kind' AS kind, COALESCE((p.props->>'pinned')::bool, false) AS pinned,
               p.props->'terms'->>0 AS term,
               (p.props->>'start')::timestamptz AS a,
               (p.props->>'end')::timestamptz AS b,
               array_remove(array_agg(DISTINCT n.source_item_id), NULL) AS items,
               array_remove(array_agg(DISTINCT ip.src), NULL) AS people
        FROM nodes p
        LEFT JOIN edges e ON e.dst = p.id AND e.type = 'PART_OF' AND e.extractor = 'threads'
        LEFT JOIN nodes n ON n.id = e.src
        LEFT JOIN edges ip ON ip.dst = p.id AND ip.type = 'INVOLVED_IN'
        WHERE p.type = 'Project' AND p.props->>'derived' = 'threads' AND NOT COALESCE((p.props->>'umbrella')::bool, false)
        GROUP BY p.id ORDER BY p.key
    """)).mappings().all()
    return [{**r, "items": set(r["items"] or []), "people": set(r["people"] or [])} for r in rows]


def _tokens(name: str) -> set[str]:
    """Name words; a course code is one identifier ("CS 401" and "cs401" match; "EE 401" is another course).
    A bare number is never a name word: it's what different courses, rooms and years share."""
    from igneous.graph.threads import course_codes

    codes = [c.lower().split() for c in course_codes(name)]
    words = {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(w) >= 3 and not w.isdigit()}
    words -= {d for _c, d in codes} | {c for c, _d in codes}
    return words | {f"course:{c}{d}" for c, d in codes}


MENTIONS_MIN = 2  # records of one thread naming another, for the pair to be judged ("CS 401" in Igneous's proposal)


def _mentions(s: Session, ts: list[dict]) -> set[tuple[int, int]]:
    """(a, b): a's records name b outright. Only distinctive names (a course code, or 2+ words), whole words."""
    from igneous.graph.threads import course_code

    out: set[tuple[int, int]] = set()
    for b in ts:
        name = (b["name"] or "").strip()
        code = course_code(name)
        if not code and len(name.split()) < 2:
            continue
        pat = (r"\m{}\s?{}\M".format(*code.lower().split()) if code else r"\m" + re.escape(name) + r"\M")
        for (tid,) in s.execute(text("""
            SELECT e.dst FROM edges e JOIN nodes n ON n.id = e.src JOIN source_items si ON si.id = n.source_item_id
            WHERE e.type = 'PART_OF' AND e.extractor = 'threads' AND e.dst <> :b
              AND (si.title || ' ' || coalesce(si.body_text, '')) ~* :p
            GROUP BY e.dst HAVING count(*) >= :m"""), {"b": b["id"], "p": pat, "m": MENTIONS_MIN}):
            out.add((tid, b["id"]))
    return out


def candidate_pairs(ts: list[dict], mentions: set[tuple[int, int]] = frozenset()) -> list[tuple[dict, dict]]:
    """Pairs worth judging: shared records/people or a shared name word, with spans that overlap or nearly do,
    or one naming the other in its records (a project done for a class). Strongest first, ties by key."""
    out = []
    for i, a in enumerate(ts):
        for b in ts[i + 1:]:
            # One's whole name is inside the other's ("Climbing" in "Climbing Club"): likely one contains the other.
            na, nb = f" {norm(a['name'])} ", f" {norm(b['name'])} "
            nested = len(na.strip()) >= 4 and len(nb.strip()) >= 4 and na != nb and (na in nb or nb in na)
            named = (a["id"], b["id"]) in mentions or (b["id"], a["id"]) in mentions or nested
            if not named and a["a"] and b["a"] and a["b"] and b["b"]:
                gap = max((b["a"] - a["b"]).days, (a["a"] - b["b"]).days)
                if gap > NEAR_DAYS:
                    continue
            jr = len(a["items"] & b["items"]) / max(1, len(a["items"] | b["items"]))
            jp = len(a["people"] & b["people"]) / max(1, len(a["people"] | b["people"]))
            jn = len(_tokens(a["name"]) & _tokens(b["name"])) / max(1, len(_tokens(a["name"]) | _tokens(b["name"])))
            if max(jr, jp) >= MIN_SHARED or jn >= 0.34 or named:
                out.append((max(jr, jp, jn, 0.3 if named else 0), a, b))
    out.sort(key=lambda x: (-x[0], x[1].get("key") or "", x[2].get("key") or ""))
    return [(a, b) for _, a, b in out[:MAX_PAIRS]]


def _codes(s: Session, ts: list[dict]) -> dict[int, set[str]]:
    """thread -> the course codes most of its records name (threads.MOSTLY of them)."""
    from igneous.graph.threads import MOSTLY, course_codes

    items = sorted(set().union(*(t["items"] for t in ts))) if ts else []
    named = {i: set(course_codes(body)) for i, body in s.execute(text("""
        SELECT id, coalesce(title, '') || ' ' || left(coalesce(body_text, ''), 4000) FROM source_items
        WHERE id = ANY(:i)"""), {"i": items})}
    out = {}
    for t in ts:
        counts = Counter(code for i in t["items"] for code in named.get(i, ()))
        out[t["id"]] = {code for code, n in counts.items() if n >= MOSTLY * len(t["items"])}
    return out


def class_code(t: dict, codes: dict[int, set[str]]) -> str | None:
    """The course code a thread is the class of: its own term, which most of its records name; None for any other."""
    from igneous.graph.threads import course_code

    code = course_code(t.get("term") or "")
    return code if code and code in codes.get(t["id"], ()) else None


def same_course(a: dict, b: dict, codes: dict[int, set[str]]) -> dict | None:
    """The verdict 'same', with no model call, for two threads most of whose records name one course code within one
    term (spans that overlap or lie within NEAR_DAYS of each other); None otherwise."""
    shared = sorted(codes.get(a["id"], set()) & codes.get(b["id"], set()))
    if not shared or not (a["a"] and a["b"] and b["a"] and b["b"]):
        return None
    if max((b["a"] - a["b"]).days, (a["a"] - b["b"]).days) > NEAR_DAYS:
        return None
    return {"relation": "same", "label": "", "confidence": 0.95, "shared_identity": shared[0], "container": "",
            "reason": f"most records of both name {shared[0]}, in one term"}


def _readable(s: Session, ts: list[dict]) -> tuple[set[int], dict[int, str]]:
    """What the judge may read (#179): the threads' records in opted-in sources, and the names a prompt may use for
    the threads and the people in them (llm.nameable)."""
    from igneous.llm import allowed_sources, nameable

    sources = allowed_sources()
    items = sorted(set().union(*(t["items"] for t in ts))) if ts else []
    shown = set(s.execute(text("SELECT id FROM source_items WHERE id = ANY(:i) AND source = ANY(CAST(:src AS text[]))"),
                          {"i": items, "src": sorted(sources)}).scalars()) if sources and items else set()
    ids = {t["id"] for t in ts} | set().union(*(t["people"] for t in ts)) if ts else set()
    return shown, nameable(s, ids, sources)


def _card(s: Session, t: dict, can_see: tuple[set[int], dict[int, str]]) -> str:
    """One thread as the judge reads it: name, kind, span and size, with people and record titles only from
    opted-in sources."""
    shown, names = can_see
    people = sorted(names[p] for p in t["people"] if p in names)[:6]
    titles = s.execute(text("""
        SELECT left(title, 90) FROM source_items WHERE id = ANY(:i) AND coalesce(title, '') <> ''
        ORDER BY occurred_at DESC NULLS LAST, id DESC LIMIT 6"""), {"i": sorted(t["items"] & shown)}).scalars().all()
    span = f"{t['a']:%Y-%m-%d} to {t['b']:%Y-%m-%d}" if t["a"] and t["b"] else "unknown span"
    return (f"'{names[t['id']]}' ({t['kind'] or 'thread'}, {span}, {len(t['items'])} records)\n"
            f"   people: {', '.join(people) or 'none recorded'}\n   records: " + " | ".join(titles))


def judge(s: Session, a: dict, b: dict, can_see: tuple[set[int], dict[int, str]] | None = None) -> dict:
    """same | part_of | related | distinct, with the relation in words. Cached by the two threads' identities;
    without the model, or when either thread has no record (or no name) a prompt may show, threads with the same name
    are the same and nothing is linked. A failed call is not remembered: the pair is judged again next run."""
    from igneous.llm import LLMDisabled, cache_lookup, cache_store, structured

    same_name = norm(a["name"]) == norm(b["name"])
    fallback = {"relation": "same" if same_name else "distinct", "label": "", "confidence": 0.9 if same_name else 0.5,
                "reason": "same name" if same_name else ""}
    a, b = sorted((a, b), key=lambda t: t["key"])  # a stable A/B, so the cached direction means the same thing
    # Keyed by the two threads' identities, not their exact current records: a judgment is reused while the
    # threads stay the same things, so it doesn't re-roll every time a few records shift.
    key = "thread-pair:v5:" + hashlib.sha256(f"{a['key']}|{b['key']}".encode()).hexdigest()
    cached = cache_lookup(key)
    if cached is not None:
        return cached
    can_see = can_see or _readable(s, [a, b])
    if (not settings.llm_enabled or not (a["items"] & can_see[0] and b["items"] & can_see[0])
            or a["id"] not in can_see[1] or b["id"] not in can_see[1]):
        return fallback
    prompt = f"A: {_card(s, a, can_see)}\n\nB: {_card(s, b, can_see)}"
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
                             "container": {"type": "string"}, "shared_identity": {"type": "string"},
                             "confidence": {"type": "number"}, "reason": {"type": "string"}}}
    try:
        out, _ = structured(system, prompt, schema, max_tokens=500, model=settings.llm_cheap_model)
        if out.get("relation") == "same":
            # A merge is hard to undo: confirm a cheap-model 'same' with the stronger model before trusting it.
            out, _ = structured(system, prompt, schema, max_tokens=500)
    except LLMDisabled:
        return fallback
    except Exception:  # one failed judgment must not stop the run; the pair stays separate until judged
        return {"relation": "distinct", "label": "", "confidence": 0.0, "reason": "not judged"}
    cache_store(key, out)
    return out


def _clusters_agree(a: set[int], b: set[int], verdicts: dict, names: dict[int, str] | None = None,
                    codes: dict[int, set[str]] | None = None) -> bool:
    """Merge two clusters only on direct evidence: every JUDGED pair across them says 'same' confidently, at
    least half of all cross pairs were actually judged, and every thread shares a name word with the other
    side (the course code most of its records name counts as one: `codes`). Unjudged pairs can't chain unrelated
    threads together."""
    pairs = [(min(x, y), max(x, y)) for x in a for y in b]
    judged = [verdicts[p] for p in pairs if p in verdicts]
    if not judged or len(judged) < 0.5 * len(pairs):
        return False
    if any(v.get("relation") != "same" or (v.get("confidence") or 0) < 0.75 for v in judged):
        return False
    if names:
        def tokens(i: int) -> set[str]:
            return _tokens(names.get(i, "")) | {"course:" + c.lower().replace(" ", "") for c in (codes or {}).get(i, ())}

        wa, wb = set().union(*(tokens(i) for i in a)), set().union(*(tokens(i) for i in b))
        if any(not (tokens(i) & wb) for i in a) or any(not (tokens(i) & wa) for i in b):
            return False
    return True


def run(s: Session, log=print, writer: Writer | None = None) -> dict:
    from igneous.graph.overrides import fold_into

    w = writer or Writer(s, EXTRACTOR, VERSION)
    ts = _threads(s)
    s.execute(text("DELETE FROM edges WHERE extractor = :x"), {"x": EXTRACTOR})
    root = {t["id"]: t["id"] for t in ts}

    def find(x: int) -> int:
        while root[x] != x:
            root[x] = root[root[x]]
            x = root[x]
        return x

    related, stats = [], {"pairs": 0, "same": 0, "related": 0, "distinct": 0, "same_not_merged": 0, "same_course": 0}
    verdicts: dict[tuple[int, int], dict] = {}
    can_see = _readable(s, ts)
    codes = _codes(s, ts)
    pairs = candidate_pairs(ts, _mentions(s, ts))
    judged = {(min(a["id"], b["id"]), max(a["id"], b["id"])) for a, b in pairs}
    pairs += [(a, b) for i, a in enumerate(ts) for b in ts[i + 1:]
              if (min(a["id"], b["id"]), max(a["id"], b["id"])) not in judged and same_course(a, b, codes)]
    for a, b in pairs:
        v = same_course(a, b, codes)
        stats["same_course"] += bool(v)
        v = v or judge(s, a, b, can_see)
        verdicts[(min(a["id"], b["id"]), max(a["id"], b["id"]))] = v
        stats["pairs"] += 1
        stats[v.get("relation", "distinct")] = stats.get(v.get("relation", "distinct"), 0) + 1

    def is_same(x: int, y: int) -> bool:
        v = verdicts.get((min(x, y), max(x, y)))
        return bool(v) and v.get("relation") == "same" and (v.get("confidence") or 0) >= SAME

    members_of: dict[int, set[int]] = {t["id"]: {t["id"]} for t in ts}
    names = {t["id"]: t["name"] for t in ts}
    # Complete-link merging, strongest first: two clusters merge only if EVERY pair across them was judged the
    # same thing, so one eager judgment can't chain unrelated threads together.
    for (x, y), v in sorted(verdicts.items(), key=lambda kv: (-(kv[1].get("confidence") or 0), kv[0])):
        if not is_same(x, y):
            if v.get("relation") in ("related", "part_of") and (v.get("confidence") or 0) >= 0.6:
                related.append((x, y, v))
            continue
        rx, ry = find(x), find(y)
        if rx == ry:
            continue
        if _clusters_agree(members_of[rx], members_of[ry], verdicts, names, codes):
            root[rx] = ry
            members_of[ry] |= members_of.pop(rx)
        else:
            stats["same_not_merged"] += 1
            related.append((x, y, {**v, "label": v.get("label") or "closely related"}))

    # Merge: each cluster keeps a pinned thread first, then its class (over the class's project), then its biggest; the
    # others fold in and become its facets, a class's project or demo day as its part.
    from igneous.graph.threads import PART, course_code

    clusters: dict[int, list[dict]] = defaultdict(list)
    for t in ts:
        clusters[find(t["id"])].append(t)
    folded_into: dict[int, int] = {}
    survivors: set[str] = set()
    for members in clusters.values():
        if len(members) < 2:
            continue
        keep = min(members, key=lambda t: (not t["pinned"], not class_code(t, codes), -len(t["items"]),
                                           -len(t["people"]), t["key"]))
        survivors.add(keep["key"])
        whole = class_code(keep, codes)
        for t in sorted(members, key=lambda t: t["key"]):
            if t["id"] == keep["id"]:
                continue
            folded_into[t["id"]] = keep["id"]
            fold_into(s, t["id"], keep["id"], f"Project:{t['key']}", mark=False)
            part = whole and whole in codes.get(t["id"], ()) and not course_code(t.get("term") or "")
            s.execute(text("""UPDATE thread_registry SET status = 'merged', merged_into = :keep, merged_by = :by,
                              updated_at = now() WHERE key = :gone AND status = 'active'"""),
                      {"keep": keep["key"], "gone": t["key"], "by": PART if part else None})
    # Link related threads (after merging, so links land on survivors).
    for x, y, v in related:
        ia, ib = folded_into.get(x, x), folded_into.get(y, y)
        if ia == ib:
            continue  # they ended up merged after all
        if v.get("relation") == "part_of":
            # The judge names the container (not an A/B label, which models flip): match it to one of the two.
            box = norm(v.get("container") or "")
            hit = [i for i in (x, y) if box and (norm(names[i]) == box or norm(names[i]) in box or box in norm(names[i]))]
            if len(hit) != 1:
                continue  # can't tell which is the container: no link rather than a backwards one
            container = hit[0]
            child = y if container == x else x
            child, container = folded_into.get(child, child), folded_into.get(container, container)
            if child == container:
                continue  # one was merged into the other earlier in this run
            if w.edge(child, container, "PART_OF", {"why": v.get("label") or "part of it", "reason": v.get("reason")},
                      confidence=v.get("confidence"), extractor="hierarchy"):
                stats["part_of"] = stats.get("part_of", 0) + 1
            continue
        w.edge(min(ia, ib), max(ia, ib), "RELATED_TO", {"why": v.get("label") or "related", "reason": v.get("reason")},
               confidence=v.get("confidence"), extractor=EXTRACTOR)
    stats["merged_away"] = len(folded_into)
    stats["survivors"] = sorted(survivors)  # the threads that gained facets
    if log:
        log(json.dumps(stats))
    return stats
