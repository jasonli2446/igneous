"""HTTP surface for derived entities (Projects) and corrections: the inspector's read model, and every correction.

Reads: GET /graph/entity/{id} (with the corrections that shape it, for undo), GET /graph/suggestions (links the
verifier was unsure about). Corrections: rename, hide, merge and unlink a Project (POST /graph/entity/{id}/...), "This
is wrong" and "This is right" on any derived link (POST /graph/edge/wrong, /graph/edge/confirm), bring removed links
back (POST /graph/edge/restore), "same person" and "not the same person" (POST /graph/person/{id}/merge, /split), a
connection you add or take out again (POST /graph/link, DELETE /graph/link/{id}), and undo (DELETE /overrides/{id}).

Each correction is an intent (graph/corrections.py): recorded at once by natural keys, then applied to the published
graph in place, or by the final step of a rebuild that is running. Every answer says which: {"applied": "now"}, or
{"applied": "on_publish", "note": ...}, and carries the override id that undoes it. Those that remove or bring back
links say, per node pair they touched, whether a link still joins the two (`pairs`: [{a, b, still_linked}]). No
correction is refused because a rebuild is running.
"""
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.db import get_session
from igneous.graph import corrections, overrides
from igneous.graph.corrections import apply_now, recording
from igneous.graph.visibility import group_conversation, not_service

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
    """Other names this entity is known by: its derived name (if renamed) and the names of what was folded into it."""
    out = [props.get("original_name"), *props.get("aliases", [])]
    return sorted({a for a in out if a and a != name})


@router.get("/graph/entity/{node_id}")
def entity(node_id: int = Path(ge=1), items: int = Query(30, ge=1, le=200), s: Session = Depends(get_session)):
    """Everything known about one derived entity: who is in it, where it lives, and the evidence per item."""
    p = _project(s, node_id)
    # A person is their identity head, and only group conversations are groups; no service is a member.
    members = s.execute(text(f"""
        SELECT n.id, n.type, n.name, e.type AS edge, e.props AS eprops, e.confidence,
               COALESCE(si.source, si2.source) AS source,
               COALESCE(si.occurred_at, si2.occurred_at) AS at
        FROM edges e LEFT JOIN canonical c ON c.node_id = e.src JOIN nodes n ON n.id = COALESCE(c.canonical_id, e.src)
        LEFT JOIN source_items si ON si.id = e.source_item_id
        LEFT JOIN source_items si2 ON si2.id = n.source_item_id
        WHERE e.dst = :p AND e.type IN ('PART_OF', 'INVOLVED_IN') AND n.type = ANY(:types) AND {not_service('n')}
          AND (n.type <> 'Thread' OR {group_conversation('n')})
        ORDER BY e.id
    """), {"p": node_id, "types": list(MEMBER_TYPES)}).mappings().all()

    from igneous.graph.traversal import label, names
    named = names(s)
    team: dict[int, dict] = {}
    groups, events, rows = [], [], []
    for m in members:
        w, ep = m["edge"], m["eprops"] or {}
        if m["type"] == "Person" and w == "INVOLVED_IN":
            person = team.setdefault(m["id"], {"id": m["id"], "name": label(m["name"], "Person", named), "items": 0})
            person["items"] += int(ep.get("items", 1))
        elif m["type"] == "Thread":
            groups.append({"id": m["id"], "name": label(m["name"], "Thread", named),
                           "why": ep.get("why"), "days": ep.get("days")})
        elif m["type"] == "Event":
            events.append({"id": m["id"], "name": m["name"], "date": m["at"], "why": ep.get("why")})
        elif m["type"] != "Person":
            rows.append({"id": m["id"], "type": m["type"], "name": m["name"], "why": ep.get("why"),
                         "quote": ep.get("quote"), "source": m["source"], "date": m["at"],
                         "confidence": m["confidence"]})
    people = sorted(team.values(), key=lambda x: -x["items"])
    groups.sort(key=lambda x: -(x["days"] or 0))
    events.sort(key=lambda x: (x["date"] is None, str(x["date"])), reverse=False)
    rows.sort(key=lambda x: str(x["date"] or ""), reverse=True)
    props = p["props"] or {}
    # The corrections that shape it (your rename, your hide, what you merged into it), each undone by its id.
    shaped = [dict(r) for r in s.execute(text("""
        SELECT id AS override_id, op, value, created_at FROM overrides
        WHERE undone_at IS NULL AND op IN ('rename', 'hide', 'merge') AND node_keys[1] = :k ORDER BY id
    """), {"k": f"Project:{p['key']}"}).mappings()]
    return {"id": p["id"], "name": p["name"], "key": p["key"], "aliases": _aliases(props, p["name"]),
            "anchors": props.get("anchors", []), "hidden": bool(props.get("hidden")),
            "people": people, "groups": groups, "events": events, "items": rows[:items],
            "counts": {"people": len(people), "groups": len(groups), "events": len(events), "items": len(rows)},
            "corrections": shaped}


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class MergeBody(BaseModel):
    other: int  # folded into the entity in the URL


class UnlinkBody(BaseModel):
    node: int


def _correct(s: Session, op: str, keys: list[str], value: str | None = None, data: dict | None = None) -> dict:
    with recording(s):
        oid = overrides.add(s, op, keys, value, data)
    return {"override_id": oid, "op": op, **apply_now(s)}


@router.post("/graph/entity/{node_id}/rename")
def rename(node_id: int, body: RenameBody, s: Session = Depends(get_session)):
    p = _project(s, node_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "name must not be blank")
    return {**_correct(s, "rename", [_key(s, node_id)], name), "id": node_id, "was": p["name"]}


@router.post("/graph/entity/{node_id}/hide")
def hide(node_id: int, s: Session = Depends(get_session)):
    _project(s, node_id)
    return {**_correct(s, "hide", [_key(s, node_id)]), "id": node_id}


@router.post("/graph/entity/{node_id}/merge")
def merge(node_id: int, body: MergeBody, s: Session = Depends(get_session)):
    """Fold `other` into this entity: everything linked to it moves here and it never reappears on its own (a thread
    folded into a thread keeps contributing its records to it). Undo brings it back as it was."""
    if body.other == node_id:
        raise HTTPException(422, "Can't merge an entity into itself")
    _project(s, node_id)
    fold = _project(s, body.other)
    snapshot = {"node": {"name": fold["name"], "props": fold["props"] or {}}}
    return {**_correct(s, "merge", [_key(s, node_id), _key(s, body.other)], data=snapshot), "id": node_id}


@router.post("/graph/entity/{node_id}/unlink")
def unlink(node_id: int, body: UnlinkBody, s: Session = Depends(get_session)):
    """Drop one item or person from this entity, and keep it dropped across rebuilds."""
    from igneous.graph.derived import STATED

    _project(s, node_id)
    with recording(s):
        eid = s.execute(text("""
            SELECT id FROM edges WHERE src=:m AND dst=:p AND type IN ('PART_OF','INVOLVED_IN')
                AND extractor <> ALL(:stated) ORDER BY id LIMIT 1
        """), {"m": body.node, "p": node_id, "stated": list(STATED)}).scalar()
        if eid is None:
            raise HTTPException(404, "That isn't linked to this entity")
        result = corrections.reject(s, eid)
    return {**_settled(s, result), "op": "unlink", "id": node_id}


@router.delete("/overrides/{override_id}")
def undo(override_id: int, s: Session = Depends(get_session)):
    """Undo one correction: it is reversed on the graph as it is now (a merged entity comes back with what moved from
    it; the next rebuild recomputes the rest), now or by the running rebuild's final step."""
    with recording(s):
        try:
            row = corrections.undo(s, override_id)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
    return {"undone": override_id, "op": row["op"], **apply_now(s)}


class EdgeBody(BaseModel):
    edge: int


def _settled(s: Session, result: dict) -> dict:
    """Apply a recorded correction now if it can be, and answer with it: when it applies, and per node pair it
    touched whether a link still joins the two."""
    applied = apply_now(s)
    pairs = corrections.outcome(s, result.pop("pairs", []))
    s.rollback()
    return {**result, "pairs": pairs, **applied}


@router.post("/graph/edge/wrong")
def edge_is_wrong(body: EdgeBody, s: Session = Depends(get_session)):
    """'This is wrong' on any derived link: recorded as your verdict on the pair (it stays gone on every rebuild, for
    any of their identities, and teaches the verifier), and the pair's derived links leave the graph. Links read
    straight from your data (who sent an email) can't be wrong this way; only inferred ones can."""
    with recording(s):
        try:
            result = corrections.reject(s, body.edge)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    return _settled(s, result)


@router.post("/graph/edge/confirm")
def edge_is_right(body: EdgeBody, s: Session = Depends(get_session)):
    """'This is right' on a derived link (the answer to a suggestion): your 'keep' verdict, which the verifier never
    overrides and learns from, so the link shows as checked."""
    with recording(s):
        try:
            result = corrections.confirm(s, body.edge)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    return {**result, **apply_now(s)}


@router.get("/graph/suggestions")
def suggestions(limit: int = Query(50, ge=1, le=200), s: Session = Depends(get_session)):
    """Links the verifier was unsure about, for you to answer: 'This is right' (POST /graph/edge/confirm) or 'This is
    wrong' (POST /graph/edge/wrong). Each answer becomes a label the verifier learns from. Most confident first."""
    from igneous.graph.verify import CLAIMS, claim_of

    rows = s.execute(text("""
        SELECT e.id AS edge, e.type, e.extractor, a.id AS src, a.name AS src_name, a.type AS src_type,
               b.id AS dst, b.name AS dst_name, b.type AS dst_type, v.reason, v.confidence
        FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst
        JOIN edge_verdicts v ON v.src_key = a.type || ':' || a.key AND v.dst_key = b.type || ':' || b.key
            AND v.type = e.type AND v.extractor = e.extractor
        WHERE v.verdict = 'unsure' AND v.source <> 'user'
          AND NOT COALESCE((a.props->>'hidden')::bool, false) AND NOT COALESCE((b.props->>'hidden')::bool, false)
        ORDER BY v.confidence DESC NULLS LAST, e.id LIMIT :n
    """), {"n": limit}).mappings().all()
    return {"suggestions": [{**r, "claim": CLAIMS.get(claim_of(r), "")} for r in map(dict, rows)]}


class SamePersonBody(BaseModel):
    other: int


@router.post("/graph/person/{node_id}/merge")
def merge_people(node_id: int, body: SamePersonBody, s: Session = Depends(get_session)):
    """"These are the same person": a trusted identity link (extractor 'user') that entity resolution always
    honors, so the two become one person now and on every rebuild (e.g. a nickname contact and a school address).
    `person`: who they are now (the identity head, once applied)."""
    a, b = sorted((node_id, body.other))
    with recording(s):
        ends = {r.id: r for r in s.execute(text(
            "SELECT id, type, type || ':' || key AS k FROM nodes WHERE id = ANY(:i)"), {"i": [a, b]})}
        if any(ends.get(i) is None or ends[i].type != "Person" for i in (a, b)):
            raise HTTPException(400, "Both must be people")
        if a == b:
            raise HTTPException(400, "That's the same person already")
        oid = corrections.link(s, ends[a].k, ends[b].k, "SAME_AS", {"why": "you said they are the same person"})
    applied = apply_now(s)
    head = s.execute(text("SELECT COALESCE((SELECT canonical_id FROM canonical WHERE node_id = :n), :n)"),
                     {"n": a}).scalar()
    s.rollback()
    return {"merged": [a, b], "person": head, "override_id": oid, **applied}


@router.post("/graph/person/{node_id}/split")
def split_people(node_id: int, body: SamePersonBody, s: Session = Depends(get_session)):
    """"These are not the same person": your verdict, which keeps the two in separate identities on every rebuild
    whatever joins them (a contact card, an address match, a 'same person' you said before, which it takes out).
    Undo (DELETE /overrides/{override_id}) joins them again."""
    with recording(s):
        try:
            result = corrections.split(s, node_id, body.other)
        except LookupError as e:
            raise HTTPException(400, str(e)) from e
    return _settled(s, result)


class RestoreBody(BaseModel):
    edges: list[int]


@router.post("/graph/edge/restore")
def restore_edges(body: RestoreBody, s: Session = Depends(get_session)):
    """Undo "This is wrong": bring the removed links back and forget the verdicts that removed them (a link the
    verifier dropped comes back with your 'keep'). `restored`: those of them in the graph now."""
    with recording(s):
        result = corrections.restore_links(s, body.edges)
    applied = apply_now(s)
    restored = list(s.execute(text("SELECT id FROM edges WHERE id = ANY(:i) ORDER BY id"),
                              {"i": result.pop("edges")}).scalars())
    ids = dict(s.execute(text("SELECT type || ':' || key, id FROM nodes WHERE type || ':' || key = ANY(:k)"),
                         {"k": sorted({k for pair in result["pairs"] for k in pair})}).all())
    pairs = corrections.outcome(s, [(ids[a], ids[b]) for a, b in result.pop("pairs") if a in ids and b in ids])
    s.rollback()
    return {"restored": restored, **result, "pairs": pairs, **applied}


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
    on every rebuild by natural keys (a Project under a Project is stated hierarchy, which the threads stage builds
    on). A 'same person' link merges the two identities. `id`: the link in the graph, once applied."""
    with recording(s):
        ends = {r.id: r for r in s.execute(text(
            "SELECT id, type, type || ':' || key AS k FROM nodes WHERE id = ANY(:i)"), {"i": [body.src, body.dst]})}
        if body.src not in ends or body.dst not in ends:
            raise HTTPException(404, "No such node")
        ta, tb = ends[body.src].type, ends[body.dst].type
        # Only what the menu offers for these two kinds of thing (#181): any other pair would be an illegal edge.
        if not (body.type == "RELATED_TO" or (body.type == "SAME_AS" and ta == tb == "Person")
                or body.type in {t for _l, t in RELATIONS.get((ta, tb), [])}):
            raise HTTPException(400, f"A {ta} can't be connected to a {tb} that way")
        props = {"why": body.note or "you added this", "reason": body.note or "You added this connection."}
        oid = corrections.link(s, ends[body.src].k, ends[body.dst].k, body.type, props)
    applied = apply_now(s)
    a, b = sorted((body.src, body.dst)) if body.type == "SAME_AS" else (body.src, body.dst)
    eid = s.execute(text("SELECT id FROM edges WHERE src = :a AND dst = :b AND type = :t AND extractor = 'user'"),
                    {"a": a, "b": b, "t": body.type}).scalar()
    s.rollback()
    return {"id": eid, "src": body.src, "dst": body.dst, "type": body.type, "override_id": oid, **applied}


@router.delete("/graph/link/{edge_id}")
def remove_link(edge_id: int, s: Session = Depends(get_session)):
    """Take out a link you added (extractor 'user'): it stays out on every rebuild until you add it again, and undo
    (DELETE /overrides/{override_id}) brings it back."""
    with recording(s):
        try:
            result = corrections.remove_link(s, edge_id)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    return _settled(s, result)
