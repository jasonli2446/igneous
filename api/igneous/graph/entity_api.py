"""HTTP surface for derived entities (Projects) and corrections: the inspector's read model, and every correction.

Reads: GET /graph/entity/{id} (with the corrections that shape it, for undo), GET /graph/suggestions (links the
verifier was unsure about). Corrections: rename, hide, merge and unlink a Project (POST /graph/entity/{id}/...), "This
is wrong" and "This is right" on any derived link (POST /graph/edge/wrong, /graph/edge/confirm), bring removed links
back (POST /graph/edge/restore), "same person" and "not the same person" (POST /graph/person/{id}/merge, /split), a
fact you or an agent add (POST /facts, graph/facts.py: a connection, to something new if need be) and a link someone
added taken out again (DELETE /graph/link/{id}), and undo (DELETE /overrides/{id}).

Each correction is an intent (graph/corrections.py): recorded at once by natural keys, then applied to the published
graph in place, or by the final step of a rebuild that is running. Every answer says which: {"applied": "now"}, or
{"applied": "on_publish", "note": ...}, and carries the override id that undoes it. Those that remove or bring back
links say, per node pair they touched, whether a link still joins the two (`pairs`: [{a, b, still_linked}]). No
correction is refused because a rebuild is running.
"""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.db import get_session
from igneous.graph import corrections, facts, overrides
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
            "added_by": props.get("added_by"),  # a project a fact brought in: 'user' or 'agent:<name>'
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
    from igneous.graph.derived import inferred

    _project(s, node_id)
    with recording(s):
        eid = s.execute(text(f"""
            SELECT id FROM edges WHERE src=:m AND dst=:p AND type IN ('PART_OF','INVOLVED_IN')
                AND {inferred('extractor')} ORDER BY id LIMIT 1
        """), {"m": body.node, "p": node_id}).scalar()
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
    """"These are the same person": your fact (graph/facts.py), a trusted identity link (extractor 'user') that entity
    resolution always honors, so the two become one person now and on every rebuild (e.g. a nickname contact and a
    school address). `person`: who they are now (the identity head, once applied)."""
    a, b = sorted((node_id, body.other))
    types = dict(s.execute(text("SELECT id, type FROM nodes WHERE id = ANY(:i)"), {"i": [a, b]}).all())
    if any(types.get(i) != "Person" for i in (a, b)):
        raise HTTPException(400, "Both must be people")
    if a == b:
        raise HTTPException(400, "That's the same person already")
    added = facts.add(s, a, "SAME_AS", b, note="you said they are the same person")
    head = s.execute(text("SELECT COALESCE((SELECT canonical_id FROM canonical WHERE node_id = :n), :n)"),
                     {"n": a}).scalar()
    s.rollback()
    return {"merged": [a, b], "person": head, "override_id": added["override_id"],
            **{k: added[k] for k in ("applied", "note") if k in added}}


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


@router.get("/graph/link/options")
def link_options(a: int, b: int, s: Session = Depends(get_session)):
    """The plain-language ways two things can be connected (either direction), for the add-connection menu: each a
    `relation` POST /facts takes from `src` to `dst`."""
    ta, tb = (s.execute(text("SELECT type FROM nodes WHERE id = :i"), {"i": i}).scalar() for i in (a, b))
    return {"options": [{"label": label, "type": t, "src": min(a, b) if t == "SAME_AS" else a if forward else b,
                         "dst": max(a, b) if t == "SAME_AS" else b if forward else a}
                        for label, t, forward in facts.options(ta, tb)]}


class NewThing(BaseModel):
    """Something the graph doesn't have yet, which the fact brings in."""
    name: str = Field(min_length=1, max_length=facts.NAME_MAX)
    type: Literal["Project", "Organization"] = "Project"
    kind: str | None = Field(None, max_length=facts.KIND_MAX)  # a project's: hobby, trip, class, job, project...


class FactBody(BaseModel):
    subject: Annotated[int, Field(ge=1)] | NewThing
    relation: str = Field(min_length=1, max_length=40)  # an edge type GET /graph/link/options offers for the two
    object: Annotated[int, Field(ge=1)] | NewThing
    note: str | None = Field(None, max_length=facts.NOTE_MAX)
    agent: str | None = Field(None, min_length=1, max_length=facts.AGENT_MAX)  # stated by this agent, not by you


@router.post("/facts")
def add_fact(body: FactBody, s: Session = Depends(get_session)):
    """Add a fact: `subject relation object`, where each side is a node id or something new (a project or an
    organization), and the relation one GET /graph/link/options offers for the two kinds of thing (RELATED_TO for any
    two, SAME_AS for two people). Stated by you, or by `agent` (extractor 'agent:<agent>'); `note` says why. Kept on
    every rebuild, never checked or dropped by the AI, and never written to a source. 404 for the id of nothing, 400
    for a fact that can't be stated.

    Answers like every correction (`applied`: now, or on_publish with a note) with `override_id`, which undoes it
    (DELETE /overrides/{override_id}); `id`, `src` and `dst`: its link and ends in the graph, once applied; `nodes`:
    the new things it brought in; `already`: the same fact was stated before, and that one stands."""
    def end(x: int | NewThing) -> int | facts.New:
        return x if isinstance(x, int) else facts.New(x.type, x.name, x.kind)

    try:
        return facts.add(s, end(body.subject), body.relation, end(body.object), body.note,
                         facts.agent(body.agent) if body.agent is not None else "user")
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/graph/link/{edge_id}")
def remove_link(edge_id: int, s: Session = Depends(get_session)):
    """Take out a link you or an agent added (extractor 'user' or 'agent:<name>'), and a new thing only it named: it
    stays out on every rebuild until it is added again, and undo (DELETE /overrides/{override_id}) brings it back."""
    with recording(s):
        try:
            result = corrections.remove_link(s, edge_id)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    return _settled(s, result)
