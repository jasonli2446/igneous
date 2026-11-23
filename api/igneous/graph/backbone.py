"""Hang your life's threads on its backbone: the organizations and schools you worked at or studied at.

Your resume (graph/profile.py) says where you were; the threads stage (graph/threads.py) finds what you did. Neither
knows the other, which left "the Lab" as both an organization and a thread, and State University with no classes under
it. This stage joins them, from evidence only, for every org in your backbone:

  1. Same thing: a thread named exactly like the org (or a shorter name of it, or a name it's also known by) IS
     the org. It becomes the org's own thread: its sub-projects, records and people show under the org, and any
     other thread with that name is marked props.same_as_org (the last step hides it). An org with no such thread
     gets one of its own (`backbone:<org key>`, a Project under the correction contract: a name you give it stays).
  2. Stated: projects your resume lists under the org (`props.resume_projects`) are part of it, and a lab inside a
     university is part of the university's thread. These are stated (extractor 'profile'), never judged.
  3. Mostly theirs: a thread most of whose correspondence involves the org's email domain (a class whose mail
     comes from university.edu, a job thread on employer.com) is part of it. Only the most specific org takes a thread
     (a lab before its university), and an org you never wrote to doesn't. Your own addresses don't count.
  4. Classes: a thread judged a class or course, inside your dates at exactly one school, was taken there (a class
     run in group chats has no school email to go on). Its records from those dates are the evidence.

Inferred links (3, 4) are PART_OF thread -> org's thread with extractor 'backbone' and their evidence; the verifier
checks them like any other. A thread you hid takes no part. Rebuilt every run: the stage owns its links (both
extractors, between Projects), its flags (props.org, org_why, same_as_org on threads; props.thread on organizations)
and the org threads it made, which go when their org leaves your backbone.
"""
import json
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph.derived import Writer, norm

EXTRACTOR = "backbone"
VERSION = "1"
DOMAIN_SHARE = 0.5     # of a thread's people-bearing records, involving the org's domain
MIN_DOMAIN_RECORDS = 3
EVIDENCE = 6


def _orgs(s: Session) -> list[dict]:
    """Your backbone orgs: what your resume (or you) said you worked at / studied at, most specific first."""
    rows = s.execute(text("""
        SELECT DISTINCT ON (o.id) o.id, o.name, o.key, o.props, (e.props->>'school') IS NOT NULL AS school,
               e.valid_from, e.valid_to,
               (SELECT p.dst FROM edges p WHERE p.src = o.id AND p.type = 'PART_OF' ORDER BY p.id LIMIT 1) AS parent
        FROM edges e JOIN nodes o ON o.id = e.dst
        JOIN nodes me ON me.id = e.src AND COALESCE((me.props->>'self')::bool, false)
        WHERE e.type = 'WORKED_AT' AND e.extractor IN ('profile', 'user') AND o.type = 'Organization'
        ORDER BY o.id, (e.props->>'school') IS NULL
    """)).mappings().all()
    orgs = [dict(r) for r in rows]
    parents = {o["parent"] for o in orgs if o["parent"]}
    # Children before parents: a lab claims its threads before the university does.
    return sorted(orgs, key=lambda o: (o["id"] in parents, o["id"]))


def _within(props: dict, org: dict) -> bool:
    """The thread happened while you were there (a month's slack either side)."""
    from datetime import datetime, timedelta
    try:
        start, end = (datetime.fromisoformat(props[k]) for k in ("start", "end"))
    except (KeyError, TypeError, ValueError):
        return False
    lo = org["valid_from"] - timedelta(days=31) if org["valid_from"] else None
    hi = org["valid_to"] + timedelta(days=31) if org["valid_to"] else None
    return (lo is None or start >= lo) and (hi is None or end <= hi)


def _threads(s: Session) -> list[dict]:
    """Every Project except the ones you hid, and the org threads this stage makes."""
    from igneous.graph import overrides

    hidden = overrides.plan(overrides.load(s)).hidden
    return [dict(r) for r in s.execute(text("""
        SELECT n.id, n.key, n.name, n.props FROM nodes n
        WHERE n.type = 'Project' AND n.props->>'derived' IS DISTINCT FROM :x AND NOT (n.type || ':' || n.key = ANY(:h))
        ORDER BY n.id
    """), {"x": EXTRACTOR, "h": sorted(hidden)}).mappings()]


def _org_thread(w: Writer, org: dict, threads: list[dict]) -> tuple[int, str | None]:
    """The org's own thread: the one named like it, or one of its own that its children hang on (its key is
    returned too)."""
    s = w.s
    names = {norm(org["name"]), *(norm(a) for a in (org["props"] or {}).get("aliases", []))}

    def same_name(n: str) -> bool:
        # Exactly, or a shorter name of it, word for word with 2+ words ("State University" for Western State
        # University). A single shared word ("Case") is not enough, and a name that adds words to the org's is
        # something there ("Acme Robotics standup", "State University Robotics Lab").
        return n in names or any(len(n.split()) >= 2 and f" {n} " in f" {m} " for m in names)
    same = [t for t in threads if same_name(norm(t["name"]))
            or any(same_name(norm(a)) for a in (t["props"] or {}).get("aliases", []))]
    made = None
    if same:
        tid = min(same, key=lambda t: (-((t["props"] or {}).get("records") or 0), t["id"]))["id"]
        why = "same name as the organization"
        # Every other thread with its name is the same thing too: its children move up to the org's thread, and it
        # leaves the view (the last step hides what this flag marks).
        for t in same:
            if t["id"] != tid:
                for c, p in s.execute(text("""SELECT e.src, e.props FROM edges e JOIN nodes c ON c.id = e.src
                                              AND c.type = 'Project' WHERE e.dst = :gone AND e.type = 'PART_OF'
                                              AND e.src <> :keep ORDER BY e.src"""), {"gone": t["id"], "keep": tid}):
                    w.edge(c, tid, "PART_OF", p, confidence=1.0)
                s.execute(text("UPDATE nodes SET props = props || jsonb_build_object('same_as_org', CAST(:o AS bigint)) "
                               "WHERE id = :t"), {"t": t["id"], "o": org["id"]})
    else:
        made = f"backbone:{org['key']}"
        tid = w.project(made, org["name"], {"derived": EXTRACTOR, "kind": "organization", "named_from": "profile"})
        why = "organization on your resume"
    # The thread stands for the org from now on: the explorer shows the org and opens to the thread's children.
    s.execute(text("UPDATE nodes SET props = props || jsonb_build_object('org', CAST(:o AS bigint), 'org_why', CAST(:w AS text)) "
                   "WHERE id = :t"), {"o": org["id"], "t": tid, "w": why})
    s.execute(text("UPDATE nodes SET props = props || jsonb_build_object('thread', CAST(:t AS bigint)) WHERE id = :o"),
              {"o": org["id"], "t": tid})
    return tid, made


def _domain_share(s: Session, domain: str) -> dict[int, tuple[float, int, list[int]]]:
    """{thread: (share, records, evidence items)}: how much of each thread's people-bearing records involve the
    domain (sent from or to an address there, or an event someone there attends)."""
    rows = s.execute(text("""
        WITH rec AS (
            SELECT pe.dst AS t, n.source_item_id AS sid,
                   bool_or(p.key LIKE '%@' || :d OR p.key LIKE '%.' || :d) AS hit
            FROM edges pe JOIN nodes n ON n.id = pe.src AND n.type IN ('Message', 'Event')
            JOIN edges x ON (x.src = n.id AND x.type = 'RECEIVED') OR (x.dst = n.id AND x.type IN ('SENT', 'ATTENDED'))
            JOIN nodes p ON p.id = CASE WHEN x.src = n.id THEN x.dst ELSE x.src END AND p.type = 'Person'
            LEFT JOIN canonical c ON c.node_id = p.id LEFT JOIN nodes h ON h.id = c.canonical_id
            WHERE pe.type = 'PART_OF' AND pe.extractor = 'threads'
              -- Your own addresses aren't the org's people: your school address is on everything you send.
              AND NOT COALESCE((p.props->>'self')::bool, false) AND NOT COALESCE((h.props->>'self')::bool, false)
            GROUP BY pe.dst, n.source_item_id)
        SELECT t AS thread, avg(hit::int) AS share, count(*) FILTER (WHERE hit) AS n,
               (array_agg(sid ORDER BY sid DESC) FILTER (WHERE hit))[1:6] AS ev
        FROM rec GROUP BY t ORDER BY t
    """), {"d": domain}).mappings().all()
    return {r["thread"]: (float(r["share"]), int(r["n"]), list(r["ev"] or [])) for r in rows}


def _class_evidence(s: Session, thread: int, org: dict) -> list[int]:
    """A class's records from your dates at the school, those naming the school or involving its domain first."""
    domain = org["key"] if "." in org["key"] and ":" not in org["key"] else ""
    return list(s.execute(text("""
        SELECT si.id FROM edges pe JOIN nodes n ON n.id = pe.src JOIN source_items si ON si.id = n.source_item_id
        WHERE pe.dst = :t AND pe.type = 'PART_OF' AND pe.extractor = 'threads'
          AND (CAST(:a AS timestamptz) IS NULL OR si.occurred_at >= CAST(:a AS timestamptz) - interval '31 days')
          AND (CAST(:b AS timestamptz) IS NULL OR si.occurred_at <= CAST(:b AS timestamptz) + interval '31 days')
        ORDER BY (si.title || ' ' || coalesce(si.body_text, '')) ILIKE :name
                 OR (:d <> '' AND CAST(si.raw_meta AS text) ILIKE :domain) DESC,
                 si.occurred_at DESC, si.id DESC
        LIMIT :n
    """), {"t": thread, "a": org["valid_from"], "b": org["valid_to"], "name": f"%{org['name']}%", "d": domain,
           "domain": f"%@{domain}%", "n": EVIDENCE}).scalars())


def run(s: Session, log=print) -> dict:
    # Only this stage's own rows: its links between Projects (inferred and stated), its flags.
    s.execute(text("""DELETE FROM edges e USING nodes c, nodes p WHERE c.id = e.src AND p.id = e.dst
                      AND c.type = 'Project' AND p.type = 'Project' AND e.type = 'PART_OF'
                      AND e.extractor IN (:x, 'profile')"""), {"x": EXTRACTOR})
    s.execute(text("UPDATE nodes SET props = props - 'org' - 'org_why' WHERE type = 'Project' AND props ? 'org'"))
    s.execute(text("UPDATE nodes SET props = props - 'thread' WHERE type = 'Organization' AND props ? 'thread'"))
    s.execute(text("UPDATE nodes SET props = props - 'same_as_org' WHERE type = 'Project' AND props ? 'same_as_org'"))
    w = Writer(s, EXTRACTOR, VERSION)
    orgs = _orgs(s)
    threads = _threads(s)
    # A name on your resume is the life thread of that name before any other Project (a repo folder of the same name).
    by_name: dict[str, int] = {}
    for t in sorted(threads, key=lambda t: ((t["props"] or {}).get("derived") != "threads",
                                            -((t["props"] or {}).get("records") or 0), t["id"])):
        by_name.setdefault(norm(t["name"]), t["id"])
    made = {o["id"]: _org_thread(w, o, threads) for o in orgs}
    org_thread = {o: tid for o, (tid, _key) in made.items()}
    # Org threads of orgs no longer on your backbone (or now with a thread of their own name) go.
    s.execute(text("""DELETE FROM nodes WHERE type = 'Project' AND props->>'derived' = :x AND NOT (key = ANY(:k))"""),
              {"x": EXTRACTOR, "k": sorted(key for _tid, key in made.values() if key)})
    # Threads that already have a parent you stated keep it; the backbone fills in the rest.
    has_parent = set(s.execute(text("""
        SELECT e.src FROM edges e JOIN nodes p ON p.id = e.dst AND p.type = 'Project'
        WHERE e.type = 'PART_OF' AND e.extractor = 'user'""")).scalars())
    placed: set[int] = set(org_thread.values())
    stats = {"orgs": len(orgs), "stated": 0, "by_domain": 0, "nested": 0, "classes": 0}
    for o in orgs:
        ot = org_thread[o["id"]]
        # An org inside an org (the Lab in State University): its thread is under the parent's.
        if o["parent"] in org_thread:
            w.edge(ot, org_thread[o["parent"]], "PART_OF", {"why": "part of it, per your resume"}, confidence=1.0,
                   extractor="profile")
            stats["nested"] += 1
        for name in sorted((o["props"] or {}).get("resume_projects", [])):
            t = by_name.get(norm(name))
            if t and t != ot and t not in placed:
                w.edge(t, ot, "PART_OF", {"why": f"your resume lists it under {o['name']}"}, confidence=1.0,
                       extractor="profile")
                placed.add(t)
                stats["stated"] += 1
        domain = o["key"] if "." in o["key"] and ":" not in o["key"] else None
        if not domain:
            continue
        for t, (share, n, ev) in _domain_share(s, domain).items():
            if t in placed or t in has_parent or t == ot or n < MIN_DOMAIN_RECORDS or share < DOMAIN_SHARE:
                continue
            if w.edge(t, ot, "PART_OF", {"why": f"{round(share * 100)}% of its conversations involve {domain}"},
                      confidence=round(share, 3), evidence=[(sid, "backbone", {}) for sid in ev[:EVIDENCE]]):
                placed.add(t)
                stats["by_domain"] += 1
    schools = [o for o in orgs if o.get("school")]
    for t in threads if schools else []:
        p = t["props"] or {}
        if t["id"] in placed or t["id"] in has_parent or not re.search(r"\b(class|course)\b", p.get("kind") or ""):
            continue
        inside = [o for o in schools if _within(p, o)]
        if len(inside) == 1:
            school = inside[0]
            if w.edge(t["id"], org_thread[school["id"]], "PART_OF",
                      {"why": f"a class you took while at {school['name']}"}, confidence=0.9,
                      evidence=[(sid, "backbone", {}) for sid in _class_evidence(s, t["id"], school)]):
                placed.add(t["id"])
                stats["classes"] += 1
    stats["skipped_rejected"] = w.skipped
    if log:
        log(json.dumps(stats))
    return stats
