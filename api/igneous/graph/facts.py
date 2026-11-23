"""Facts you or an agent add: a connection between two things, either of which may be something new.

A fact is `subject relation object` ("Alex took part in Board Game Night", "this document is part of Igneous"): each
side is a node already in the graph (by id) or a new project or organization (`New`); the relation is an edge type
these two kinds of thing can have (`RELATIONS`, what the explorer's "+ Connect to…" menu offers); it carries a note
saying why and when it was said; and it says who stated it (`by`): you ('user'), or an agent ('agent:<its name>', an
MCP client when IGNEOUS_MCP_WRITE lets it write, mcp_server.py).

It is stored as every correction is (graph/corrections.py): one intent, an override 'link' keyed by natural keys,
which also defines the new things it names (key 'added:<name>', graph/overrides.py). So it holds through every rebuild
with no mechanism of its own, applies now or with the rebuild that is running (the answer says which), and is undone
by its override id (DELETE /overrides/{id}) or taken out by its link (DELETE /graph/link/{id}), which takes a new thing
only it named with it. It is a statement, not an inference: the verifier never judges it and a pair you reject keeps
it (graph/derived.py `stated`). Nothing is ever written to a source (CO-2).

An agent's fact is shown with its origin and shapes no stage: only a project you put under another is hierarchy the
threads stage builds on, and only you can say two people are one person.
"""
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph import corrections, overrides
from igneous.graph.derived import AGENT, norm

# How two kinds of thing can be connected, in plain words (the add-a-connection menu), and the edge each means. Any
# two things can also be related (RELATED_TO), and two people can be one person (SAME_AS).
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
NEW_TYPES = ("Project", "Organization")  # what a fact can bring into the graph
NAME_MAX, KIND_MAX, NOTE_MAX, AGENT_MAX = 120, 40, 500, 60


@dataclass(frozen=True)
class New:
    """Something the graph doesn't have yet: a project (`kind`, such as hobby, trip, class or job, is its section in
    the explorer) or an organization."""
    type: str
    name: str
    kind: str | None = None

    @property
    def key(self) -> str:
        return f"{self.type}:{overrides.ADDED}{norm(self.name)}"


def options(ta: str | None, tb: str | None) -> list[tuple[str, str, bool]]:
    """The ways a `ta` and a `tb` can be connected, in the menu's order: (label, edge type, from the first to the
    second?)."""
    out = [(label, t, True) for label, t in RELATIONS.get((ta, tb), [])]
    out += [(label, t, False) for label, t in RELATIONS.get((tb, ta), [])]
    if ta == tb == "Person":
        out.append(("same person", "SAME_AS", True))
    return out or [("related", "RELATED_TO", True)]


def allowed(ta: str, tb: str, relation: str) -> bool:
    """Whether `relation` may go from a `ta` to a `tb`: what the menu offers for them (#181), RELATED_TO between any
    two things, SAME_AS between two people."""
    return relation == "RELATED_TO" or (relation == "SAME_AS" and ta == tb == "Person") \
        or relation in {t for _label, t in RELATIONS.get((ta, tb), [])}


def agent(name: str) -> str:
    """The `by` of a fact an agent states: 'agent:<its name>', as the agent gave it, whitespace folded. Raises
    ValueError for a blank one."""
    name = " ".join(str(name).split())[:AGENT_MAX]
    if not name:
        raise ValueError("An agent needs a name")
    return AGENT + name


def _end(s: Session, end: "int | New") -> tuple[str, str, New | None]:
    """A fact's side: (its natural key, its node type, the new thing it is or None)."""
    if isinstance(end, New):
        name = " ".join(end.name.split())[:NAME_MAX]
        if end.type not in NEW_TYPES:
            raise ValueError(f"A fact can add a {' or '.join(NEW_TYPES)}, not a {end.type}")
        if not norm(name):
            raise ValueError("A new thing needs a name")
        kind = (" ".join((end.kind or "").split())[:KIND_MAX] or None) if end.type == "Project" else None
        new = New(end.type, name, kind)
        return new.key, new.type, new
    row = s.execute(text("SELECT type, type || ':' || key AS k FROM nodes WHERE id = :i"), {"i": end}).first()
    if row is None:
        raise LookupError(f"No node {end}")
    return row.k, row.type, None


def add(s: Session, subject: "int | New", relation: str, object_: "int | New", note: str | None = None,
        by: str = "user") -> dict:
    """Record the fact `subject relation object_`, stated by `by`, and apply it now unless a rebuild is running (then
    its final step does). Raises LookupError for the id of nothing and ValueError for a fact that can't be stated: a
    relation these two kinds of thing can't have, a thing connected to itself, a new thing without a name, an agent's
    fact without a note, or an agent saying two people are one. The same fact stated again by the same one is the fact
    already recorded (`already`).

    Returns {id: its link, src, dst: its ends, each once it is in the graph; type, by, override_id: what undoes it,
    nodes: the new things it named, once in the graph; applied: 'now' or 'on_publish' (with a note); already}."""
    if by != "user" and not by.startswith(AGENT):
        raise ValueError("A fact is stated by you ('user') or by an agent ('agent:<name>')")
    note = " ".join((note or "").split())[:NOTE_MAX] or None
    if by != "user" and note is None:
        raise ValueError("An agent's fact needs a note saying why")
    if relation == "SAME_AS" and by != "user":
        raise ValueError("Only you can say two people are the same person")
    with corrections.recording(s):
        (ka, ta, na), (kb, tb, nb) = _end(s, subject), _end(s, object_)
        if ka == kb:
            raise ValueError("A fact connects two different things")
        if not allowed(ta, tb, relation):
            raise ValueError(f"A {ta} can't be connected to a {tb} that way ({relation})")
        if relation == "SAME_AS":
            ka, kb = sorted((ka, kb))
        already = s.execute(text("""
            SELECT id FROM overrides WHERE op = 'link' AND undone_at IS NULL AND node_keys = :k AND value = :t
              AND COALESCE(data->>'by', 'user') = :by ORDER BY id LIMIT 1
        """), {"k": [ka, kb], "t": relation, "by": by}).scalar()
        if already is None:
            props = {"why": note or "you added this", "reason": note or "You added this connection.",
                     "added_at": datetime.now(UTC).isoformat(timespec="seconds")}
            nodes = {n.key: {"type": n.type, "name": n.name, **({"kind": n.kind} if n.kind else {})}
                     for n in (na, nb) if n is not None}
            oid = corrections.link(s, ka, kb, relation, props, by, nodes)
        else:
            oid = already
    applied = corrections.apply_now(s)
    ids = dict(s.execute(text("SELECT type || ':' || key, id FROM nodes WHERE type || ':' || key = ANY(:k)"),
                         {"k": [ka, kb]}).all())
    src, dst = ids.get(ka), ids.get(kb)
    if relation == "SAME_AS" and src and dst:
        src, dst = sorted((src, dst))  # stored lowest id first, as every 'same person' link is
    eid = s.execute(text("SELECT id FROM edges WHERE src = :a AND dst = :b AND type = :t AND extractor = :x"),
                    {"a": src, "b": dst, "t": relation, "x": by}).scalar() if src and dst else None
    s.rollback()
    return {"id": eid, "src": src, "dst": dst, "type": relation, "by": by, "override_id": oid,
            "nodes": [ids[n.key] for n in (na, nb) if n is not None and n.key in ids], "already": already is not None,
            **applied}
