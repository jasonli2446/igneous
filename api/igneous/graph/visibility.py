"""SQL predicates for what the explorer, search and the MCP server may show as a node.

A node is hidden when `props.hidden` is true: a non-conversation message or one in a thread nobody answered, a thread
that is not a group conversation (graph/item_class.py), a project you hid or one that duplicates a resume
organization. A Person is a service when `props.kind` is 'service' (an automated sender). Hidden nodes and services
stay evidence and search results, but never appear as nodes, members or connections.
"""


def visible(alias: str) -> str:
    """`alias` (a nodes row) is not hidden."""
    return f"({alias}.props->>'hidden') IS DISTINCT FROM 'true'"


def not_service(alias: str) -> str:
    """`alias` (a nodes row) is not an automated sender."""
    return f"({alias}.props->>'kind') IS DISTINCT FROM 'service'"


def not_owner(alias: str) -> str:
    """`alias` (a nodes row) is not the owner of the graph."""
    return f"({alias}.props->>'self') IS DISTINCT FROM 'true'"
