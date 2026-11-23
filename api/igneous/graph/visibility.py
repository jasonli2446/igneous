"""What the explorer, search and the MCP server may show as a node, and the links you rejected.

A node is hidden when `props.hidden` is true: a non-conversation message or one in a thread nobody answered, a thread
that is not a group conversation (graph/item_class.py), a project you hid or one that duplicates a resume
organization. A Person is a service when `props.kind` is 'service' (an automated sender). Hidden nodes and services
stay evidence and search results, but never appear as nodes, members or connections.

A group conversation is a Thread that is not hidden: the thread flags (graph/item_class.py) show a thread only when it
is a conversation among three or more people.

A pair you rejected ("This is wrong", or taking something out of a project) is your 'drop' verdict on the two nodes,
read as the derived stages read it (graph/derived.py): it holds for both identities, a person under any address and
a Project under what it was folded into. No link the explorer draws joins such a pair: neither a spoke from a centre
nor a tie between two things on screen.
"""
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
    """The pairs you rejected, as the explorer shows the graph: the node of each identity (a person's identity head,
    the Project a merge kept). The same reading the derived stages write by, so what they never link the explorer
    never draws."""
    from igneous.graph.derived import rejections

    return rejections(s).heads()
