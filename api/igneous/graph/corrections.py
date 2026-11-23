"""Corrections are intents: what you said is recorded at once, and applied to the graph as soon as it can be.

A correction made through the API (rename, hide, merge, "This is wrong", a connection you add, undo) takes two steps:

  1. `recording`: its intent is committed at once, in rows no rebuild writes (overrides, your verdicts in
     edge_verdicts, thread_parents), keyed by natural keys (`Type:key`), never by node or edge ids. That transaction
     locks no graph row: it holds the publish lock shared, so it waits only while corrections are being applied (a
     rebuild's final step, or another correction's step 2).
  2. `apply_now`: when no rebuild is reading sources or deriving, every recorded correction is applied to the
     published graph in place, under the exclusive publish lock (it waits for intents still being recorded, as a
     final step does), in a transaction whose lock timeout is below the deadlock timeout, so it always loses a lock
     race and never aborts a rebuild. When a rebuild is running, its final step applies it instead (`apply_all`
     under the exclusive publish lock), and the answer says so. No correction is refused because a rebuild is
     running.

A pair rejection ("This is wrong" on a link, or removing something from a project) is your 'drop' verdict on the
pair (graph/verify.py record_correction): no derived link of any type joins the two again, in either direction, under
any of their identities (graph/derived.py). `reject` records it with exact copies of the links it removes and the
verdicts they had, so its undo (`restore`) puts back exactly those.
"""
import json
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from igneous import pipeline
from igneous.graph import overrides, verify
from igneous.graph.derived import STATED

APPLIED = {"applied": "now"}
ON_PUBLISH = {"applied": "on_publish",
              "note": "A rebuild is running: your correction is saved and applies when it publishes."}
NEXT_REBUILD = {"applied": "on_publish", "note": "Your correction is saved and applies with the next rebuild."}


@contextmanager
def recording(s: Session):
    """The transaction that records a correction's intent: it waits only while corrections are being applied (the
    publish lock, shared), and commits."""
    s.execute(text("SELECT pg_advisory_xact_lock_shared(hashtext(:k))"), {"k": pipeline.PUBLISH_LOCK})
    try:
        yield
        s.commit()
    except BaseException:
        s.rollback()
        raise


def apply_all(s: Session) -> dict:
    """Apply every recorded correction to the graph in the session: undos, Project corrections and your links
    (graph/overrides.py), then every verdict (graph/verify.py). The rebuild's final step, and each correction's
    in-place step."""
    return {"overrides": overrides.apply(s), "verdicts": verify.apply_verdicts(s)}


def apply_now(s: Session) -> dict:
    """Apply every recorded correction to the published graph now, unless a rebuild is reading sources or deriving
    (then its final step applies them). Returns what to tell the user: {applied: 'now'} or {applied: 'on_publish',
    note}.

    While it applies, it holds the sources and deriving locks shared, so no rebuild starts meanwhile. It takes them
    without waiting, as session locks on its own transaction's connection that it releases before that transaction
    ends (a lock taken for the transaction would outlive it where the transaction is nested in another)."""
    from igneous.graph import dirty

    s.rollback()  # a transaction of its own, after the intent's
    held: list[str] = []
    outcome = ON_PUBLISH
    try:
        for gate in (pipeline.SOURCES_LOCK, pipeline.DERIVING_LOCK):
            if not s.execute(text("SELECT pg_try_advisory_lock_shared(hashtext(:k))"), {"k": gate}).scalar():
                break
            held.append(gate)
        else:
            try:
                with s.begin_nested():  # whatever fails in here, the transaction can still release the locks
                    s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": pipeline.PUBLISH_LOCK})
                    previous = s.execute(text("SELECT current_setting('lock_timeout')")).scalar()
                    s.execute(text("""SELECT set_config('lock_timeout', CAST(GREATEST(1, extract(epoch FROM
                        CAST(current_setting('deadlock_timeout') AS interval)) * 500) AS int) || 'ms', true)"""))
                    apply_all(s)
                    dirty.touch(s)
                    s.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": previous})
                outcome = APPLIED
            except OperationalError as e:
                if getattr(e.orig, "sqlstate", None) not in ("55P03", "40P01"):
                    raise
                outcome = NEXT_REBUILD  # it gave up a lock wait: the next rebuild's final step applies it
    finally:
        for gate in reversed(held):
            s.execute(text("SELECT pg_advisory_unlock_shared(hashtext(:k))"), {"k": gate})
        s.commit() if outcome is APPLIED else s.rollback()
    return outcome


def _edge(s: Session, edge_id: int):
    return s.execute(text("""
        SELECT e.id, e.src, e.dst, e.type, e.extractor, a.type || ':' || a.key AS a, b.type || ':' || b.key AS b
        FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst WHERE e.id = :i
    """), {"i": edge_id}).mappings().first()


def reject(s: Session, edge_id: int) -> dict:
    """Record "this link is wrong" for the pair edge `edge_id` joins (as published now). Raises LookupError for no
    such link and ValueError for a stated one. Returns {removed, also_removed, edges, override_id, pairs}: `edges` are
    the links it removes (the pairs' every derived link), `pairs` the node pairs it rejects."""
    root = _edge(s, edge_id)
    if root is None:
        raise LookupError("No such link")
    if root["extractor"] in STATED:
        raise ValueError("This link is stated in your data or added by you, not inferred")
    # Removing one email from a project removes its email conversation, as the inspector shows a conversation as one
    # item. A chat's other days are other conversations and stay.
    siblings = s.execute(text("""
        SELECT DISTINCT n.id AS src, :dst AS dst, x.type, x.extractor, n.type || ':' || n.key AS a, :b AS b
        FROM edges it JOIN nodes root ON root.id = it.src AND root.props->>'source' IN ('gmail', 'email')
        JOIN edges it2 ON it2.dst = it.dst AND it2.type = 'IN_THREAD'
        JOIN nodes n ON n.id = it2.src AND n.props->>'source' IN ('gmail', 'email')
        JOIN edges x ON x.src = n.id AND x.dst = :dst AND x.type = :type
        WHERE it.src = :src AND it.type = 'IN_THREAD' AND x.extractor <> ALL(:stated)
        ORDER BY 1
    """), {"src": root["src"], "dst": root["dst"], "type": root["type"], "b": root["b"],
           "stated": list(STATED)}).mappings().all()
    roots = {tuple(r[k] for k in ("a", "b", "type", "extractor")): dict(r) for r in [root, *siblings]}
    oid, snapshots = _reject(s, roots, "you said this connection is wrong")
    if not snapshots:
        raise LookupError("No such link")
    ids = [e["id"] for e in snapshots]
    return {"removed": edge_id, "also_removed": len(ids) - 1, "edges": ids, "override_id": oid,
            "pairs": sorted({tuple(sorted((r["src"], r["dst"]))) for r in roots.values()})}


def _reject(s: Session, roots: dict[tuple, dict], reason: str, data: dict | None = None) -> tuple[int, list]:
    """Record your 'drop' on each root (natural keys (a, b, type, extractor) -> its node ids {src, dst}): one override
    'unlink' holding exact copies of every derived link between each pair (with the evidence a delete cascades away)
    and the verdicts the roots had, for undo. Returns (override id, those copies)."""
    pairs = json.dumps([{"a": a, "b": b} for a, b in sorted({tuple(sorted((r["src"], r["dst"])))
                                                             for r in roots.values()})])
    snapshots = s.execute(text("""
        SELECT to_jsonb(e) || jsonb_build_object('src_key', a.type || ':' || a.key,
            'dst_key', b.type || ':' || b.key, 'evidence', COALESCE((
                SELECT jsonb_agg(to_jsonb(ev) ORDER BY ev.id) FROM edge_evidence ev WHERE ev.edge_id=e.id), '[]'))
        FROM jsonb_to_recordset(CAST(:pairs AS jsonb)) AS p(a bigint,b bigint)
        JOIN edges e ON (e.src=p.a AND e.dst=p.b) OR (e.src=p.b AND e.dst=p.a)
        JOIN nodes a ON a.id=e.src JOIN nodes b ON b.id=e.dst
        WHERE e.extractor <> ALL(:stated)
        ORDER BY e.id
    """), {"pairs": pairs, "stated": list(STATED)}).scalars().all()
    first = next(iter(roots))
    oid = overrides.add(s, "unlink", [first[1], first[0]])
    verdicts = []
    for a, b, kind, extractor in sorted(roots):  # natural-key order, as the verifier writes (verify._store)
        previous = s.execute(text("""
            SELECT to_jsonb(v) FROM edge_verdicts v
            WHERE src_key=:a AND dst_key=:b AND type=:t AND extractor=:x FOR UPDATE
        """), {"a": a, "b": b, "t": kind, "x": extractor}).scalar()
        verdicts.append({"a": a, "b": b, "type": kind, "extractor": extractor, "previous": previous})
        verify.record_correction(s, a, b, kind, extractor, "drop", reason, oid)
    s.execute(text("UPDATE overrides SET data=CAST(:data AS jsonb) WHERE id=:i"), {"i": oid, "data": json.dumps(
        {"pair_correction": True, "edges": snapshots, "verdicts": verdicts, **(data or {})})})
    return oid, snapshots


def split(s: Session, a: int, b: int) -> dict:
    """Record "these are not the same person" for two Person nodes: your 'drop' on their SAME_AS, which keeps them
    in separate identities from then on whatever links them (a contact card, a merge you made; graph/resolve.py),
    and takes out any 'same person' link you added between the two. Undo puts it all back. Raises LookupError unless
    both are people."""
    ends = {r.id: r for r in s.execute(text(
        "SELECT id, type, type || ':' || key AS k FROM nodes WHERE id = ANY(:i)"), {"i": [a, b]})}
    if a == b or any(ends.get(i) is None or ends[i].type != "Person" for i in (a, b)):
        raise LookupError("Both must be people, and two of them")
    a, b = sorted((a, b))
    keys = sorted((ends[a].k, ends[b].k))
    links = [dict(r) for r in s.execute(text("""
        DELETE FROM overrides WHERE op = 'link' AND value = 'SAME_AS' AND undone_at IS NULL
            AND node_keys @> CAST(:k AS text[]) AND cardinality(node_keys) = 2
        RETURNING op, node_keys, value, data"""), {"k": keys}).mappings()]
    oid, _snapshots = _reject(s, {(*keys, "SAME_AS", "user"): {"src": a, "dst": b}},
                              "you said they are not the same person", {"links": links})
    return {"override_id": oid, "pairs": [(a, b)]}


def remove_link(s: Session, edge_id: int) -> dict:
    """Record "take out this link I added": the intents behind link `edge_id` (extractor 'user') go, kept in an
    override 'unlink' (data.user_link) whose undo puts them back. Raises LookupError for no such link and ValueError
    for a link you didn't add."""
    from igneous.graph.derived import follow, merges

    edge = _edge(s, edge_id)
    if edge is None:
        raise LookupError("No such link")
    if edge["extractor"] != "user":
        raise ValueError("Only a link you added can be taken out this way; say an inferred one is wrong instead")
    folded = merges(s)
    ends = {edge["a"], edge["b"]}
    links = [dict(r) for r in s.execute(text("""
        SELECT id, op, node_keys, value, data FROM overrides
        WHERE op = 'link' AND undone_at IS NULL AND value = :t ORDER BY id"""), {"t": edge["type"]}).mappings()
        if (follow(folded, r["node_keys"][0]), follow(folded, r["node_keys"][1])) in ((edge["a"], edge["b"]),
                                                                                  (edge["b"], edge["a"]))]
    parents = [list(r) for r in s.execute(text("""
        SELECT child_key, parent_key, origin FROM thread_parents ORDER BY child_key, parent_key""")).all()
        if edge["type"] == "PART_OF" and (follow(folded, f"Project:{r[0]}"), follow(folded, f"Project:{r[1]}"))
        == (edge["a"], edge["b"])]
    if links:
        s.execute(text("DELETE FROM overrides WHERE id = ANY(:i)"), {"i": [r["id"] for r in links]})
    for child, parent, _origin in parents:
        s.execute(text("DELETE FROM thread_parents WHERE child_key = :c AND parent_key = :p"), {"c": child, "p": parent})
    oid = overrides.add(s, "unlink", sorted(ends) if edge["type"] == "SAME_AS" else [edge["b"], edge["a"]], edge["type"],
                        {"user_link": True,
                         "links": [{k: r[k] for k in ("op", "node_keys", "value", "data")} for r in links],
                         "parents": parents})
    return {"removed": edge_id, "override_id": oid, "pairs": [tuple(sorted((edge["src"], edge["dst"])))]}


def outcome(s: Session, pairs) -> list[dict]:
    """For each node pair a correction touched, as the explorer shows the two (each end its identity head): whether
    any link still joins them now, under any of their identities and in either direction, so a view removes or
    restores exactly those pairs. A pair that is one person now (two identities of one head) joins nothing on screen
    and is left out."""
    from igneous.graph import identity

    head = identity.heads(s, {x for p in pairs for x in p})
    pairs = sorted({tuple(sorted((head[a], head[b]))) for a, b in pairs if head[a] != head[b]})
    if not pairs:
        return []
    linked = {tuple(r) for r in s.execute(text("""
        WITH q AS (SELECT * FROM jsonb_to_recordset(CAST(:p AS jsonb)) AS q(a bigint, b bigint)),
        ends AS (SELECT q.a, q.b, x.id AS x, y.id AS y FROM q
                 CROSS JOIN LATERAL (SELECT q.a AS id UNION SELECT node_id FROM canonical WHERE canonical_id = q.a) x
                 CROSS JOIN LATERAL (SELECT q.b AS id UNION SELECT node_id FROM canonical WHERE canonical_id = q.b) y)
        SELECT DISTINCT ends.a, ends.b FROM ends
        JOIN edges e ON (e.src = ends.x AND e.dst = ends.y) OR (e.src = ends.y AND e.dst = ends.x)
        WHERE COALESCE(e.props->>'verdict', '') <> 'drop'
    """), {"p": json.dumps([{"a": a, "b": b} for a, b in pairs])})}
    return [{"a": a, "b": b, "still_linked": (a, b) in linked} for a, b in pairs]


def node_pairs(s: Session, keys) -> list[tuple[int, int]]:
    """Pairs of natural keys (`Type:key`) as the node ids that hold those keys now; a pair whose node is gone is
    left out."""
    keys = list(keys)
    ids = dict(s.execute(text("SELECT type || ':' || key, id FROM nodes WHERE type || ':' || key = ANY(:k)"),
                         {"k": sorted({k for pair in keys for k in pair})}).all())
    return [(ids[a], ids[b]) for a, b in keys if a in ids and b in ids]


def restore(s: Session, row: dict) -> list[int]:
    """Undo one pair rejection on the graph as it is now (graph/overrides.py runs it for an undone row): its verdicts
    go back to what they were and the links it removed come back, unless another rejection of yours still covers the
    pair or a record behind them is gone. Returns the restored link ids."""
    from igneous.graph.derived import rejections

    data = row["data"] or {}
    for v in data.get("verdicts", []):
        params = {"a": v["a"], "b": v["b"], "t": v["type"], "x": v["extractor"], "token": f"override:{row['id']}"}
        removed = s.execute(text("""
            DELETE FROM edge_verdicts WHERE src_key=:a AND dst_key=:b AND type=:t AND extractor=:x
                AND source='user' AND evidence=:token RETURNING src_key
        """), params).first()
        previous = v["previous"]
        if removed is None or previous is None:
            continue
        # Undoing an older action must not bring back a correction which has itself been undone.
        if previous["evidence"].startswith("override:"):
            prior_id = int(previous["evidence"].split(":", 1)[1])
            if s.execute(text("SELECT 1 FROM overrides WHERE id=:i AND undone_at IS NULL"),
                         {"i": prior_id}).first() is None:
                continue
        s.execute(text("""
            INSERT INTO edge_verdicts SELECT * FROM jsonb_populate_record(NULL::edge_verdicts, CAST(:v AS jsonb))
            ON CONFLICT DO NOTHING
        """), {"v": json.dumps(previous)})
    blocked = rejections(s)
    restored = []
    for e in data.get("edges", []):
        ends = s.execute(text("""
            SELECT a.id, b.id FROM nodes a, nodes b
            WHERE a.type || ':' || a.key = :a AND b.type || ':' || b.key = :b
        """), {"a": e["src_key"], "b": e["dst_key"]}).first()
        if ends is None or blocked.blocks(*ends):
            continue  # an end is gone, or another rejection of yours still covers the pair
        eid = s.execute(text("""
            INSERT INTO edges SELECT e.id, :a, :b, e.type, e.props, e.valid_from, e.valid_to, e.extractor,
                e.extractor_version, e.model, e.confidence, e.source_item_id, e.span_start, e.span_end
            FROM jsonb_populate_record(NULL::edges, CAST(:edge AS jsonb)) e
            WHERE e.source_item_id IS NULL OR EXISTS (SELECT 1 FROM source_items WHERE id=e.source_item_id)
            ON CONFLICT DO NOTHING RETURNING id
        """), {"edge": json.dumps(e), "a": ends[0], "b": ends[1]}).scalar()
        if eid is None:
            continue
        restored.append(eid)
        for ev in e["evidence"]:
            s.execute(text("""
                INSERT INTO edge_evidence SELECT ev.* FROM jsonb_populate_record(
                    NULL::edge_evidence, CAST(:ev AS jsonb)) ev
                WHERE ev.source_item_id IS NULL OR EXISTS (SELECT 1 FROM source_items WHERE id=ev.source_item_id)
                ON CONFLICT DO NOTHING
            """), {"ev": json.dumps(ev)})
    # A 'same person' link you added that "not the same person" took out comes back.
    for link in data.get("links", []):
        overrides.add(s, link["op"], link["node_keys"], link["value"], link["data"])
    if any(e["type"] == "SAME_AS" for e in data.get("edges", [])) or any(
            v["type"] == "SAME_AS" for v in data.get("verdicts", [])):
        from igneous.graph.resolve import refresh_canonical
        refresh_canonical(s)
    return restored


def undo(s: Session, override_id: int) -> dict:
    """Record the undo of one correction: its row waits, marked undone, for the step that reverses it. Returns {id,
    op, pairs}: `pairs` are the node pairs (by natural key) whose links the undo brings back or takes out, for a link
    you added or a link taken out; none for a rename, hide or merge. Raises LookupError when there is no such active
    correction."""
    row = s.execute(text("""
        UPDATE overrides SET undone_at = now() WHERE id = :i AND undone_at IS NULL RETURNING id, op, node_keys, data
    """), {"i": override_id}).mappings().first()
    if row is None:
        raise LookupError("No such correction (already undone?)")
    data = row["data"] or {}
    pairs: set[tuple[str, str]] = set()
    if row["op"] == "link" or (row["op"] == "unlink" and data.get("user_link")):
        pairs.add(tuple(row["node_keys"][:2]))
    elif row["op"] == "unlink":  # a pair rejection: the pairs its verdicts cover, and those of the links it removed
        pairs |= {(v["a"], v["b"]) for v in data.get("verdicts", [])}
        pairs |= {(e["src_key"], e["dst_key"]) for e in data.get("edges", [])}
    return {"id": row["id"], "op": row["op"], "pairs": sorted(pairs)}


def restore_links(s: Session, edge_ids: list[int]) -> dict:
    """Record "bring these links back": the rejections of yours that removed them are undone, and a link the
    verifier dropped gets your 'keep' (a label it learns from), which brings it back from edges_dropped. Returns
    {undone: [override ids], kept: n, edges: every link id they bring back, pairs: their node pairs by natural key}."""
    groups = s.execute(text("""
        SELECT id FROM overrides WHERE op = 'unlink' AND undone_at IS NULL AND data->>'pair_correction' = 'true'
            AND EXISTS (SELECT 1 FROM jsonb_array_elements(data->'edges') e WHERE (e->>'id')::bigint = ANY(:i))
        ORDER BY id DESC
    """), {"i": edge_ids}).scalars().all()
    grouped = {int(e) for (e,) in s.execute(text("""
        SELECT (e->>'id')::bigint FROM overrides o, jsonb_array_elements(o.data->'edges') e WHERE o.id = ANY(:g)
    """), {"g": list(groups)})}
    for oid in groups:
        undo(s, oid)
    dropped = s.execute(text("""
        SELECT src_key, dst_key, type, extractor FROM edges_dropped WHERE id = ANY(:i) ORDER BY 1, 2, 3, 4
    """), {"i": [i for i in edge_ids if i not in grouped]}).all()
    for a, b, kind, extractor in dropped:
        verify.record_correction(s, a, b, kind, extractor, "keep", "you said this connection is right")
    keys = {(e["src_key"], e["dst_key"]) for (e,) in s.execute(text("""
        SELECT e FROM overrides o, jsonb_array_elements(o.data->'edges') e WHERE o.id = ANY(:g)"""), {"g": list(groups)})}
    return {"undone": list(groups), "kept": len(dropped), "edges": sorted(grouped | set(edge_ids)),
            "pairs": sorted(keys | {(a, b) for a, b, _t, _x in dropped})}


def confirm(s: Session, edge_id: int) -> dict:
    """Record "this link is right": your 'keep' verdict on it, which the verifier never overrides and learns from.
    Raises LookupError for no such link and ValueError for a stated one."""
    edge = _edge(s, edge_id)
    if edge is None:
        raise LookupError("No such link")
    if edge["extractor"] in STATED:
        raise ValueError("This link is stated in your data or added by you, not inferred")
    verify.record_correction(s, edge["a"], edge["b"], edge["type"], edge["extractor"], "keep",
                             "you said this connection is right")
    return {"confirmed": edge_id}


def link(s: Session, src_key: str, dst_key: str, type_: str, props: dict) -> int | None:
    """Record a connection you added. A Project under a Project is stated hierarchy (thread_parents, which the threads
    stage builds on); any other connection is an override 'link'. Returns the override id (None for hierarchy)."""
    if type_ == "PART_OF" and src_key.startswith("Project:") and dst_key.startswith("Project:"):
        s.execute(text("""
            INSERT INTO thread_parents (child_key, parent_key, origin) VALUES (:c, :p, 'user') ON CONFLICT DO NOTHING
        """), {"c": src_key.split(":", 1)[1], "p": dst_key.split(":", 1)[1]})
        return None
    return overrides.add(s, "link", [src_key, dst_key], type_, {"props": props})


def for_pair(s: Session, a: list[int], b: list[int]) -> list[dict]:
    """Your active rejections between two sets of nodes, for the Why panel (never their undo snapshots)."""
    rows = s.execute(text("""
        SELECT v.reason, v.updated_at, n.id AS src, m.id AS dst, n.name AS src_name, m.name AS dst_name,
               o.id AS override_id
        FROM edge_verdicts v JOIN nodes n ON n.type || ':' || n.key=v.src_key
        JOIN nodes m ON m.type || ':' || m.key=v.dst_key
        LEFT JOIN overrides o ON v.evidence='override:' || o.id AND o.undone_at IS NULL
        WHERE v.source='user' AND v.verdict='drop'
            AND ((n.id=ANY(:a) AND m.id=ANY(:b)) OR (n.id=ANY(:b) AND m.id=ANY(:a)))
        ORDER BY v.updated_at DESC LIMIT 40
    """), {"a": a, "b": b}).mappings()
    seen, out = set(), []
    for r in rows:
        key = r["override_id"] or tuple(sorted((r["src"], r["dst"])))
        if key not in seen:
            out.append(dict(r))
            seen.add(key)
    return out
