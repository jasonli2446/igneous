"""The one way a derived stage writes its edges and Projects, so that your corrections hold while it writes.

A rejection of yours ("This is wrong", or unlinking something from a project) is a 'drop' verdict on a pair of
nodes, keyed by their natural keys (`Type:key`). It holds for the pair, not one rule's spelling of a link: no
derived edge of any type, in either direction, joins them again. It also holds for the pair's identities: a person
is their identity head (another address of theirs is still them), a Project is the Project it was folded into
(a merge you made, or two threads found to be one), and an organization is the one its key stands for now (a domain
its company took over: graph/organizations.py), with the backbone's thread keyed by it (`rekeyed`). `rejections`
reads those pairs, for the writers and for what reads the graph (`Rejections.heads`: the pairs as the explorer shows
identities); `Writer` never writes an edge between one. Every other reader of a correction's keys resolves them the
same way (`merges`, or `rekeyed` for what is the same thing under another key).

A Project is written under one contract by every stage that derives Projects (entities, threads with their
umbrellas, backbone): `Writer.project` upserts it by key with the props the stage derives this run, records the name
it derived as `props.original_name`, and leaves a name you gave it (`props.renamed`) in place. The pipeline's last
step (graph/overrides.py) puts every Project back to its derived state and applies your corrections again.
"""
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

# Extractors that state a fact rather than infer it: read from your data, or added by you. Never rejected, never
# judged by the verifier.
STATED = ("spine", "user", "profile", "contacts", "self", "github", "linkedin")
# A Project's props that the stage deriving it doesn't own: your corrections' (renamed, hidden: the last step sets
# them again), and the backbone's marks on the threads it places (graph/backbone.py).
OTHERS = ("renamed", "hidden", "org", "org_why", "same_as_org")


def norm(name: str | None) -> str:
    """A name for comparing: lowercase words, punctuation as spaces ('Atlas-platform (v2)' -> 'atlas platform v2')."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def follow(folded: dict[str, str], key: str) -> str:
    """Where `key` ends up after every merge in `folded` (fold -> survivor), cycle-safe."""
    seen = {key}
    while key in folded and folded[key] not in seen:
        key = folded[key]
        seen.add(key)
    return key


def rekeyed(s: Session) -> dict[str, str]:
    """What is the same thing under another key now, by natural key: an organization known by a key another
    organization has now (graph/organizations.py records them in `organization_keys`: a domain its company took over,
    your resume's spelling of a company its people name otherwise), and the thread the backbone keys by it
    (`backbone:<org key>`, graph/backbone.py)."""
    out = {}
    for old, new in s.execute(text("SELECT key, org_key FROM organization_keys WHERE key <> org_key ORDER BY key")):
        out[f"Organization:{old}"] = f"Organization:{new}"
        out[f"Project:backbone:{old}"] = f"Project:backbone:{new}"
    return {k: follow(out, k) for k in out}


def merges(s: Session) -> dict[str, str]:
    """Every Project folded into another, by natural key: threads the registry merged (merged_into) and merges you
    made in the inspector, each mapped to the Project it ends up in; and what is the same thing under another key now
    (`rekeyed`)."""
    from igneous.graph import overrides

    moved = rekeyed(s)
    folded = {f"Project:{k}": f"Project:{m}" for k, m in s.execute(text(
        "SELECT key, merged_into FROM thread_registry WHERE status = 'merged' AND merged_into IS NOT NULL"))}
    folded.update(overrides.plan(overrides.load(s), moved).merges)
    folded.update(moved)
    return {k: follow(folded, k) for k in folded}


@dataclass
class Rejections:
    """The pairs you rejected, as identities: the one reading of your rejections that writers and readers share.
    `pairs` holds the canonical natural keys of each pair (a person's identity head, a Project's survivor); `canon` maps
    every live node that is one of those identities to its canonical key, and `ids` each canonical key to its node. A
    pair whose two sides are one identity (two addresses of one person, still joined by a trusted link) holds between
    those exact nodes only (`exact`)."""
    pairs: set[frozenset[str]] = field(default_factory=set)
    canon: dict[int, str] = field(default_factory=dict)
    ids: dict[str, int] = field(default_factory=dict)
    exact: set[frozenset[int]] = field(default_factory=set)
    folded: dict[str, str] = field(default_factory=dict)

    def heads(self) -> set[frozenset[int]]:
        """The rejected pairs as the explorer shows the graph: the node of each identity (a person's head, the Project
        a merge kept)."""
        return {frozenset((self.ids[a], self.ids[b])) for a, b in map(sorted, self.pairs)
                if a in self.ids and b in self.ids}

    def blocks(self, a: int, b: int) -> bool:
        if frozenset((a, b)) in self.exact:
            return True
        ca, cb = self.canon.get(a), self.canon.get(b)
        return ca is not None and cb is not None and ca != cb and frozenset((ca, cb)) in self.pairs

    def track(self, node_id: int, natural: str) -> None:
        """A node created after loading (a Project a stage just wrote): count it as the identity it is."""
        key = follow(self.folded, natural)
        if any(key in pair for pair in self.pairs):
            self.canon[node_id] = key
            if key == natural:
                self.ids[key] = node_id

    def id_pairs(self) -> list[str]:
        """Every blocked (src, dst) node pair, both directions, as 'src>dst' (for SQL: `blocked_sql`)."""
        by_key: dict[str, list[int]] = {}
        for nid, key in self.canon.items():
            by_key.setdefault(key, []).append(nid)
        out = {f"{a}>{b}" for pair in self.exact for a in pair for b in pair if a != b}
        for pair in self.pairs:
            if len(pair) != 2:
                continue
            x, y = sorted(pair)
            for a in by_key.get(x, ()):
                for b in by_key.get(y, ()):
                    out |= {f"{a}>{b}", f"{b}>{a}"}
        return sorted(out)


def blocked_sql(src: str, dst: str) -> str:
    """A SQL condition: the edge from `src` to `dst` (column expressions) joins a rejected pair. Bind :blocked to
    `Rejections.id_pairs()`."""
    return f"(CAST({src} AS text) || '>' || CAST({dst} AS text) = ANY(CAST(:blocked AS text[])))"


def rejections(s: Session) -> Rejections:
    """The pairs you rejected (your 'drop' verdicts), resolved to identities as the graph is now."""
    rows = s.execute(text(
        "SELECT DISTINCT src_key, dst_key FROM edge_verdicts WHERE source = 'user' AND verdict = 'drop'")).all()
    if not rows:
        return Rejections()
    folded = merges(s)
    keys = sorted({k for r in rows for k in r} | {follow(folded, k) for r in rows for k in r})
    # Each key's node, and for a person their identity head.
    live = {r.k: r for r in s.execute(text("""
        SELECT n.type || ':' || n.key AS k, n.id, n.type, h.id AS head, h.type || ':' || h.key AS head_key
        FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id JOIN nodes h ON h.id = COALESCE(c.canonical_id, n.id)
        WHERE n.type || ':' || n.key = ANY(:keys)"""), {"keys": keys})}

    def canonical(key: str) -> str:
        key = follow(folded, key)
        row = live.get(key)
        return row.head_key if row is not None and row.type == "Person" else key

    out = Rejections(folded=folded)
    for a, b in rows:
        ca, cb = canonical(a), canonical(b)
        if ca == cb:
            if a in live and b in live and live[a].id != live[b].id:
                out.exact.add(frozenset((live[a].id, live[b].id)))
            continue
        out.pairs.add(frozenset((ca, cb)))
    wanted = {k for pair in out.pairs for k in pair}
    if not wanted:
        return out
    # Every live node that is one of these identities: a person's addresses, a Project and what was folded into it.
    sources = sorted(wanted | {k for k, survivor in folded.items() if survivor in wanted})
    for nid, natural, type_, head_key in s.execute(text("""
        SELECT n.id, n.type || ':' || n.key, n.type, h.type || ':' || h.key
        FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id JOIN nodes h ON h.id = COALESCE(c.canonical_id, n.id)
        WHERE n.type || ':' || n.key = ANY(:keys)
           OR (n.type = 'Person' AND h.type || ':' || h.key = ANY(:keys))"""), {"keys": sources}):
        key = head_key if type_ == "Person" else follow(folded, natural)
        if key in wanted:
            out.canon[nid] = key
            if key == natural:
                out.ids[key] = nid
    return out


class Writer:
    """A derived stage's edges and Projects. Created when the stage starts (after identities and merges are what
    this run reads), it skips every edge between a pair you rejected and upserts Projects under the correction
    contract."""

    def __init__(self, s: Session, extractor: str, version: str = "1"):
        self.s, self.extractor, self.version = s, extractor, version
        self.rejected = rejections(s)
        self.skipped = 0

    def allows(self, src: int, dst: int) -> bool:
        if self.rejected.blocks(src, dst):
            self.skipped += 1
            return False
        return True

    def refresh(self) -> None:
        """Read the rejections again: this stage folded Projects together, so they are one identity now."""
        self.rejected = rejections(self.s)

    def prune(self) -> int:
        """Delete every derived link that joins a rejected pair (one a merge moved onto the survivor)."""
        blocked = self.rejected.id_pairs()
        if not blocked:
            return 0
        n = self.s.execute(text(f"DELETE FROM edges e WHERE e.extractor <> ALL(:stated) AND {blocked_sql('e.src', 'e.dst')}"),
                           {"stated": list(STATED), "blocked": blocked}).rowcount
        self.skipped += n
        return n

    def edge(self, src: int, dst: int, type_: str, props: dict | None = None, *, confidence: float | None = None,
             source_item_id: int | None = None, evidence: Iterable[tuple[int | None, str, dict]] = (),
             valid_from=None, valid_to=None, extractor: str | None = None) -> int | None:
        """Write one edge with its evidence ((source item, kind, detail) each), or nothing when you rejected the
        pair (returns None). Written again in the same run, it takes the new props and keeps its evidence."""
        if not self.allows(src, dst):
            return None
        eid = self.s.execute(text("""
            INSERT INTO edges (src, dst, type, props, valid_from, valid_to, extractor, extractor_version, confidence,
                               source_item_id)
            VALUES (:src, :dst, :t, CAST(:p AS jsonb), :vf, :vt, :x, :v, :c, :sid)
            ON CONFLICT (src, dst, type, extractor) DO UPDATE SET props = EXCLUDED.props,
                confidence = EXCLUDED.confidence, valid_from = EXCLUDED.valid_from, valid_to = EXCLUDED.valid_to,
                source_item_id = COALESCE(EXCLUDED.source_item_id, edges.source_item_id)
            RETURNING id
        """), {"src": src, "dst": dst, "t": type_, "p": json.dumps(props or {}), "vf": valid_from, "vt": valid_to,
               "x": extractor or self.extractor, "v": self.version, "c": confidence,
               "sid": source_item_id}).scalar_one()
        rows = [{"e": eid, "sid": sid, "k": kind, "d": json.dumps(detail or {})} for sid, kind, detail in evidence]
        if rows:
            self.s.execute(text("""
                INSERT INTO edge_evidence (edge_id, source_item_id, kind, detail)
                SELECT :e, :sid, :k, CAST(:d AS jsonb) WHERE NOT EXISTS (
                    SELECT 1 FROM edge_evidence x WHERE x.edge_id = :e AND x.source_item_id IS NOT DISTINCT FROM :sid
                      AND x.kind = :k)
            """), rows)
        return eid

    def project(self, key: str, name: str, props: dict) -> int:
        """Upsert the Project `key` named `name` with `props` (props.derived says which stage derives it) and the
        derived name as props.original_name, in place of whatever it derived before; a name you gave it stays, and
        so do the props other writers own (OTHERS)."""
        nid = self.s.execute(text("""
            INSERT INTO nodes (type, key, name, props) VALUES ('Project', :k, :n, CAST(:p AS jsonb))
            ON CONFLICT (type, key) DO UPDATE SET
                name = CASE WHEN COALESCE((nodes.props->>'renamed')::bool, false) THEN nodes.name ELSE EXCLUDED.name END,
                props = EXCLUDED.props || COALESCE((SELECT jsonb_object_agg(k, v) FROM jsonb_each(nodes.props) kv(k, v)
                                                    WHERE k = ANY(CAST(:others AS text[]))), '{}'::jsonb)
            RETURNING id
        """), {"k": key, "n": name[:120], "p": json.dumps({**props, "original_name": name[:120]}),
               "others": list(OTHERS)}).scalar_one()
        self.rejected.track(nid, f"Project:{key}")
        return nid
