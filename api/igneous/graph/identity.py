"""Who is who: the owner of the graph, and the identity clusters entity resolution builds.

`canonical` maps each Person that resolution merged to its cluster's head (the head maps to itself); a node with no
row is its own head. Only Person nodes are merged. The owner is the Person keyed 'me' (created by the spine and the
self stage), and is always the head of its own cluster.
"""
from sqlalchemy import text
from sqlalchemy.orm import Session

OWNER_KEY = "me"


def self_id(s: Session) -> int | None:
    """The owner's node, or None before any source names one."""
    return s.execute(text("SELECT id FROM nodes WHERE type = 'Person' AND key = :k"), {"k": OWNER_KEY}).scalar()


def head_sql(expr: str) -> str:
    """SQL for the identity head of the node id `expr` (a column or bound parameter)."""
    return f"COALESCE((SELECT canonical_id FROM canonical WHERE node_id = {expr}), {expr})"


def head(s: Session, node_id: int) -> int:
    """The head of `node_id`'s identity cluster (`node_id` itself when it isn't merged)."""
    return s.execute(text(f"SELECT {head_sql(':id')}"), {"id": node_id}).scalar_one()


def members(s: Session, node_id: int) -> list[int]:
    """Every identity in `node_id`'s cluster, `node_id` included ([node_id] when it isn't merged)."""
    ids = s.execute(text(f"SELECT node_id FROM canonical WHERE canonical_id = {head_sql(':id')}"),
                    {"id": node_id}).scalars().all()
    return sorted({node_id, *ids})


def card_keys(meta: dict) -> list[str]:
    """The Person keys a contact card (raw_meta of a gcontacts or maccontacts item) names: its addresses as stored, and
    its phone numbers normalized the way phone handles are keyed, so '(614) 555-0199' finds +16145550199."""
    from igneous.connectors.mac_contacts import norm_phone

    return [*(meta.get("emails") or []), *(norm_phone(p) or p for p in meta.get("phones") or [] if p)]
