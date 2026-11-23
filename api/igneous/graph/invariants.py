"""Read-only consistency checks for the persisted graph (`igneous check-graph`).

Edge types and their endpoints, each message's senders and recipients against its own record's headers, replies
within one thread, SAME_AS well-formed and `canonical` equal to its clusters, every message built from a record and
in a thread, the spine's provenance (every edge it builds from records says which records), and every stored source
value known. `CORRESPONDS` is absent on purpose: traversal computes it, nothing stores it. The checker never writes
or repairs data; it reports each violation with machine-readable fields for the CLI and automation.
"""
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.connectors.mac_contacts import norm_phone
from igneous.graph.identity import OWNER_KEY, self_id
from igneous.source_catalog import BY_VALUE

# Edge types stored in ``edges`` and the node types permitted at each endpoint.
# An unlisted type is invalid, including the derived-only CORRESPONDS projection.
LEGAL_ENDPOINTS: dict[str, set[tuple[str, str]]] = {
    "SENT": {("Person", "Message")},
    "RECEIVED": {("Message", "Person")},
    "MEMBER_OF": {("Person", "Organization")},  # a LinkedIn connection's company; also stated by you
    "WORKED_AT": {("Person", "Organization")},
    "IN_THREAD": {("Message", "Thread")},
    "REPLIES_TO": {("Message", "Message")},
    # A folder's files; a repo's AI sessions (Thread) and GitHub activity (Message).
    "CONTAINS": {("Folder", "Folder"), ("Folder", "File"), ("Folder", "Thread"), ("Folder", "Message")},
    "ATTENDED": {("Person", "Event")},
    "MENTIONS": {("Message", "Person"), ("Message", "Organization")},
    "ABOUT": {("Message", "Topic")},
    "RELATED_TO": set(),  # Any node pair is legal.
    "SAME_AS": {("Person", "Person")},
    # Derived entities (graph/entities.py): an item or anchor belongs to a Project; people are involved in it.
    "PART_OF": {(t, "Project") for t in ("Message", "Event", "Thread", "File", "Folder", "Project")}
               | {("Organization", "Organization"),  # a lab inside its university (your resume)
                  ("Project", "Organization")},     # done at / for it (a connection you add)
    "INVOLVED_IN": {("Person", "Project")},
    # Google Takeout Maps (connectors/takeout.py): you visited / saved / reviewed a place.
    "VISITED": {("Person", "Place")},
    "SAVED": {("Person", "Place")},
    # Relationship history (graph/history.py): claims a message makes, and who introduced whom.
    "HAS_COMMITMENT": {("Message", "Commitment")},
    "HAS_DECISION": {("Message", "Decision")},
    "INTRODUCED": {("Person", "Person")},
}

# Edges the spine builds from records: each lists its supporting records in spine_edge_support (graph/spine.py).
RECORD_EXTRACTORS = ("spine", "contacts", "github", "linkedin")


@dataclass(frozen=True)
class Violation:
    """One graph invariant failure, suitable for human and JSON output."""

    code: str
    message: str
    edge_id: int | None = None
    node_id: int | None = None
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def endpoint_violations(rows: Iterable[Mapping[str, Any]]) -> list[Violation]:
    """Validate edge endpoint types from ``id/type/src_type/dst_type`` rows."""
    violations = []
    for row in rows:
        edge_type = row["type"]
        endpoints = (row["src_type"], row["dst_type"])
        allowed = LEGAL_ENDPOINTS.get(edge_type)
        if allowed is None:
            violations.append(Violation(
                code="unknown_edge_type",
                message=f"{edge_type} is not a persisted edge type",
                edge_id=row["id"],
                details={"edge_type": edge_type, "src_type": endpoints[0], "dst_type": endpoints[1]},
            ))
            continue
        if not allowed or endpoints in allowed:
            continue
        expected = ", ".join(f"{src} -> {dst}" for src, dst in sorted(allowed))
        violations.append(Violation(
            code="illegal_edge_endpoints",
            message=f"{edge_type} edge has illegal endpoints {endpoints[0]} -> {endpoints[1]}; expected {expected}",
            edge_id=row["id"],
            details={"edge_type": edge_type, "src_type": endpoints[0], "dst_type": endpoints[1]},
        ))
    return violations


def expected_participants(source: str, meta: Mapping[str, Any]) -> tuple[set[str], set[str]] | None:
    """The Person keys a message record's own headers say sent it and received it, keyed as the spine keys them, or
    None for a source without that shape (GitHub activity: its authors vary and nobody receives it)."""
    if source in ("email", "gmail"):
        return ({a["email"] for a in (meta.get("from") or [])[:1]},
                {a["email"] for f in ("to", "cc", "bcc") for a in meta.get(f) or []})
    if source == "imessage":
        handles = {h.lower(): h for h in meta.get("participants") or []}
        keys = {h: (k if "@" in k else norm_phone(k) or k) for k, h in handles.items()}
        senders = set(meta.get("senders") or [])
        return ({keys[h] for h in keys if h in senders} | ({OWNER_KEY} if "me" in senders else set()),
                set(keys.values()) | {OWNER_KEY})
    if source == "takeout_chat":
        people = set(meta.get("participants") or [])
        return people & set(meta.get("senders") or []), people
    if source == "ai_chats":
        return {OWNER_KEY}, set()
    return None


def message_cardinality_violations(rows: Iterable[Mapping[str, Any]]) -> list[Violation]:
    """Each built Message's senders and recipients against what its records' headers name (rows: id, source, metas,
    sent, received: the Person keys of its SENT and RECEIVED edges). Mail without a To or Cc has no recipient."""
    violations = []
    for row in rows:
        expected_sent, expected_received = set(), set()
        known = False
        for meta in row["metas"] or []:  # a message built from no record is reported as a phantom
            parts = expected_participants(row["source"], meta or {})
            if parts is not None:
                known = True
                expected_sent |= parts[0]
                expected_received |= parts[1]
        if not known:
            continue
        sent, received = set(row["sent"] or []), set(row["received"] or [])
        if sent != expected_sent:
            violations.append(Violation(
                code="message_sent_cardinality",
                message=f"Message has {len(sent)} senders; its records name {len(expected_sent)}",
                node_id=row["id"],
                details={"missing": sorted(expected_sent - sent), "unexpected": sorted(sent - expected_sent)},
            ))
        if received != expected_received:
            violations.append(Violation(
                code="message_received_cardinality",
                message=f"Message has {len(received)} recipients; its records name {len(expected_received)}",
                node_id=row["id"],
                details={"missing": sorted(expected_received - received),
                         "unexpected": sorted(received - expected_received)},
            ))
    return violations


def replies_thread_violations(rows: Iterable[Mapping[str, Any]]) -> list[Violation]:
    """Report REPLIES_TO edges whose messages have no thread in common."""
    return [
        Violation(
            code="reply_crosses_threads",
            message="REPLIES_TO edge does not connect messages in a shared Thread",
            edge_id=row["id"],
            details={"src": row["src"], "dst": row["dst"]},
        )
        for row in rows
    ]


def same_as_violations(rows: Iterable[Mapping[str, Any]]) -> list[Violation]:
    """SAME_AS is one undirected edge per pair, stored src < dst, between two different nodes."""
    return [Violation(code="same_as_malformed",
                      message="SAME_AS joins a node to itself" if row["src"] == row["dst"]
                      else "SAME_AS is stored with src > dst",
                      edge_id=row["id"], details={"src": row["src"], "dst": row["dst"]})
            for row in rows if row["src"] >= row["dst"]]


def canonical_violations(links: Iterable[Mapping[str, Any]], canonical: Mapping[int, int],
                         firsts: Mapping[int, str], me: int | None = None,
                         rejected: Iterable[frozenset[int]] = (),
                         yours: Iterable[frozenset[int]] = ()) -> list[Violation]:
    """`canonical` holds the identity clusters SAME_AS forms (graph/resolve.py refresh_canonical). Rows: every SAME_AS
    link between two Person nodes (id, src, dst, extractor) and every canonical row (node -> head; a node in no
    cluster has none); `firsts`: each Person's own first name ('' when unknown, and for you); `me`: your node;
    `rejected`: pairs of nodes said not to be one person, and `yours`: those of them you said. Every cluster is
    connected by the links within it and has a head that maps to itself. No link spans two clusters except where
    resolution refuses it: any link, trusted (resolve.TRUSTED) or not, when the two clusters hold a pair you said is
    not one person; and a scored one also when they carry first names that can't be one person's, one of them is
    yours, or they hold a pair said not to be one person."""
    from igneous.graph.resolve import TRUSTED, same_first

    out = []
    for head in sorted(set(canonical.values())):
        if canonical.get(head) != head:
            out.append(Violation(code="canonical_head", message="a cluster head does not map to itself", node_id=head))
    members: dict[int, set[int]] = defaultdict(set)
    for node, head in canonical.items():
        members[head].add(node)
    parent = {n: n for n in canonical}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    between = []
    for link in links:
        a, b = link["src"], link["dst"]
        if a != b and canonical.get(a, a) == canonical.get(b, b):  # both in one cluster
            parent[find(a)] = find(b)
        else:
            between.append(link)
    for head, nodes in sorted(members.items()):
        if len({find(n) for n in nodes}) > 1:
            out.append(Violation(code="canonical_disconnected", node_id=head,
                                 message="a canonical cluster is not connected by SAME_AS links within it",
                                 details={"members": sorted(nodes)}))
    never = {frozenset(p) for p in rejected}

    def cluster(n: int) -> set[int]:
        return members.get(canonical.get(n, n)) or {n}

    def named(c: set[int]) -> set[str]:
        return {f for f in (firsts.get(n, "") for n in c) if len(f) > 1}  # an initial says nothing

    apart = {frozenset(p) for p in yours}
    for link in between:
        ca, cb = cluster(link["src"]), cluster(link["dst"])
        refused = any(frozenset((x, y)) in apart for x in ca for y in cb) or (link["extractor"] not in TRUSTED and (
            any(not same_first(x, y) for x in named(ca) for y in named(cb))
            or me in ca or me in cb
            or any(frozenset((x, y)) in never for x in ca for y in cb)))
        if not refused:
            out.append(Violation(code="canonical_split", edge_id=link["id"],
                                 message="SAME_AS links two identities that canonical keeps apart",
                                 details={"src": link["src"], "dst": link["dst"], "extractor": link["extractor"]}))
    return sorted(out, key=lambda v: (v.code, v.node_id or 0, v.edge_id or 0))


_ENDPOINT_ROWS = text("""
    SELECT e.id, e.type, src.type AS src_type, dst.type AS dst_type
    FROM edges e
    JOIN nodes src ON src.id = e.src
    JOIN nodes dst ON dst.id = e.dst
    ORDER BY e.id
""")

_MESSAGE_PARTICIPANTS = text("""
    SELECT m.id, m.props->>'source' AS source,
           (SELECT array_agg(si.raw_meta) FROM spine_node_support ns JOIN source_items si ON si.id = ns.source_item_id
            WHERE ns.node_id = m.id AND ns.represents) AS metas,
           (SELECT array_agg(p.key) FROM edges e JOIN nodes p ON p.id = e.src
            WHERE e.dst = m.id AND e.type = 'SENT' AND p.type = 'Person') AS sent,
           (SELECT array_agg(p.key) FROM edges e JOIN nodes p ON p.id = e.dst
            WHERE e.src = m.id AND e.type = 'RECEIVED' AND p.type = 'Person') AS received
    FROM nodes m
    WHERE m.type = 'Message'
    ORDER BY m.id
""")

_REPLIES_OUTSIDE_THREAD = text("""
    SELECT r.id, r.src, r.dst
    FROM edges r
    WHERE r.type = 'REPLIES_TO' AND r.extractor <> 'spine-quoted'
      AND NOT EXISTS (
          SELECT 1
          FROM edges child_thread
          JOIN edges parent_thread ON parent_thread.dst = child_thread.dst
          WHERE child_thread.src = r.src AND child_thread.type = 'IN_THREAD'
            AND parent_thread.src = r.dst AND parent_thread.type = 'IN_THREAD'
      )
    ORDER BY r.id
""")

_SAME_AS = text("""
    SELECT e.id, e.src, e.dst, e.extractor FROM edges e
    JOIN nodes a ON a.id = e.src AND a.type = 'Person' JOIN nodes b ON b.id = e.dst AND b.type = 'Person'
    WHERE e.type = 'SAME_AS' ORDER BY e.id
""")

_PHANTOM_MESSAGES = text("""
    SELECT m.id, m.key FROM nodes m
    WHERE m.type = 'Message' AND NOT EXISTS (SELECT 1 FROM spine_node_support ns WHERE ns.node_id = m.id AND ns.represents)
    ORDER BY m.id
""")

_THREADLESS_MESSAGES = text("""
    SELECT m.id FROM nodes m
    WHERE m.type = 'Message' AND NOT EXISTS (SELECT 1 FROM edges it WHERE it.src = m.id AND it.type = 'IN_THREAD')
    ORDER BY m.id
""")

_UNSUPPORTED_EDGES = text("""
    SELECT e.id, e.type, e.extractor FROM edges e
    WHERE e.extractor = ANY(:ex) AND NOT EXISTS (SELECT 1 FROM spine_edge_support x WHERE x.edge_id = e.id)
    ORDER BY e.id
""")

_FOREIGN_SUPPORT = text("""
    SELECT x.edge_id, e.extractor FROM spine_edge_support x JOIN edges e ON e.id = x.edge_id
    WHERE e.extractor <> ALL(:ex) GROUP BY x.edge_id, e.extractor ORDER BY x.edge_id
""")


def check_graph(s: Session) -> list[Violation]:
    """Return every persisted graph invariant violation without modifying the session."""
    violations = endpoint_violations(s.execute(_ENDPOINT_ROWS).mappings())
    violations.extend(message_cardinality_violations(s.execute(_MESSAGE_PARTICIPANTS).mappings()))
    violations.extend(replies_thread_violations(s.execute(_REPLIES_OUTSIDE_THREAD).mappings()))
    links = s.execute(_SAME_AS).mappings().all()
    violations.extend(same_as_violations(links))
    from igneous.graph import resolve

    canonical = dict(s.execute(text("""
        SELECT c.node_id, c.canonical_id FROM canonical c JOIN nodes n ON n.id = c.node_id WHERE n.type = 'Person'
    """)).all())
    me = self_id(s)
    ids = sorted({*canonical, *(link["src"] for link in links), *(link["dst"] for link in links)})
    firsts = {r.id: "" if r.id == me else resolve.split_name(resolve.own_name(r.name, r.props), "")[0]
              for r in s.execute(text("SELECT id, name, props FROM nodes WHERE id = ANY(:ids)"), {"ids": ids})}
    violations.extend(canonical_violations(links, canonical, firsts, me, resolve.rejected(s),
                                           resolve.rejected(s, yours=True)))
    violations.extend(Violation(code="phantom_message", node_id=r.id, details={"key": r.key},
                                message="Message is built from no record")
                      for r in s.execute(_PHANTOM_MESSAGES))
    violations.extend(Violation(code="message_without_thread", node_id=r.id, message="Message is in no Thread")
                      for r in s.execute(_THREADLESS_MESSAGES))
    violations.extend(Violation(code="spine_edge_unsupported", edge_id=r.id,
                                message=f"{r.type} edge ({r.extractor}) lists no record it was built from",
                                details={"edge_type": r.type, "extractor": r.extractor})
                      for r in s.execute(_UNSUPPORTED_EDGES, {"ex": list(RECORD_EXTRACTORS)}))
    violations.extend(Violation(code="spine_support_foreign", edge_id=r.edge_id,
                                message=f"an edge another stage owns ({r.extractor}) has spine support rows",
                                details={"extractor": r.extractor})
                      for r in s.execute(_FOREIGN_SUPPORT, {"ex": list(RECORD_EXTRACTORS)}))
    for (source,) in s.execute(text("SELECT DISTINCT source FROM source_items ORDER BY 1")):
        if source not in BY_VALUE:
            violations.append(Violation(code="unknown_source", message=f"source_items.source {source!r} is not in "
                                        "source_catalog", details={"source": source}))
    from igneous.graph import derived_invariants  # corrections and the thread registry hold
    violations.extend(derived_invariants.check(s))
    return violations
