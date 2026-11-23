"""Who is who: the owner of the graph, and the identity clusters entity resolution builds.

`canonical` maps each Person that resolution merged to its cluster's head (the head maps to itself); a node with no
row is its own head. Only Person nodes are merged. The owner is the Person keyed 'me' (created by the spine and the
self stage), and is always the head of its own cluster.

Every reader follows one rule: a person is their identity head. Every Person id the API returns is a head, and the
records of every identity in a cluster count for its head (`members`, `members_sql`).
"""
from sqlalchemy import text
from sqlalchemy.orm import Session

OWNER_KEY = "me"
OWNER_NAME = "Me"  # the owner's name until a source names you
# SAME_AS links that are ground truth, which entity resolution never second-guesses: your identity signals, contact
# cards, your corrections, a commit's verified email, a LinkedIn connection's address.
TRUSTED = ("self", "contacts", "user", "github", "linkedin")


def self_id(s: Session) -> int | None:
    """The owner's node, or None before any source names one."""
    return s.execute(text("SELECT id FROM nodes WHERE type = 'Person' AND key = :k"), {"k": OWNER_KEY}).scalar()


def head_sql(expr: str) -> str:
    """SQL for the identity head of the node id `expr` (a column or bound parameter)."""
    return f"COALESCE((SELECT canonical_id FROM canonical WHERE node_id = {expr}), {expr})"


def head(s: Session, node_id: int) -> int:
    """The head of `node_id`'s identity cluster (`node_id` itself when it isn't merged)."""
    return s.execute(text(f"SELECT {head_sql(':id')}"), {"id": node_id}).scalar_one()


class NotFound(LookupError):
    """No node has this id."""

    def __init__(self, node_id: int):
        super().__init__(f"No node {node_id}")
        self.node_id = node_id


def node(s: Session, node_id: int) -> dict:
    """The node `node_id` names, as readers show it: for a person, their identity head (another address of theirs is
    them). {id, type, key, name, props, source_item_id}. Raises NotFound."""
    row = s.execute(text(f"""SELECT id, type, key, name, props, source_item_id FROM nodes
                             WHERE id = {head_sql(':id')}"""), {"id": node_id}).mappings().first()
    if row is None:
        raise NotFound(node_id)
    return dict(row)


def heads(s: Session, node_ids) -> dict[int, int]:
    """{node: its identity head} for each of `node_ids`."""
    ids = sorted({int(i) for i in node_ids})
    if not ids:
        return {}
    return dict(s.execute(text("""
        SELECT x, COALESCE(c.canonical_id, x) FROM unnest(CAST(:ids AS bigint[])) AS x
        LEFT JOIN canonical c ON c.node_id = x"""), {"ids": ids}).all())


def members_sql(expr: str) -> str:
    """SQL for the set of every identity in the cluster of the node id `expr` (a bound parameter or a column), the
    node itself included, for `IN`."""
    h = head_sql(expr)
    return f"(SELECT {h} UNION SELECT node_id FROM canonical WHERE canonical_id = {h})"


def members(s: Session, node_id: int) -> list[int]:
    """Every identity in `node_id`'s cluster, `node_id` included ([node_id] when it isn't merged)."""
    ids = s.execute(text(f"SELECT node_id FROM canonical WHERE canonical_id = {head_sql(':id')}"),
                    {"id": node_id}).scalars().all()
    return sorted({node_id, *ids})


def known(s: Session) -> dict[int, int]:
    """The identities known to be one person before any scoring: each Person with a trusted SAME_AS link (TRUSTED) ->
    the smallest id in its cluster of such links. A stage that runs before entity resolution folds identities by
    these, never by `canonical`, which still holds the last run's scored merges."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in s.execute(text("""
        SELECT e.src, e.dst FROM edges e JOIN nodes a ON a.id = e.src AND a.type = 'Person'
        JOIN nodes b ON b.id = e.dst AND b.type = 'Person' WHERE e.type = 'SAME_AS' AND e.extractor = ANY(:t)
    """), {"t": list(TRUSTED)}):
        ra, rb = find(a), find(b)
        parent[max(ra, rb)] = min(ra, rb)
    return {n: find(n) for n in list(parent)}


def card_keys(meta: dict) -> list[str]:
    """The Person keys a contact card (raw_meta of a record of source_catalog.CONTACTS) names, each once: its
    addresses lowercased, as mail addresses are keyed, and its phone numbers normalized the way phone handles are
    keyed, so '(614) 555-0199' finds +16145550199."""
    from igneous.connectors.mac_contacts import norm_phone

    keys = [e.strip().lower() for e in meta.get("emails") or [] if e and e.strip()]
    keys += [norm_phone(p) or p for p in meta.get("phones") or [] if p]
    return list(dict.fromkeys(keys))
