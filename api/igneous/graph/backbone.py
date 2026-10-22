"""Hang your life's threads on its backbone: the organizations and schools you worked at or studied at.

Your resume (graph/profile.py) says where you were; the threads stage (graph/threads.py) finds what you did. Neither
knows the other, which left "xLab" as both an organization and a thread, and Case Western with no classes under
it. This stage joins them, from evidence only, for every org in your backbone:

  1. Same thing: a thread named exactly like the org (or a name it's also known by) IS the org. It becomes the
     org's own thread: its sub-projects, records and people show under the org, and the duplicate leaves the view.
  2. Stated: projects your resume lists under the org (`props.resume_projects`) are part of it.
  3. Mostly theirs: a thread most of whose correspondence involves the org's email domain (a class whose mail
     comes from case.edu, a job thread on kkr.com) is part of it. Only the most specific org takes a thread
     (a lab before its university), and an org you never wrote to doesn't.

Each link is PART_OF thread -> org's thread with its evidence (the records that show it), extractor 'backbone';
the verifier checks the inferred ones like any other. Rebuilt every run; owns only its own rows.
"""
import json
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

EXTRACTOR = "backbone"
VERSION = "1"
DOMAIN_SHARE = 0.5     # of a thread's people-bearing records, involving the org's domain
MIN_DOMAIN_RECORDS = 3
EVIDENCE = 6


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _orgs(s: Session) -> list[dict]:
    """Your backbone orgs: what your resume (or you) said you worked at / studied at, most specific first."""
    rows = s.execute(text("""
        SELECT DISTINCT o.id, o.name, o.key, o.props,
               (SELECT p.dst FROM edges p WHERE p.src = o.id AND p.type = 'PART_OF' LIMIT 1) AS parent
        FROM edges e JOIN nodes o ON o.id = e.dst
        JOIN nodes me ON me.id = e.src AND COALESCE((me.props->>'self')::bool, false)
        WHERE e.type = 'WORKED_AT' AND e.extractor IN ('profile', 'user') AND o.type = 'Organization'
    """)).mappings().all()
    orgs = [dict(r) for r in rows]
    parents = {o["parent"] for o in orgs if o["parent"]}
    # Children before parents: a lab claims its threads before the university does.
    return sorted(orgs, key=lambda o: o["id"] in parents)


def _threads(s: Session) -> list[dict]:
    return [dict(r) for r in s.execute(text("""
        SELECT n.id, n.key, n.name, n.props FROM nodes n
        WHERE n.type = 'Project' AND NOT COALESCE((n.props->>'hidden')::bool, false)
    """)).mappings()]


def _org_thread(s: Session, org: dict, threads: list[dict]) -> int:
    """The org's own thread: the one named like it, or a new empty one that its children hang on."""
    names = {_norm(org["name"]), *(_norm(a) for a in (org["props"] or {}).get("aliases", []))}
    same = [t for t in threads if _norm(t["name"]) in names
            or names & {_norm(a) for a in (t["props"] or {}).get("aliases", [])}]
    if same:
        tid = max(same, key=lambda t: (t["props"] or {}).get("records") or 0)["id"]
        why = "same name as the organization"
    else:
        tid = s.execute(text("""
            INSERT INTO nodes (type, key, name, props) VALUES ('Project', :k, :n, CAST(:p AS jsonb))
            ON CONFLICT (type, key) DO UPDATE SET name = EXCLUDED.name, props = nodes.props || EXCLUDED.props
            RETURNING id
        """), {"k": f"backbone:{org['key']}", "n": org["name"],
               "p": json.dumps({"derived": EXTRACTOR, "kind": "organization"})}).scalar_one()
        why = "organization on your resume"
    # The thread stands for the org from now on: the explorer shows the org and opens to the thread's children.
    s.execute(text("UPDATE nodes SET props = props || jsonb_build_object('org', CAST(:o AS bigint), 'org_why', CAST(:w AS text)) "
                   "WHERE id = :t"), {"o": org["id"], "t": tid, "w": why})
    s.execute(text("UPDATE nodes SET props = props || jsonb_build_object('thread', CAST(:t AS bigint)) WHERE id = :o"),
              {"o": org["id"], "t": tid})
    return tid


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
            WHERE pe.type = 'PART_OF' AND pe.extractor = 'threads'
              AND NOT COALESCE((p.props->>'self')::bool, false)
            GROUP BY pe.dst, n.source_item_id)
        SELECT t, avg(hit::int) AS share, count(*) FILTER (WHERE hit) AS n,
               (array_agg(sid ORDER BY sid DESC) FILTER (WHERE hit))[1:6] AS ev
        FROM rec GROUP BY t
    """), {"d": domain}).all()
    return {r.t: (float(r.share), int(r.n), list(r.ev or [])) for r in rows}


def _link(s: Session, child: int, parent: int, why: str, conf: float, evidence: list[int], stated: bool = False) -> None:
    """Stated by your resume -> extractor 'profile' (never second-guessed); inferred -> 'backbone' (verified)."""
    eid = s.execute(text("""
        INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
        VALUES (:a, :b, 'PART_OF', CAST(:p AS jsonb), :x, :v, :c)
        ON CONFLICT (src, dst, type, extractor) DO UPDATE SET props = EXCLUDED.props RETURNING id
    """), {"a": child, "b": parent, "x": "profile" if stated else EXTRACTOR, "v": VERSION, "c": conf,
           "p": json.dumps({"why": why})}).scalar_one()
    for sid in evidence[:EVIDENCE]:
        s.execute(text("INSERT INTO edge_evidence (edge_id, source_item_id, kind, detail) VALUES (:e, :s, 'backbone', '{}')"),
                  {"e": eid, "s": sid})


def run(s: Session, log=print) -> dict:
    s.execute(text("""DELETE FROM edges e USING nodes c WHERE c.id = e.src AND (e.extractor = :x
                      OR (e.extractor = 'profile' AND e.type = 'PART_OF' AND c.type = 'Project'))"""), {"x": EXTRACTOR})
    s.execute(text("UPDATE nodes SET props = props - 'org' - 'org_why' WHERE type = 'Project' AND props ? 'org'"))
    s.execute(text("UPDATE nodes SET props = props - 'thread' WHERE type = 'Organization' AND props ? 'thread'"))
    orgs = _orgs(s)
    threads = _threads(s)
    by_name = {_norm(t["name"]): t["id"] for t in threads}
    org_thread = {o["id"]: _org_thread(s, o, threads) for o in orgs}
    # Threads that already have a stated or found parent keep it; the backbone fills in the rest.
    has_parent = set(s.execute(text("""
        SELECT e.src FROM edges e JOIN nodes p ON p.id = e.dst AND p.type = 'Project'
        WHERE e.type = 'PART_OF' AND e.extractor IN ('user', 'hierarchy')""")).scalars())
    placed: set[int] = set(org_thread.values())
    stats = {"orgs": len(orgs), "stated": 0, "by_domain": 0, "nested": 0}
    for o in orgs:
        ot = org_thread[o["id"]]
        # An org inside an org (xLab in Case Western): its thread is under the parent's.
        if o["parent"] in org_thread:
            _link(s, ot, org_thread[o["parent"]], "part of it, per your resume", 1.0, [], stated=True)
            stats["nested"] += 1
        for name in (o["props"] or {}).get("resume_projects", []):
            t = by_name.get(_norm(name))
            if t and t != ot and t not in placed:
                _link(s, t, ot, f"your resume lists it under {o['name']}", 1.0, [], stated=True)
                placed.add(t)
                stats["stated"] += 1
        domain = o["key"] if "." in o["key"] and ":" not in o["key"] else None
        if not domain:
            continue
        for t, (share, n, ev) in _domain_share(s, domain).items():
            if t in placed or t in has_parent or t == ot or n < MIN_DOMAIN_RECORDS or share < DOMAIN_SHARE:
                continue
            _link(s, t, ot, f"{round(share * 100)}% of its conversations involve {domain}", round(share, 3), ev)
            placed.add(t)
            stats["by_domain"] += 1
    s.commit()
    if log:
        log(json.dumps(stats))
    return stats
