"""Pair-wide rejections and their exact undo snapshots, in the existing overrides table (#173)."""
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph import overrides, verify


def reject(s: Session, edge_id: int) -> dict:
    root = s.execute(text("""
        SELECT e.id, e.src, e.dst, e.type, e.extractor,
               a.type || ':' || a.key AS a, b.type || ':' || b.key AS b
        FROM edges e JOIN nodes a ON a.id=e.src JOIN nodes b ON b.id=e.dst WHERE e.id=:i
    """), {"i": edge_id}).mappings().first()
    if root is None:
        raise LookupError("No such link")
    if root["extractor"] in verify.STATED:
        raise ValueError("This link is stated in your data or added by you, not inferred")
    # Removing one message from a project still removes its conversation, as the inspector already does.
    siblings = s.execute(text("""
        SELECT DISTINCT n.id AS src, :dst AS dst, x.type, x.extractor, n.type || ':' || n.key AS a, :b AS b
        FROM edges it JOIN edges it2 ON it2.dst=it.dst AND it2.type='IN_THREAD'
        JOIN nodes n ON n.id=it2.src JOIN edges x ON x.src=n.id AND x.dst=:dst AND x.type=:type
        WHERE it.src=:src AND it.type='IN_THREAD' AND x.extractor <> ALL(:stated)
    """), {"src": root["src"], "dst": root["dst"], "type": root["type"], "b": root["b"],
           "stated": list(verify.STATED)}).mappings().all()
    roots = {tuple(r[k] for k in ("a", "b", "type", "extractor")): dict(r) for r in [root, *siblings]}
    pairs = json.dumps([{"a": a, "b": b} for a, b in sorted({tuple(sorted((r["src"], r["dst"])))
                                                             for r in roots.values()})])
    # JSON snapshots retain every edge column, original props and the evidence which ON DELETE CASCADE removes.
    snapshots = s.execute(text("""
        SELECT to_jsonb(e) || jsonb_build_object('src_key', a.type || ':' || a.key,
            'dst_key', b.type || ':' || b.key, 'evidence', COALESCE((
                SELECT jsonb_agg(to_jsonb(ev) ORDER BY ev.id) FROM edge_evidence ev WHERE ev.edge_id=e.id), '[]'))
        FROM jsonb_to_recordset(CAST(:pairs AS jsonb)) AS p(a bigint,b bigint)
        JOIN edges e ON (e.src=p.a AND e.dst=p.b) OR (e.src=p.b AND e.dst=p.a)
        JOIN nodes a ON a.id=e.src JOIN nodes b ON b.id=e.dst
        WHERE e.extractor <> ALL(:stated)
        ORDER BY e.id FOR UPDATE OF e
    """), {"pairs": pairs, "stated": list(verify.STATED)}).scalars().all()
    if not snapshots:
        raise LookupError("No such link")
    oid = overrides.add(s, "unlink", [root["b"], root["a"]])
    verdicts = []
    for a, b, kind, extractor in roots:
        previous = s.execute(text("""
            SELECT to_jsonb(v) FROM edge_verdicts v
            WHERE src_key=:a AND dst_key=:b AND type=:t AND extractor=:x FOR UPDATE
        """), {"a": a, "b": b, "t": kind, "x": extractor}).scalar()
        verdicts.append({"a": a, "b": b, "type": kind, "extractor": extractor, "previous": previous})
        verify.record_correction(s, a, b, kind, extractor, "drop", "you said this connection is wrong", oid)
    data = {"pair_correction": True, "edges": snapshots, "verdicts": verdicts}
    s.execute(text("UPDATE overrides SET data=CAST(:data AS jsonb) WHERE id=:i"),
              {"i": oid, "data": json.dumps(data)})
    verify.apply_verdicts(s)
    ids = [e["id"] for e in snapshots]
    return {"removed": edge_id, "also_removed": len(ids) - 1, "edges": ids, "override_id": oid}


def restore(s: Session, row: dict) -> list[int]:
    """Undo this correction only. A later correction or a replacement live edge always wins."""
    data = row["data"]
    for v in data["verdicts"]:
        params = {"a": v["a"], "b": v["b"], "t": v["type"], "x": v["extractor"],
                  "token": f"override:{row['id']}"}
        removed = s.execute(text("""
            DELETE FROM edge_verdicts WHERE src_key=:a AND dst_key=:b AND type=:t AND extractor=:x
                AND source='user' AND evidence=:token RETURNING src_key
        """), params).first()
        previous = v["previous"]
        if removed is None or previous is None:
            continue
        # Undoing an older action must not resurrect a correction which has itself already been undone.
        if previous["evidence"].startswith("override:"):
            prior_id = int(previous["evidence"].split(":", 1)[1])
            if s.execute(text("SELECT 1 FROM overrides WHERE id=:i"), {"i": prior_id}).first() is None:
                continue
        s.execute(text("""
            INSERT INTO edge_verdicts SELECT * FROM jsonb_populate_record(NULL::edge_verdicts, CAST(:v AS jsonb))
            ON CONFLICT DO NOTHING
        """), {"v": json.dumps(previous)})
    restored = []
    for e in data["edges"]:
        # Another independent correction can still prohibit this pair. Keep its edges archived.
        blocked = s.execute(text("""
            SELECT 1 FROM edge_verdicts WHERE source='user' AND verdict='drop'
                AND ((src_key=:a AND dst_key=:b) OR (src_key=:b AND dst_key=:a)) LIMIT 1
        """), {"a": e["src_key"], "b": e["dst_key"]}).first()
        if blocked:
            continue
        eid = s.execute(text("""
            INSERT INTO edges SELECT e.id,a.id,b.id,e.type,e.props,e.valid_from,e.valid_to,e.extractor,
                e.extractor_version,e.model,e.confidence,e.source_item_id,e.span_start,e.span_end
            FROM jsonb_populate_record(NULL::edges, CAST(:edge AS jsonb)) e
            JOIN nodes a ON a.type=split_part(:a, ':', 1) AND a.key=substr(:a, length(split_part(:a, ':', 1)) + 2)
            JOIN nodes b ON b.type=split_part(:b, ':', 1) AND b.key=substr(:b, length(split_part(:b, ':', 1)) + 2)
            WHERE e.source_item_id IS NULL OR EXISTS (SELECT 1 FROM source_items WHERE id=e.source_item_id)
            ON CONFLICT DO NOTHING RETURNING id
        """), {"edge": json.dumps(e), "a": e["src_key"], "b": e["dst_key"]}).scalar()
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
    s.execute(text("DELETE FROM edges_dropped WHERE id=ANY(:ids)"), {"ids": restored})
    verify.apply_verdicts(s)
    if any(e["type"] == "SAME_AS" for e in data["edges"]):
        from igneous.graph.resolve import refresh_canonical
        refresh_canonical(s, commit=False)
    return list(s.execute(text("SELECT id FROM edges WHERE id=ANY(:ids) ORDER BY id"), {"ids": restored}).scalars())


def for_pair(s: Session, a: list[int], b: list[int]) -> list[dict]:
    """Active user corrections, including legacy ones, for the Why panel (never their undo snapshots)."""
    rows = s.execute(text("""
        SELECT v.reason, v.updated_at, n.id AS src, m.id AS dst, n.name AS src_name, m.name AS dst_name,
               o.id AS override_id
        FROM edge_verdicts v JOIN nodes n ON n.type || ':' || n.key=v.src_key
        JOIN nodes m ON m.type || ':' || m.key=v.dst_key
        LEFT JOIN overrides o ON v.evidence='override:' || o.id
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
