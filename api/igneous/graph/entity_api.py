"""HTTP surface for derived entities (Projects): the inspector's read model and its corrections.

Reads: GET /graph/entity/{id}. Corrections: POST /graph/entity/{id}/rename|hide|merge|unlink, each stored in
`overrides` (graph/overrides.py) and applied immediately; DELETE /overrides/{id} undoes one.
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.db import get_session
from igneous.graph import entities, overrides

router = APIRouter()
MEMBER_TYPES = ("Person", "Thread", "Event", "Message", "File", "Folder")


def _project(s: Session, node_id: int) -> dict:
    n = s.execute(text("SELECT id, type, key, name, props FROM nodes WHERE id = :i"), {"i": node_id}).mappings().first()
    if not n or n["type"] != "Project":
        raise HTTPException(404, "Not a project")
    return dict(n)


def _key(s: Session, node_id: int) -> str:
    row = s.execute(text("SELECT type || ':' || key FROM nodes WHERE id = :i"), {"i": node_id}).scalar()
    if row is None:
        raise HTTPException(404, f"Node {node_id} not found")
    return row


def _aliases(props: dict, name: str) -> list[str]:
    """Other names this entity is known by: its derived name (if renamed) and names folded into it."""
    out = [props.get("original_name"), *props.get("merged_names", [])]
    return sorted({a for a in out if a and a != name})


@router.get("/graph/entity/{node_id}")
def entity(node_id: int, items: int = Query(30, ge=1, le=200), s: Session = Depends(get_session)):
    """Everything known about one derived entity: who is in it, where it lives, and the evidence per item."""
    p = _project(s, node_id)
    members = s.execute(text("""
        SELECT n.id, n.type, n.name, e.type AS edge, e.props AS eprops, e.confidence,
               COALESCE(si.source, si2.source) AS source,
               COALESCE(si.occurred_at, si2.occurred_at) AS at
        FROM edges e JOIN nodes n ON n.id = e.src
        LEFT JOIN source_items si ON si.id = e.source_item_id
        LEFT JOIN source_items si2 ON si2.id = n.source_item_id
        WHERE e.dst = :p AND e.type IN ('PART_OF', 'INVOLVED_IN') AND n.type = ANY(:types)
    """), {"p": node_id, "types": list(MEMBER_TYPES)}).mappings().all()

    from igneous.graph.traversal import handle_names, pretty_thread
    named = handle_names(s)
    people, groups, events, rows = [], [], [], []
    for m in members:
        w, ep = m["edge"], m["eprops"] or {}
        if m["type"] == "Person" and w == "INVOLVED_IN":
            people.append({"id": m["id"], "name": m["name"], "items": int(ep.get("items", 1))})
        elif m["type"] == "Thread":
            groups.append({"id": m["id"], "name": pretty_thread(m["name"], named) or m["name"],
                           "why": ep.get("why"), "days": ep.get("days")})
        elif m["type"] == "Event":
            events.append({"id": m["id"], "name": m["name"], "date": m["at"], "why": ep.get("why")})
        elif m["type"] != "Person":
            rows.append({"id": m["id"], "type": m["type"], "name": m["name"], "why": ep.get("why"),
                         "quote": ep.get("quote"), "source": m["source"], "date": m["at"],
                         "confidence": m["confidence"]})
    people.sort(key=lambda x: -x["items"])
    groups.sort(key=lambda x: -(x["days"] or 0))
    events.sort(key=lambda x: (x["date"] is None, str(x["date"])), reverse=False)
    rows.sort(key=lambda x: str(x["date"] or ""), reverse=True)
    props = p["props"] or {}
    return {"id": p["id"], "name": p["name"], "key": p["key"], "aliases": _aliases(props, p["name"]),
            "anchors": props.get("anchors", []), "hidden": bool(props.get("hidden")),
            "people": people, "groups": groups, "events": events, "items": rows[:items],
            "counts": {"people": len(people), "groups": len(groups), "events": len(events), "items": len(rows)}}


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class MergeBody(BaseModel):
    other: int  # folded into the entity in the URL


class UnlinkBody(BaseModel):
    node: int


def _commit(s: Session, op: str, keys: list[str], value: str | None = None, data: dict | None = None) -> dict:
    oid = overrides.add(s, op, keys, value, data)
    overrides.apply(s)
    s.commit()
    return {"override_id": oid, "op": op}


@router.post("/graph/entity/{node_id}/rename")
def rename(node_id: int, body: RenameBody, s: Session = Depends(get_session)):
    p = _project(s, node_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "name must not be blank")
    return {**_commit(s, "rename", [_key(s, node_id)], name), "id": node_id, "was": p["name"]}


@router.post("/graph/entity/{node_id}/hide")
def hide(node_id: int, s: Session = Depends(get_session)):
    _project(s, node_id)
    return {**_commit(s, "hide", [_key(s, node_id)]), "id": node_id}


@router.post("/graph/entity/{node_id}/merge")
def merge(node_id: int, body: MergeBody, s: Session = Depends(get_session)):
    """Fold `other` into this entity: its members move here and it never reappears on its own."""
    if body.other == node_id:
        raise HTTPException(422, "Can't merge an entity into itself")
    _project(s, node_id)
    _project(s, body.other)
    return {**_commit(s, "merge", [_key(s, node_id), _key(s, body.other)]), "id": node_id}


@router.post("/graph/entity/{node_id}/unlink")
def unlink(node_id: int, body: UnlinkBody, s: Session = Depends(get_session)):
    """Drop one item or person from this entity, and keep it dropped across rebuilds."""
    _project(s, node_id)
    edges = [dict(r) for r in s.execute(text("""
        SELECT src, dst, type, props, extractor_version, confidence, source_item_id, span_start, span_end
        FROM edges WHERE src = :m AND dst = :p AND type IN ('PART_OF', 'INVOLVED_IN')
          AND extractor IN (:x, 'threads', 'hierarchy')
    """), {"m": body.node, "p": node_id, "x": overrides.EXTRACTOR}).mappings()]
    if not edges:
        raise HTTPException(404, "That isn't linked to this entity")
    # Also a labeled verdict: the verifier keeps it dropped on every rebuild and learns from it (few-shot).
    from igneous.graph import verify
    for e in s.execute(text("""
        SELECT e.type, e.extractor FROM edges e WHERE e.src = :m AND e.dst = :p AND e.type IN ('PART_OF', 'INVOLVED_IN')
    """), {"m": body.node, "p": node_id}).all():
        verify.record_correction(s, _key(s, body.node), _key(s, node_id), e.type, e.extractor, "drop",
                                 "removed by you")
    return {**_commit(s, "unlink", [_key(s, node_id), _key(s, body.node)], data={"edges": edges}), "id": node_id}


@router.delete("/overrides/{override_id}")
def undo(override_id: int, s: Session = Depends(get_session)):
    """Undo one correction. Hide / rename / unlink are replayed from the table; a merge needs the folded
    entity back, so the entity stage re-derives it."""
    row = overrides.remove(s, override_id)
    if row is None:
        raise HTTPException(404, "No such correction (already undone?)")
    if row["op"] == "merge":
        entities.run(s, log=None)  # commits; rebuilds the folded entity, then replays the remaining overrides
    else:
        if row["op"] == "unlink":
            overrides.restore_unlink(s, row["data"])
        overrides.apply(s)
        s.commit()
    return {"undone": override_id, "op": row["op"]}


class WrongEdgeBody(BaseModel):
    edge: int


@router.post("/graph/edge/wrong")
def edge_is_wrong(body: WrongEdgeBody, s: Session = Depends(get_session)):
    """'This is wrong' on any derived link: recorded as your verdict (it stays gone on every rebuild and teaches
    the verifier), and the link leaves the graph now. Links read straight from your data (who sent an email) can't
    be wrong this way; only inferred ones can."""
    from igneous.graph import verify
    e = s.execute(text("""
        SELECT e.type, e.extractor, a.type || ':' || a.key AS a, b.type || ':' || b.key AS b
        FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst WHERE e.id = :i"""), {"i": body.edge}).first()
    if e is None:
        raise HTTPException(404, "No such link")
    if e.extractor == "spine":
        raise HTTPException(400, "This link comes straight from your data, not from inference")
    verify.record_correction(s, e.a, e.b, e.type, e.extractor, "drop", "you said this is wrong")
    # A correction applies at the level you made it: "this email isn't part of that project" means its whole
    # conversation isn't, so every other message in the same thread linked the same way goes too.
    siblings = s.execute(text("""
        SELECT m2.type || ':' || m2.key, x.type, x.extractor, x.id
        FROM edges src_e JOIN edges it ON it.src = src_e.src AND it.type = 'IN_THREAD'
        JOIN edges it2 ON it2.dst = it.dst AND it2.type = 'IN_THREAD' JOIN nodes m2 ON m2.id = it2.src
        JOIN edges x ON x.src = m2.id AND x.dst = src_e.dst AND x.type = src_e.type
        WHERE src_e.id = :i AND x.extractor <> 'spine' AND m2.id <> src_e.src"""), {"i": body.edge}).all()
    for key, type_, extractor, _id in siblings:
        verify.record_correction(s, key, e.b, type_, extractor, "drop", "you said this conversation isn't part of it")
    verify.apply_verdicts(s)
    from igneous.graph import dirty
    dirty.touch(s)  # the graph changed: cached views must not serve the old link
    s.commit()
    return {"removed": body.edge, "also_removed": len(siblings), "edges": [body.edge, *(r.id for r in siblings)]}


class SamePersonBody(BaseModel):
    other: int


@router.post("/graph/person/{node_id}/merge")
def merge_people(node_id: int, body: SamePersonBody, s: Session = Depends(get_session)):
    """"These are the same person": a trusted identity link (extractor 'user') that entity resolution always
    honors, so the two become one person now and on every rebuild (e.g. a nickname contact and a school address)."""
    from igneous.graph import resolve
    a, b = sorted((node_id, body.other))
    types = dict(s.execute(text("SELECT id, type FROM nodes WHERE id = ANY(:i)"), {"i": [a, b]}).all())
    if types.get(a) != "Person" or types.get(b) != "Person":
        raise HTTPException(400, "Both must be people")
    if a == b:
        raise HTTPException(400, "That's the same person already")
    s.execute(text("""
        INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
        VALUES (:a, :b, 'SAME_AS', '{"why": "you said they are the same person"}', 'user', '1', 1.0)
        ON CONFLICT (src, dst, type, extractor) DO NOTHING"""), {"a": a, "b": b})
    resolve.refresh_canonical(s)
    from igneous.graph import dirty
    dirty.touch(s)
    s.commit()
    head = s.execute(text("SELECT COALESCE((SELECT canonical_id FROM canonical WHERE node_id = :n), :n)"), {"n": a}).scalar()
    return {"merged": [a, b], "person": head}


class RestoreBody(BaseModel):
    edges: list[int]


@router.post("/graph/edge/restore")
def restore_edges(body: RestoreBody, s: Session = Depends(get_session)):
    """Undo "This is wrong": bring the removed links back and forget the verdicts that removed them."""
    from igneous.graph import dirty
    rows = s.execute(text("""
        SELECT d.id, a.type || ':' || a.key AS a, b.type || ':' || b.key AS b, d.type, d.extractor
        FROM edges_dropped d JOIN nodes a ON a.id = d.src JOIN nodes b ON b.id = d.dst WHERE d.id = ANY(:i)
    """), {"i": body.edges}).all()
    for r in rows:
        s.execute(text("DELETE FROM edge_verdicts WHERE src_key = :a AND dst_key = :b AND type = :t AND extractor = :x"),
                  {"a": r.a, "b": r.b, "t": r.type, "x": r.extractor})
    s.execute(text("""
        INSERT INTO edges SELECT id, src, dst, type, props - 'verdict' - 'reason', valid_from, valid_to, extractor,
               extractor_version, model, confidence, source_item_id, span_start, span_end
        FROM edges_dropped WHERE id = ANY(:i) ON CONFLICT DO NOTHING"""), {"i": [r.id for r in rows]})
    s.execute(text("DELETE FROM edges_dropped WHERE id = ANY(:i)"), {"i": [r.id for r in rows]})
    dirty.touch(s)
    s.commit()
    return {"restored": [r.id for r in rows]}


# How two kinds of thing can be connected, in plain words (the add-a-connection menu), and the edge each means.
RELATIONS = {
    ("Person", "Project"): [("went on it / took part", "INVOLVED_IN"), ("connected to it", "RELATED_TO")],
    ("Project", "Project"): [("part of it", "PART_OF"), ("related to it", "RELATED_TO")],
    ("Project", "Organization"): [("done at / for it", "PART_OF")],
    ("Person", "Organization"): [("worked or studied there", "WORKED_AT"), ("is part of it", "MEMBER_OF")],
    ("Thread", "Project"): [("this chat is about it", "PART_OF")],
    ("Event", "Project"): [("part of it", "PART_OF")],
    ("File", "Project"): [("part of it", "PART_OF")],
    ("Message", "Project"): [("part of it", "PART_OF")],
}


@router.get("/graph/link/options")
def link_options(a: int, b: int, s: Session = Depends(get_session)):
    """The plain-language ways two things can be connected (either direction), for the add-connection menu."""
    ta, tb = (s.execute(text("SELECT type FROM nodes WHERE id = :i"), {"i": i}).scalar() for i in (a, b))
    out = [{"label": lbl, "type": t, "src": a, "dst": b} for lbl, t in RELATIONS.get((ta, tb), [])]
    out += [{"label": lbl, "type": t, "src": b, "dst": a} for lbl, t in RELATIONS.get((tb, ta), [])]
    if ta == tb == "Person":
        out.append({"label": "same person", "type": "SAME_AS", "src": min(a, b), "dst": max(a, b)})
    return {"options": out or [{"label": "related", "type": "RELATED_TO", "src": a, "dst": b}]}


class LinkBody(BaseModel):
    src: int
    dst: int
    type: str
    note: str | None = None


@router.post("/graph/link")
def add_link(body: LinkBody, s: Session = Depends(get_session)):
    """Add a connection you know about: stored as yours (extractor 'user'), never checked or dropped by the AI, kept
    on every rebuild. A 'same person' link merges the two identities."""
    from igneous.graph import dirty, resolve
    ta, tb = (s.execute(text("SELECT type FROM nodes WHERE id = :i"), {"i": i}).scalar() for i in (body.src, body.dst))
    if ta is None or tb is None:
        raise HTTPException(404, "No such node")
    # Only what the menu offers for these two kinds of thing (#181): any other pair would be an illegal edge.
    if not (body.type == "RELATED_TO" or (body.type == "SAME_AS" and ta == tb == "Person")
            or body.type in {t for _l, t in RELATIONS.get((ta, tb), [])}):
        raise HTTPException(400, f"A {ta} can't be connected to a {tb} that way")
    eid = s.execute(text("""
        INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
        VALUES (:a, :b, :t, CAST(:p AS jsonb), 'user', '1', 1.0)
        ON CONFLICT (src, dst, type, extractor) DO UPDATE SET props = EXCLUDED.props RETURNING id
    """), {"a": body.src, "b": body.dst, "t": body.type,
           "p": json.dumps({"why": body.note or "you added this", "reason": body.note or "You added this connection."})}
    ).scalar_one()
    if body.type == "SAME_AS":
        resolve.refresh_canonical(s)
    dirty.touch(s)
    s.commit()
    return {"id": eid, "src": body.src, "dst": body.dst, "type": body.type}
