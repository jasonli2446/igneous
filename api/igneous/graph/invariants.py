"""Read-only consistency checks for persisted graph edges.

``CORRESPONDS`` is intentionally absent: it is a traversal API projection, not a
persisted edge type.  The checker never writes or repairs data; it reports each
violation with stable, machine-readable fields for CLI and automation use.
"""
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

# Edge types stored in ``edges`` and the node types permitted at each endpoint.
# An unlisted type is invalid, including the derived-only CORRESPONDS projection.
LEGAL_ENDPOINTS: dict[str, set[tuple[str, str]]] = {
    "SENT": {("Person", "Message")},
    "RECEIVED": {("Message", "Person")},
    "MEMBER_OF": {("Person", "Organization")},
    "IN_THREAD": {("Message", "Thread")},
    "REPLIES_TO": {("Message", "Message")},
    "ATTACHED": {("Message", "File")},
    "CONTAINS": {("Folder", "Folder"), ("Folder", "File")},
    "ATTENDED": {("Person", "Event")},
    "AUTHORED": {("Person", "File")},
    "MENTIONS": {("Message", "Person"), ("Message", "Organization")},
    "ABOUT": {("Message", "Topic")},
    "RELATED_TO": set(),  # Any node pair is legal.
    "SAME_AS": {("Person", "Person")},
}


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
        if allowed is not None and (not allowed or endpoints in allowed):
            continue
        expected = "any node types" if allowed == set() else ", ".join(
            f"{src} -> {dst}" for src, dst in sorted(allowed or ())
        )
        violations.append(Violation(
            code="illegal_edge_endpoints",
            message=f"{edge_type} edge has illegal endpoints {endpoints[0]} -> {endpoints[1]}; expected {expected}",
            edge_id=row["id"],
            details={"edge_type": edge_type, "src_type": endpoints[0], "dst_type": endpoints[1]},
        ))
    return violations


def message_cardinality_violations(rows: Iterable[Mapping[str, Any]]) -> list[Violation]:
    """Validate one sender and at least one recipient for each Message."""
    violations = []
    for row in rows:
        sent, received = row["sent_count"], row["received_count"]
        if sent != 1:
            violations.append(Violation(
                code="message_sent_cardinality",
                message=f"Message has {sent} SENT edges; expected exactly 1",
                node_id=row["id"],
                details={"sent_count": sent, "expected": 1},
            ))
        if received < 1:
            violations.append(Violation(
                code="message_received_cardinality",
                message=f"Message has {received} RECEIVED edges; expected at least 1",
                node_id=row["id"],
                details={"received_count": received, "minimum": 1},
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


_ENDPOINT_ROWS = text("""
    SELECT e.id, e.type, src.type AS src_type, dst.type AS dst_type
    FROM edges e
    JOIN nodes src ON src.id = e.src
    JOIN nodes dst ON dst.id = e.dst
    ORDER BY e.id
""")

_MESSAGE_CARDINALITIES = text("""
    SELECT m.id,
           count(DISTINCT s.id) FILTER (WHERE sender.type = 'Person') AS sent_count,
           count(DISTINCT r.id) FILTER (WHERE recipient.type = 'Person') AS received_count
    FROM nodes m
    LEFT JOIN edges s ON s.dst = m.id AND s.type = 'SENT'
    LEFT JOIN nodes sender ON sender.id = s.src
    LEFT JOIN edges r ON r.src = m.id AND r.type = 'RECEIVED'
    LEFT JOIN nodes recipient ON recipient.id = r.dst
    WHERE m.type = 'Message'
    GROUP BY m.id
    HAVING count(DISTINCT s.id) FILTER (WHERE sender.type = 'Person') <> 1
        OR count(DISTINCT r.id) FILTER (WHERE recipient.type = 'Person') < 1
    ORDER BY m.id
""")

_REPLIES_OUTSIDE_THREAD = text("""
    SELECT r.id, r.src, r.dst
    FROM edges r
    WHERE r.type = 'REPLIES_TO'
      AND NOT EXISTS (
          SELECT 1
          FROM edges child_thread
          JOIN edges parent_thread ON parent_thread.dst = child_thread.dst
          WHERE child_thread.src = r.src AND child_thread.type = 'IN_THREAD'
            AND parent_thread.src = r.dst AND parent_thread.type = 'IN_THREAD'
      )
    ORDER BY r.id
""")


def check_graph(s: Session) -> list[Violation]:
    """Return every persisted graph invariant violation without modifying the session."""
    violations = endpoint_violations(s.execute(_ENDPOINT_ROWS).mappings())
    violations.extend(message_cardinality_violations(s.execute(_MESSAGE_CARDINALITIES).mappings()))
    violations.extend(replies_thread_violations(s.execute(_REPLIES_OUTSIDE_THREAD).mappings()))
    return violations
