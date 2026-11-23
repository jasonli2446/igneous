"""What the explorer, search and the MCP server may show as a node, and the links you rejected.

A node is hidden when `props.hidden` is true: a non-conversation message or one in a thread nobody answered, a thread
that is not a group conversation (graph/item_class.py), a project you hid or one that duplicates a resume
organization. A Person is a service when `props.kind` is 'service' (an automated sender). Hidden nodes and services
stay evidence and search results, but never appear as nodes, members or connections.

A group conversation is a Thread that is not hidden: the thread flags (graph/item_class.py) show a thread only when it
is a conversation among three or more people.

A pair you rejected ("This is wrong", or taking something out of a project) is your 'drop' verdict on the two nodes.
No link the explorer draws joins such a pair: neither a spoke from a centre nor a tie between two things on screen.
"""
from sqlalchemy import text
from sqlalchemy.orm import Session


def visible(alias: str) -> str:
    """`alias` (a nodes row) is not hidden."""
    return f"({alias}.props->>'hidden') IS DISTINCT FROM 'true'"


def not_service(alias: str) -> str:
    """`alias` (a nodes row) is not an automated sender."""
    return f"({alias}.props->>'kind') IS DISTINCT FROM 'service'"


def not_owner(alias: str) -> str:
    """`alias` (a nodes row) is not the owner of the graph."""
    return f"({alias}.props->>'self') IS DISTINCT FROM 'true'"


def shown(alias: str) -> str:
    """`alias` (a nodes row) may appear as a node: neither hidden nor a service."""
    return f"({visible(alias)} AND {not_service(alias)})"


def group_conversation(alias: str) -> str:
    """`alias` (a nodes row) is a group conversation."""
    return f"({alias}.type = 'Thread' AND {visible(alias)})"


def rejected(s: Session) -> set[frozenset[int]]:
    """The pairs you rejected, as the graph is now: {a, b} for each of your 'drop' verdicts, a person by their
    identity head. A pair whose two sides are now one identity is left out."""
    rows = s.execute(text("""
        SELECT DISTINCT COALESCE(ca.canonical_id, a.id), COALESCE(cb.canonical_id, b.id)
        FROM edge_verdicts v
        JOIN nodes a ON a.type = split_part(v.src_key, ':', 1) AND a.key = substr(v.src_key, strpos(v.src_key, ':') + 1)
        JOIN nodes b ON b.type = split_part(v.dst_key, ':', 1) AND b.key = substr(v.dst_key, strpos(v.dst_key, ':') + 1)
        LEFT JOIN canonical ca ON ca.node_id = a.id LEFT JOIN canonical cb ON cb.node_id = b.id
        WHERE v.source = 'user' AND v.verdict = 'drop'
    """)).all()
    return {frozenset(pair) for pair in rows if pair[0] != pair[1]}
