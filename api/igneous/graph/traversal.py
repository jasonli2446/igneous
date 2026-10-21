"""Bounded, provenance-filtered graph traversals used by the Explorer API."""
import math
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

# Hub nodes (a big Organization, a mass-mail Thread) would swallow the node budget; skip expanding them.
MAX_FANOUT = 200


@dataclass(frozen=True)
class GraphFilters:
    """Request filters applied both while discovering and serializing edges."""

    source: str | None = None
    edge_types: list[str] | None = None
    since: datetime | None = None
    until: datetime | None = None


NO_FILTERS = GraphFilters()


def _edge_where(filters: GraphFilters, edge: str = "e", item: str = "si") -> str:
    """Build a bound-parameter predicate for provenance-backed graph edges."""
    clauses = []
    if filters.edge_types:
        clauses.append(f"{edge}.type = ANY(:types)")
    if filters.source:
        clauses.append(f"{item}.source = :source")
    # Date filters select intervals that overlap the requested window. Point-in-time communication edges
    # have equal bounds; MEMBER_OF remains visible throughout its observed membership span.
    if filters.since:
        clauses.append(f"COALESCE({edge}.valid_to, {item}.occurred_at, {edge}.valid_from) >= :since")
    if filters.until:
        clauses.append(f"COALESCE({edge}.valid_from, {item}.occurred_at, {edge}.valid_to) <= :until")
    return " AND ".join(clauses) if clauses else "TRUE"


def _filter_params(filters: GraphFilters) -> dict:
    return {"types": filters.edge_types, "source": filters.source, "since": filters.since, "until": filters.until}


def neighborhood(s: Session, seeds: list[int], hops: int = 1, limit: int = 300,
                 filters: GraphFilters = NO_FILTERS) -> dict:
    frontier, seen = list(seeds), list(dict.fromkeys(seeds))
    edge_where = _edge_where(filters)
    for _ in range(hops):
        if not frontier or len(seen) >= limit:
            break
        rows = s.execute(text(f"""
            WITH f AS (SELECT unnest(CAST(:frontier AS bigint[])) AS id),
            deg AS (SELECT f.id, (SELECT count(*) FROM edges e WHERE e.src = f.id OR e.dst = f.id) AS d FROM f)
            SELECT CASE WHEN e.src = deg.id THEN e.dst ELSE e.src END AS nbr
            FROM deg JOIN edges e ON (e.src = deg.id OR e.dst = deg.id)
            LEFT JOIN source_items si ON si.id = e.source_item_id
            WHERE (deg.d <= :fanout OR deg.id = ANY(:seeds)) AND {edge_where}
            LIMIT :room
        """), {"frontier": frontier, "fanout": MAX_FANOUT, "seeds": seeds,
               "room": (limit - len(seen)) * 4, **_filter_params(filters)}).scalars().all()
        known = set(seen)
        new = [n for n in dict.fromkeys(rows) if n not in known][: limit - len(seen)]
        seen += new
        frontier = new
    return subgraph(s, seen, filters)


HALF_LIFE_DAYS = 90  # an interaction's weight halves every 90 days: last week ~1, six months ~0.25, 2 years ~0


def relationships(s: Session, head: int, limit: int) -> list[tuple[int, float, dict]]:
    """Rank the seed's correspondents by how real the relationship is, not raw volume.

    A relationship needs both directions: someone who emailed you once and got no reply (a newsletter, a
    recruiter blast) isn't one. Strength combines volume (log-scaled so one chatty thread can't dominate),
    how many separate weeks you talked, how many channels (texts, email, events), and whether both of you
    wrote. Returns (person, strength, stats), strongest first."""
    canon = "COALESCE((SELECT canonical_id FROM canonical WHERE node_id = {x}), {x})"
    rows = s.execute(text(f"""
        WITH me AS (SELECT :head AS id UNION SELECT node_id FROM canonical WHERE canonical_id = :head),
        pm AS (  -- (person, message, did they send it)
            SELECT {canon.format(x='src')} AS p, dst AS m, true AS sent FROM edges WHERE type = 'SENT'
            UNION ALL
            SELECT {canon.format(x='dst')}, src, false FROM edges WHERE type = 'RECEIVED'),
        mine AS (SELECT m, bool_or(sent) AS i_sent FROM pm WHERE p IN (SELECT id FROM me) GROUP BY m),
        -- How many people each conversation involves: a message in a 12-person group chat says much less about
        -- you and any one member than a one-to-one text, so its weight is split across the others in it.
        size AS (SELECT m, count(DISTINCT p) AS n FROM pm WHERE m IN (SELECT m FROM mine) GROUP BY m),
        theirs AS (
            SELECT pm.p, pm.m, bool_or(pm.sent) AS they_sent, bool_or(mine.i_sent) AS i_sent
            FROM pm JOIN mine USING (m) WHERE pm.p NOT IN (SELECT id FROM me) GROUP BY pm.p, pm.m),
        -- Shared events counted by occurrence, not by series node: a weekly meeting with someone is ~15 shared
        -- occurrences. The ATTENDED edge is one per series (UNIQUE src,dst,type), so read each occurrence's
        -- attendee list from its source item instead.
        my_keys AS (SELECT key FROM nodes WHERE id IN (SELECT id FROM me)),
        occ AS (
            SELECT si.id AS occurrence, lower(a->>'email') AS email, si.occurred_at AS at
            FROM source_items si, jsonb_array_elements(si.raw_meta->'attendees') a
            WHERE si.source = 'gcal' AND COALESCE(a->>'response', '') <> 'declined'
              AND si.occurred_at < now() + interval '60 days'),
        events AS (
            SELECT p, count(*) AS n,
                   sum(power(0.5, GREATEST(0, EXTRACT(EPOCH FROM now() - at) / 86400) / :hl)) AS recent
            FROM (SELECT DISTINCT {canon.format(x='p.id')} AS p, o.occurrence, o.at
                  FROM occ mine JOIN occ o ON o.occurrence = mine.occurrence AND o.email <> mine.email
                  JOIN nodes p ON p.type = 'Person' AND p.key = o.email
                  WHERE mine.email IN (SELECT key FROM my_keys)) shared GROUP BY 1)
        SELECT t.p, count(*) AS msgs,
               count(*) FILTER (WHERE t.they_sent) AS from_them, count(*) FILTER (WHERE t.i_sent) AS from_me,
               count(DISTINCT date_trunc('week', si.occurred_at)) AS weeks,
               count(DISTINCT si.source) FILTER (WHERE sz.n <= 4) AS channels, COALESCE(max(ev.n), 0) AS events,
               -- Recency-weighted volume: each message counts 0.5^(age/half-life), so last week ~1, a year ago ~0.06.
               sum(power(0.5, GREATEST(0, EXTRACT(EPOCH FROM now() - si.occurred_at) / 86400) / :hl)
                   / GREATEST(1, sz.n - 1)) AS recent_msgs,
               sum(1.0 / GREATEST(1, sz.n - 1)) AS direct_msgs,
               -- Weeks you talked one-on-one or in a small group (4 or fewer people); a busy group chat would
               -- otherwise give every member the same "talked every week" credit.
               count(DISTINCT date_trunc('week', si.occurred_at))
                   FILTER (WHERE si.occurred_at > now() - interval '180 days' AND sz.n <= 4) AS recent_weeks,
               COALESCE(max(ev.recent), 0) AS recent_events, max(si.occurred_at) AS last_at
        FROM theirs t JOIN size sz ON sz.m = t.m JOIN nodes msg ON msg.id = t.m
        JOIN source_items si ON si.id = msg.source_item_id
        LEFT JOIN events ev ON ev.p = t.p
        GROUP BY t.p
    """), {"head": head, "hl": HALF_LIFE_DAYS}).mappings().all()
    scored = []
    for r in rows:
        two_way = r["from_them"] > 0 and r["from_me"] > 0
        if not two_way and r["events"] == 0:
            continue  # one-directional: mail you received and never answered, or a list you're on
        # Mostly about now (recency-weighted volume, recent weeks, recent shared events); the lifetime history
        # only adds a little, so an old friendship isn't zero but doesn't crowd out who you talk to today.
        strength = (math.log1p(float(r["recent_msgs"])) * 2.0 + math.log1p(r["recent_weeks"]) * 1.5
                    + math.log1p(float(r["recent_events"])) * 1.2
                    + (r["channels"] + (1 if r["events"] else 0) - 1) * 0.6
                    + (math.log1p(float(r["direct_msgs"])) + math.log1p(r["weeks"])) * 0.25)
        scored.append((r["p"], round(strength, 3), dict(r)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


def people(s: Session, seed: int, limit: int = 60) -> dict:
    """The seed's real relationships (see relationships()) and the ties between those people. Edge weight is
    messages they share; each person node carries `strength`, their relationship with the seed."""
    canon = "COALESCE((SELECT canonical_id FROM canonical WHERE node_id = {x}), {x})"
    head = s.execute(text(f"SELECT {canon.format(x=':seed')}"), {"seed": seed}).scalar_one()
    rel = relationships(s, head, limit)
    ids = [head] + [p for p, _, _ in rel]
    ties = s.execute(text(f"""
        WITH parts AS (
            SELECT {canon.format(x='src')} AS p, dst AS m FROM edges WHERE type = 'SENT'
            UNION SELECT {canon.format(x='dst')}, src FROM edges WHERE type = 'RECEIVED'),
        mine AS (SELECT * FROM parts WHERE p = ANY(:ids))
        SELECT a.p AS src, b.p AS dst, count(*) AS w
        FROM mine a JOIN mine b ON a.m = b.m AND a.p < b.p GROUP BY 1, 2
        -- The seed's own ties always; between others, 2+ shared messages to keep it readable.
        HAVING count(*) >= 2 OR CAST(:head AS bigint) IN (a.p, b.p)
    """), {"ids": ids, "head": head}).all()
    g = subgraph(s, ids)
    # Rank-based 0..1 (closest = 1): raw scores bunch at the top, so sizes from them all looked the same.
    order = {p: i for i, (p, _, _) in enumerate(rel)}
    for n in g["nodes"]:
        n["strength"] = 1.0 if n["id"] == head else round(1 - order.get(n["id"], len(rel)) / max(1, len(rel)), 3)
    g["edges"] = [{"id": -(i + 1), "src": t.src, "dst": t.dst, "type": "CORRESPONDS", "extractor": "derived",
                   "confidence": None, "weight": t.w} for i, t in enumerate(ties)]
    return g


def network(s: Session, seed: int, people_limit: int = 50, hub_limit: int = 30) -> dict:
    """People plus the shared things that join them: group chats, email threads, events. Each hub is a node
    and people link to the hubs they took part in, so friend groups and teams form visible clusters instead
    of a star around the seed. A hub must join 2+ of the seed's correspondents (a thread with only the seed
    and one other person adds nothing a direct tie doesn't). The seed itself is left off hubs: it is in all of
    them, and drawing those spokes would pull the layout back into a star."""
    base = people(s, seed, limit=people_limit)
    if not base["nodes"]:
        return base
    head = base["nodes"][0]["id"]
    others = [n["id"] for n in base["nodes"][1:]]
    canon = "COALESCE((SELECT canonical_id FROM canonical WHERE node_id = {x}), {x})"
    hubs = s.execute(text(f"""
        WITH part AS (  -- (person, hub) for every conversation thread or event they were part of
            SELECT DISTINCT {canon.format(x='pm.p')} AS person, t.dst AS hub
            FROM (SELECT src AS p, dst AS m FROM edges WHERE type = 'SENT'
                  UNION SELECT dst, src FROM edges WHERE type = 'RECEIVED') pm
            JOIN edges t ON t.src = pm.m AND t.type = 'IN_THREAD'
            UNION
            SELECT DISTINCT {canon.format(x='src')}, dst FROM edges WHERE type = 'ATTENDED'),
        mine AS (SELECT * FROM part WHERE person = ANY(:others))
        SELECT hub, array_agg(person) AS members, count(*) AS n
        FROM mine GROUP BY hub HAVING count(*) >= 2
        ORDER BY count(*) DESC LIMIT :hubs
    """), {"others": others, "hubs": hub_limit}).all()
    # How active each member is in each hub (messages), so a link's thickness means something.
    activity = {(r.hub, r.person): r.n for r in s.execute(text(f"""
        SELECT t.dst AS hub, {canon.format(x='pm.p')} AS person, count(DISTINCT pm.m) AS n
        FROM (SELECT src AS p, dst AS m FROM edges WHERE type = 'SENT'
              UNION SELECT dst, src FROM edges WHERE type = 'RECEIVED') pm
        JOIN edges t ON t.src = pm.m AND t.type = 'IN_THREAD'
        WHERE t.dst = ANY(:hubs) GROUP BY 1, 2
    """), {"hubs": [h.hub for h in hubs]})}
    hub_ids = [h.hub for h in hubs]
    g = subgraph(s, [head, *others, *hub_ids])
    strength = {n["id"]: n.get("strength") for n in base["nodes"]}
    for n in g["nodes"]:
        if strength.get(n["id"]) is not None:
            n["strength"] = strength[n["id"]]
    # Keep every edge, including the seed's spokes: prune() decides what survives, and its spanning tree needs
    # the spokes to hold the graph together (dropping them up front left the seed floating in its own island).
    edges = list(base["edges"])
    edges += [{"id": -(100000 + i), "src": p, "dst": h.hub, "type": "PART_OF", "extractor": "derived",
               "confidence": None, "weight": activity.get((h.hub, p), 1)}
              for i, (h, p) in enumerate((h, p) for h in hubs for p in h.members)]
    g["edges"] = prune(g["nodes"], edges, head)
    # Hub nodes carry their size so the UI can say "group chat, 6 people", and readable names.
    sizes = {h.hub: len(h.members) for h in hubs}
    named = handle_names(s)
    for n in g["nodes"]:
        if n["id"] in sizes:
            n["members"] = sizes[n["id"]]
            n["name"] = pretty_thread(n["name"], named) or n["name"]
    return g


def explain(s: Session, a: int, b: int, limit: int = 8) -> dict:
    """Why two nodes are connected: the shared conversations (with message counts and the latest excerpt) and
    shared events, entity-resolved on both sides. This is the evidence behind a CORRESPONDS / PART_OF link."""
    canon = "COALESCE((SELECT canonical_id FROM canonical WHERE node_id = {x}), {x})"
    # Every identity in each side's cluster counts (Ken Lay's 12 addresses are one Ken Lay).
    ids = {}
    for side, n in (("a", a), ("b", b)):
        head = s.execute(text(f"SELECT {canon.format(x=':n')}"), {"n": n}).scalar_one()
        ids[side] = [head, *s.execute(text("SELECT node_id FROM canonical WHERE canonical_id = :h"),
                                      {"h": head}).scalars()]
    names = dict(s.execute(text("SELECT id, name FROM nodes WHERE id = ANY(:i)"), {"i": [a, b]}).all())
    convo = s.execute(text("""
        WITH pm AS (SELECT src AS p, dst AS m FROM edges WHERE type = 'SENT'
                    UNION SELECT dst, src FROM edges WHERE type = 'RECEIVED'),
        shared AS (SELECT DISTINCT x.m FROM pm x JOIN pm y ON x.m = y.m
                   WHERE x.p = ANY(:a) AND y.p = ANY(:b))
        SELECT t.id AS thread_id, t.name AS thread, count(*) AS messages,
               min(si.occurred_at) AS first, max(si.occurred_at) AS last,
               (array_agg(si.source ORDER BY si.occurred_at DESC))[1] AS source,
               (array_agg(left(si.body_text, 220) ORDER BY si.occurred_at DESC))[1] AS excerpt,
               (SELECT count(DISTINCT p) FROM pm JOIN edges it2 ON it2.src = pm.m AND it2.type = 'IN_THREAD'
                WHERE it2.dst = t.id) AS people
        FROM shared JOIN nodes msg ON msg.id = shared.m JOIN source_items si ON si.id = msg.source_item_id
        JOIN edges it ON it.src = shared.m AND it.type = 'IN_THREAD' JOIN nodes t ON t.id = it.dst
        GROUP BY t.id, t.name ORDER BY count(*) DESC LIMIT :lim
    """), {"a": ids["a"], "b": ids["b"], "lim": limit}).mappings().all()
    events = s.execute(text("""
        SELECT e.id, e.name, si.occurred_at AS at
        FROM edges x JOIN edges y ON x.dst = y.dst AND x.type = 'ATTENDED' AND y.type = 'ATTENDED'
        JOIN nodes e ON e.id = x.dst JOIN source_items si ON si.id = e.source_item_id
        WHERE x.src = ANY(:a) AND y.src = ANY(:b) GROUP BY e.id, e.name, si.occurred_at
        ORDER BY si.occurred_at DESC LIMIT :lim
    """), {"a": ids["a"], "b": ids["b"], "lim": limit}).mappings().all()
    total = sum(c["messages"] for c in convo)
    named = handle_names(s)
    out = []
    for c in convo:
        c = dict(c)
        c["thread"] = pretty_thread(c["thread"], named)
        c["excerpt"] = name_handles(c["excerpt"], named)
        out.append(c)
    return {"a": {"id": a, "name": names.get(a)}, "b": {"id": b, "name": names.get(b)},
            "messages": total, "conversations": out, "events": [dict(e) for e in events]}


PHONE = re.compile(r"\+?\d{10,15}")


def handle_names(s: Session) -> dict[str, str]:
    """Phone number / address -> the person's name. A handle takes the name of the person it belongs to: its
    own contact card, or, once identity resolution has folded it into someone (an email address with the same
    name, a contact card listing both), that person's best real name. So learning one identity names them all."""
    rows = s.execute(text("""
        WITH named AS (
            SELECT COALESCE(c.canonical_id, n.id) AS head, n.name,
                   (n.props->>'named_by' = 'contacts') AS card
            FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id
            WHERE n.type = 'Person' AND n.name ~ ' ' AND n.name !~ '@' AND n.name !~ '[0-9]{5,}'
        ), best AS (
            SELECT DISTINCT ON (head) head, name FROM named ORDER BY head, card DESC, length(name) DESC
        )
        SELECT n.key, COALESCE(b.name, CASE WHEN n.props->>'named_by' = 'contacts' THEN n.name END)
        FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id
        LEFT JOIN best b ON b.head = COALESCE(c.canonical_id, n.id)
        WHERE n.type = 'Person' AND (n.key ~ '^\\+?[0-9]{7,15}$' OR n.key LIKE '%@%')
    """)).all()
    out = {}
    for key, name in rows:
        if not name or PHONE.fullmatch(name.strip()):
            continue  # a card with only a number on it names nothing
        out[key] = name
        digits = re.sub(r"\D", "", key)
        if len(digits) >= 10:
            out[digits[-10:]] = name  # message text may carry the number without +1
    return out




def name_handles(t: str | None, named: dict[str, str], mask: bool = False) -> str | None:
    """'[14:39] +16174075277: Yes' -> '[14:39] Sam Lee: Yes' where the number is a saved contact. With `mask`,
    a number with no contact becomes '···5277' (used for labels, where a wall of digits is unreadable)."""
    if not t:
        return t

    def sub(m):
        hit = named.get(m.group(), named.get(re.sub(r"\D", "", m.group())[-10:]))
        return hit or (f"···{m.group()[-4:]}" if mask else m.group())
    return PHONE.sub(sub, t)


def pretty_thread(name: str | None, named: dict[str, str]) -> str | None:
    """An unnamed group chat is titled by its members' handles; show names, and cap long lists."""
    if not name or not PHONE.search(name):
        return name
    parts = [name_handles(p.strip(), named) for p in name.split(",")]
    shown = [p.split()[0] if p and not PHONE.fullmatch(p) else p for p in parts]
    extra = len(shown) - 3
    return ", ".join(shown[:3]) + (f" and {extra} other{'s' if extra != 1 else ''}" if extra > 0 else "")


def prune(nodes: list[dict], edges: list[dict], seed: int, per_node: int = 4) -> list[dict]:
    """Keep the connections that explain the structure: each node's strongest `per_node` links, plus a maximum
    spanning tree so no group gets cut off. Drops the long tail of one-off links that turn the graph into a
    hairball without saying anything. The seed doesn't get a top-N of its own (it touches everyone, so its
    top-N would just be a star); it keeps only the spokes the tree needs to hold groups together."""
    def w(e):
        return e.get("weight") or 1

    keep: set[int] = set()
    by_node: dict[int, list[dict]] = {}
    for e in edges:
        by_node.setdefault(e["src"], []).append(e)
        by_node.setdefault(e["dst"], []).append(e)
    for node, es in by_node.items():
        if node == seed:
            continue
        # A person's strongest ties to *others*. Spokes to the seed are left to the spanning tree below, which
        # keeps exactly the ones needed to attach each group (else it's a star again).
        keep.update(id(e) for e in sorted((e for e in es if seed not in (e["src"], e["dst"])), key=w,
                                          reverse=True)[:per_node])
    # Kruskal for a maximum spanning forest: guarantees connectivity with the strongest possible bridges.
    parent = {n["id"]: n["id"] for n in nodes}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # Peer edges first (strongest first), then spokes: so a group hangs off the seed by one spoke, not many.
    for e in sorted(edges, key=lambda e: (seed in (e["src"], e["dst"]), -w(e))):
        ra, rb = find(e["src"]), find(e["dst"])
        if ra != rb:
            parent[ra] = rb
            keep.add(id(e))
    return [e for e in edges if id(e) in keep]


def subgraph(s: Session, node_ids: list[int], filters: GraphFilters = NO_FILTERS) -> dict:
    nodes = s.execute(text("""
        SELECT n.id, n.type, n.name, n.key,
               (SELECT count(*) FROM edges e WHERE e.src = n.id OR e.dst = n.id) AS degree
        FROM nodes n WHERE n.id = ANY(:ids)
        ORDER BY array_position(CAST(:ids AS bigint[]), n.id)
    """), {"ids": node_ids}).mappings().all()
    edge_where = _edge_where(filters)
    edges = s.execute(text(f"""
        SELECT e.id, e.src, e.dst, e.type, e.extractor, e.confidence FROM edges e
        LEFT JOIN source_items si ON si.id = e.source_item_id
        WHERE e.src = ANY(:ids) AND e.dst = ANY(:ids) AND {edge_where}
    """), {"ids": node_ids, **_filter_params(filters)}).mappings().all()
    return {"nodes": [dict(n) for n in nodes], "edges": [dict(e) for e in edges]}


# ---------------------------------------------------------------------------------------------------------
# One graph, progressive disclosure. The seed links to category hubs that exist in *this* data (a hub only
# appears if it has members), each showing its strongest few members; clicking any node reveals that node's
# top connections. Nothing is configured: categories come from node types, ordered by how much they matter.
# ---------------------------------------------------------------------------------------------------------

# How a node type is shown. Presentational only; a type with no members in the data never appears.
CATEGORY = {"Person": "People", "Group": "Groups", "Event": "Events", "Organization": "Organizations",
            "Project": "Projects", "Course": "Courses", "Repo": "Code", "File": "Files", "Topic": "Topics",
            "Place": "Places"}
HUB_ID_BASE = -1000  # category hubs are virtual nodes with stable negative ids


def _canon_head(s: Session, node: int) -> int:
    return s.execute(text(
        "SELECT COALESCE((SELECT canonical_id FROM canonical WHERE node_id = :n), :n)"), {"n": node}).scalar_one()


def _my_ids(s: Session, head: int) -> list[int]:
    return [head, *s.execute(text("SELECT node_id FROM canonical WHERE canonical_id = :h AND node_id <> :h"),
                             {"h": head}).scalars()]


def scored_members(s: Session, head: int, per: int = 8) -> dict[str, list[dict]]:
    """Everything related to the seed, scored by how strongly, grouped by category. Rank-normalized 0..1."""
    me = _my_ids(s, head)
    out: dict[str, list[dict]] = {}

    # People: real (two-way) relationships, already scored.
    rel = relationships(s, head, 200)
    people = [{"id": p, "strength": st} for p, st, _ in rel]
    if people:
        out["People"] = people

    # Groups: group conversations the seed is in, by messages the seed shared there and distinct members.
    groups = s.execute(text("""
        WITH mine AS (SELECT dst AS m FROM edges WHERE src = ANY(:me) AND type = 'SENT'
                      UNION SELECT src FROM edges WHERE dst = ANY(:me) AND type = 'RECEIVED'),
        g AS (SELECT it.dst AS t, count(DISTINCT mine.m) AS msgs,
                     sum(power(0.5, GREATEST(0, EXTRACT(EPOCH FROM now() - si.occurred_at) / 86400) / :hl)) AS recent
              FROM (SELECT DISTINCT m FROM mine) mine
              JOIN edges it ON it.src = mine.m AND it.type = 'IN_THREAD'
              JOIN nodes msg ON msg.id = mine.m LEFT JOIN source_items si ON si.id = msg.source_item_id
              GROUP BY it.dst)
        SELECT g.t, g.msgs, COALESCE(g.recent, 0),
               (SELECT count(DISTINCT x.src) FROM edges it2 JOIN edges x ON x.dst = it2.src AND x.type = 'SENT'
                WHERE it2.dst = g.t AND it2.type = 'IN_THREAD') AS people
        FROM g WHERE g.msgs >= 2
    """), {"me": me, "hl": HALF_LIFE_DAYS}).all()
    # A group you're active in now outranks a big chat that went quiet; lifetime volume only adds a little.
    gl = [{"id": t, "strength": math.log1p(float(recent)) * 2 + math.log1p(people) * 0.5 + math.log1p(msgs) * 0.25}
          for t, msgs, recent, people in groups if people >= 3]
    if gl:
        out["Groups"] = gl

    # Events: series the seed attended, by occurrences (so a weekly meeting outranks a one-off).
    keys = [k for (k,) in s.execute(text("SELECT key FROM nodes WHERE id = ANY(:me)"), {"me": me})]
    events = s.execute(text("""
        SELECT id, sum(power(0.5, GREATEST(0, EXTRACT(EPOCH FROM now() - at) / 86400) / :hl)) AS occ FROM (
        SELECT DISTINCT n.id, si.id AS occurrence, si.occurred_at AS at
        FROM source_items si
        CROSS JOIN LATERAL jsonb_array_elements(si.raw_meta->'attendees') a
        JOIN nodes n ON n.type = 'Event' AND n.key = COALESCE(si.raw_meta->>'event_key', 'gcal:' || COALESCE(si.raw_meta->>'series_key', si.external_id))
        WHERE si.source = 'gcal' AND lower(a->>'email') = ANY(:keys) AND COALESCE(a->>'response','') <> 'declined'
          AND si.occurred_at < now() + interval '60 days'  -- far-future instances of a series aren't "now"
        ) mine GROUP BY id
    """), {"keys": keys, "hl": HALF_LIFE_DAYS}).all()
    # Future occurrences count as "now" (an upcoming weekly meeting is current), past ones decay.
    el = [{"id": e, "strength": math.log1p(float(occ))} for e, occ in events]
    if el:
        out["Events"] = el

    # Organizations: the domains of the people the seed actually has relationships with, weighted by them.
    if people:
        orgs = s.execute(text("""
            SELECT e.dst AS org, sum(p.w) AS w, count(*) AS n
            FROM (SELECT unnest(CAST(:ids AS bigint[])) AS id, unnest(CAST(:ws AS float8[])) AS w) p
            JOIN edges e ON e.src = p.id AND e.type = 'WORKED_AT'
            JOIN nodes o ON o.id = e.dst
            WHERE o.key NOT IN ('gmail.com','icloud.com','yahoo.com','hotmail.com','outlook.com','me.com','aol.com')
            GROUP BY e.dst
        """), {"ids": [p["id"] for p in people], "ws": [p["strength"] for p in people]}).all()
        ol = [{"id": o, "strength": float(w) + math.log1p(n)} for o, w, n in orgs]
        if ol:
            out["Organizations"] = ol

    # Projects (derived by graph/entities.py): the ones you're in, by how many of their items involve you.
    projects = s.execute(text("""
        WITH mine AS (SELECT dst AS i FROM edges WHERE src = ANY(:me) AND type IN ('SENT', 'ATTENDED')
                      UNION SELECT src FROM edges WHERE dst = ANY(:me) AND type = 'RECEIVED'),
        via AS (SELECT pe.dst AS p, pe.src AS i FROM edges pe WHERE pe.type = 'PART_OF'
                UNION SELECT pe.dst, it.src FROM edges pe JOIN edges it ON it.dst = pe.src AND it.type = 'IN_THREAD'
                WHERE pe.type = 'PART_OF')
        SELECT via.p, count(DISTINCT via.i) AS n,
               (SELECT count(*) FROM edges x WHERE x.dst = via.p AND x.type = 'INVOLVED_IN') AS people,
               COALESCE(sum(power(0.5, GREATEST(0, EXTRACT(EPOCH FROM now() - si.occurred_at) / 86400) / :hl)), 0)
        FROM (SELECT DISTINCT p, i FROM via) via JOIN (SELECT DISTINCT i FROM mine) mine ON mine.i = via.i
        JOIN nodes n ON n.id = via.p AND n.type = 'Project' AND NOT COALESCE((n.props->>'hidden')::bool, false)
        JOIN nodes item ON item.id = via.i LEFT JOIN source_items si ON si.id = item.source_item_id
        GROUP BY via.p
    """), {"me": me, "hl": HALF_LIFE_DAYS}).all()
    pl = [{"id": p, "strength": math.log1p(float(recent)) * 2 + math.log1p(n) * 0.25 + 0.5 * math.log1p(people)}
          for p, n, people, recent in projects]
    if pl:
        out["Projects"] = pl

    # Places (Google Takeout Maps): where you actually go, by visit count and distinct days; saved places count too.
    if s.execute(text("SELECT EXISTS (SELECT 1 FROM nodes WHERE id = ANY(:me) AND props->>'self' = 'true')"),
                 {"me": me}).scalar():
        places = s.execute(text("""
            WITH v AS (
                SELECT x->>'key' AS key, CAST(x->>'start' AS timestamptz) AS at
                FROM source_items si CROSS JOIN LATERAL jsonb_array_elements(si.raw_meta->'visits') x
                WHERE si.source = 'takeout_maps' AND si.raw_meta->>'kind' = 'visits'),
            seen AS (SELECT n.id, count(*) AS visits, count(DISTINCT CAST(v.at AS date)) AS days
                     FROM v JOIN nodes n ON n.type = 'Place' AND n.key = v.key GROUP BY n.id),
            saved AS (SELECT DISTINCT dst AS id FROM edges WHERE src = ANY(:me) AND type = 'SAVED')
            SELECT COALESCE(seen.id, saved.id), COALESCE(seen.visits, 0), COALESCE(seen.days, 0),
                   (saved.id IS NOT NULL) AS is_saved
            FROM seen FULL JOIN saved ON saved.id = seen.id
            WHERE saved.id IS NOT NULL OR seen.days >= 2
        """), {"me": me}).all()
        pl = [{"id": p, "strength": math.log1p(v) + math.log1p(d) + (1.0 if sv else 0.0)} for p, v, d, sv in places]
        if pl:
            out["Places"] = pl

    # One scale for everything: each item's raw score divided by the strongest raw score in its category gives
    # 0..1 within the category; multiplying by how strong that category's best item is relative to the overall
    # best keeps a category of stale things (a class from two years ago) from claiming the top spots.
    tops = {c: max((it["strength"] for it in items), default=0) for c, items in out.items()}
    overall = max(tops.values(), default=0) or 1
    for c, items in out.items():
        items.sort(key=lambda x: x["strength"], reverse=True)
        for it in items:
            rel = it["strength"] / (tops[c] or 1)
            it["strength"] = round(rel * (0.4 + 0.6 * tops[c] / overall), 3)
    return out


def _hub(cat: str, total: int, strength: float) -> dict:
    return {"id": HUB_ID_BASE - list(CATEGORY.values()).index(cat), "type": "Category", "name": cat,
            "key": f"category:{cat}", "degree": total, "kind": "category", "category": cat,
            "total": total, "strength": strength}


def view(s: Session, seed: int, per: int = 6) -> dict:
    """The starting view: the seed and each category's strongest `per` members, linked straight to the seed and
    colored by category (no hub nodes), plus the strongest ties among them. `categories` lists every category
    with its total, for the legend; clicking one there reveals more of its members (see `expand`)."""
    head = _canon_head(s, seed)
    cats = scored_members(s, head, per)
    # Order categories by how much they matter: summed strength of their top members, normalized by size.
    order = sorted(cats, key=lambda c: -sum(m["strength"] for m in cats[c][:10]))
    shown: list[int] = [head]
    edges: list[dict] = []
    # The first ring is the strongest `per * 3` things overall, whatever kind they are (at least one of each
    # kind that has anything current, so a category is never invisible up front).
    ranked = sorted(((m, c) for c in order for m in cats[c]), key=lambda mc: -mc[0]["strength"])
    # People are the densest signal, so cap them at 60% of the ring: the projects, groups, trips and classes you
    # are in always get room, even when dozens of people outscore them.
    size = per * 3
    people = [mc for mc in ranked if mc[1] == "People"][:int(size * 0.6)]
    others = [mc for mc in ranked if mc[1] != "People"][:size - len(people)]
    first = sorted(people + others, key=lambda mc: -mc[0]["strength"])
    if len(first) < size:  # not enough of the rest: fill with more people
        first += [mc for mc in ranked if mc[1] == "People" and mc not in first][:size - len(first)]
    for c in order:
        if cats[c] and not any(fc == c for _, fc in first) and cats[c][0]["strength"] >= 0.15:
            first.append((cats[c][0], c))
    for m, _c in first:
        shown.append(m["id"])
        edges.append({"id": -(len(edges) + 1) * 7, "src": head, "dst": m["id"], "type": "RELATED",
                      "weight": 1 + 4 * m["strength"]})
    g = subgraph(s, shown)
    strength = {m["id"]: (m["strength"], c) for c in cats for m in cats[c]}
    for n in g["nodes"]:
        st, cat = strength.get(n["id"], (1.0, None))
        n.update(strength=st, kind="seed" if n["id"] == head else "entity", category=cat,
                 expandable=n["id"] != head)
    named = handle_names(s)
    for n in g["nodes"]:
        if n["type"] == "Thread":
            n["name"] = pretty_thread(n["name"], named) or n["name"]
    g["edges"] = edges + _ties(s, [x for x in shown if x != head], per_node=2)
    g["categories"] = [_hub(c, len(cats[c]), round(sum(m["strength"] for m in cats[c][:10]) / 10, 3))
                       for c in order]
    g["seed"] = head
    return g


def _ties(s: Session, ids: list[int], per_node: int = 2) -> list[dict]:
    """The strongest relationships among a set of visible nodes: people who share messages, people in the same
    group chat or event. Only each node's top `per_node`, so the picture stays readable."""
    if len(ids) < 2:
        return []
    canon = "COALESCE((SELECT canonical_id FROM canonical WHERE node_id = {x}), {x})"
    rows = s.execute(text(f"""
        WITH pm AS (SELECT {canon.format(x='src')} AS p, dst AS m FROM edges WHERE type = 'SENT'
                    UNION SELECT {canon.format(x='dst')}, src FROM edges WHERE type = 'RECEIVED'),
        part AS (  -- person -> thread or event they're in
            SELECT pm.p, it.dst AS hub FROM pm JOIN edges it ON it.src = pm.m AND it.type = 'IN_THREAD'
            UNION SELECT {canon.format(x='src')}, dst FROM edges WHERE type = 'ATTENDED'),
        vis AS (SELECT unnest(CAST(:ids AS bigint[])) AS id),
        pp AS (  -- visible person <-> visible person, by shared messages
            SELECT a.p AS x, b.p AS y, count(*) AS w, 'CORRESPONDS' AS t
            FROM pm a JOIN pm b ON a.m = b.m AND a.p < b.p
            WHERE a.p IN (SELECT id FROM vis) AND b.p IN (SELECT id FROM vis) GROUP BY 1, 2),
        ph AS (  -- visible person -> visible group/event they belong to
            SELECT part.p AS x, part.hub AS y, 1 AS w, 'PART_OF' AS t FROM part
            WHERE part.p IN (SELECT id FROM vis) AND part.hub IN (SELECT id FROM vis))
        SELECT x, y, w, t FROM pp UNION ALL SELECT x, y, w, t FROM ph
    """), {"ids": ids}).all()
    edges = [{"id": -(500000 + i), "src": x, "dst": y, "type": t, "weight": int(w)} for i, (x, y, w, t) in enumerate(rows)]
    by: dict[int, list[dict]] = {}
    for e in edges:
        by.setdefault(e["src"], []).append(e)
        by.setdefault(e["dst"], []).append(e)
    keep = {id(e) for es in by.values() for e in sorted(es, key=lambda e: -e["weight"])[:per_node]}
    return [e for e in edges if id(e) in keep]


def expand(s: Session, seed: int, node: int, visible: list[int], k: int = 6, offset: int = 0) -> dict:
    """Reveal `node`'s top connections that aren't on screen yet. A category hub reveals its next members; a
    person reveals their strongest people, group chats, and events; a group or event reveals who's in it."""
    head = _canon_head(s, seed)
    seen = set(visible)
    new: list[int] = []
    edges: list[dict] = []
    if node <= HUB_ID_BASE + 10 and node < 0:  # a category hub
        cat = list(CATEGORY.values())[HUB_ID_BASE - node]
        members = scored_members(s, head).get(cat, [])
        for m in [m for m in members if m["id"] not in seen][offset:offset + k]:
            new.append(m["id"])
            # No hub node on screen: more members of a category attach to the seed, like the first ones.
            edges.append({"id": -(900000 + m["id"]), "src": head, "dst": m["id"], "type": "RELATED", "weight": 1})
    else:
        ntype = s.execute(text("SELECT type FROM nodes WHERE id = :n"), {"n": node}).scalar()
        if ntype == "Person":
            for p, _st, _ in relationships(s, _canon_head(s, node), 40):
                if p not in seen and p != head and len(new) < k:
                    new.append(p)
        elif ntype == "Project":  # its people, then its group chats, events and items (none if hidden)
            rows = [] if s.execute(text("SELECT COALESCE((props->>'hidden')::bool, false) FROM nodes WHERE id = :n"),
                                   {"n": node}).scalar() else s.execute(text("""
                SELECT src, CASE n.type WHEN 'Person' THEN 0 WHEN 'Thread' THEN 1 WHEN 'Event' THEN 2 ELSE 3 END,
                       COALESCE((e.props->>'items')::int, (e.props->>'days')::int, 1)
                FROM edges e JOIN nodes n ON n.id = e.src
                WHERE e.dst = :n AND e.type IN ('PART_OF', 'INVOLVED_IN') ORDER BY 2, 3 DESC LIMIT 80
            """), {"n": node}).all()
            per_kind: dict[int, int] = {}
            for p, kind, _w in rows:
                if p not in seen and p != head and per_kind.get(kind, 0) < max(2, k // 2) and len(new) < k * 2:
                    new.append(p)
                    per_kind[kind] = per_kind.get(kind, 0) + 1
        else:  # a group, event, org: its members, strongest first
            rows = s.execute(text("""
                SELECT COALESCE((SELECT canonical_id FROM canonical WHERE node_id = p), p) AS p, count(*) AS n FROM (
                    SELECT x.src AS p FROM edges it JOIN edges x ON x.dst = it.src AND x.type = 'SENT'
                    WHERE it.dst = :n AND it.type = 'IN_THREAD'
                    UNION ALL SELECT src FROM edges WHERE dst = :n AND type IN ('ATTENDED', 'MEMBER_OF')) q
                GROUP BY 1 ORDER BY 2 DESC LIMIT 40
            """), {"n": node}).all()
            for p, _n in rows:
                if p not in seen and p != head and len(new) < k:
                    new.append(p)
        for p in new:
            edges.append({"id": -(800000 + p * 13 + node % 997), "src": node, "dst": p, "type": "RELATED", "weight": 1})
    g = subgraph(s, new)
    named = handle_names(s)
    for n in g["nodes"]:
        n.update(kind="entity", expandable=True, strength=0.5,
                 category=CATEGORY.get("Group" if n["type"] == "Thread" else n["type"]))
        if n["type"] == "Thread":
            n["name"] = pretty_thread(n["name"], named) or n["name"]
    g["edges"] = edges + _ties(s, [*new, *[v for v in visible if v > 0]], per_node=1)
    return g
