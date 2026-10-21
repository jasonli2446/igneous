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
        theirs AS (
            SELECT pm.p, pm.m, bool_or(pm.sent) AS they_sent, bool_or(mine.i_sent) AS i_sent
            FROM pm JOIN mine USING (m) WHERE pm.p NOT IN (SELECT id FROM me) GROUP BY pm.p, pm.m),
        -- Shared events counted by occurrence, not by series node: a weekly meeting with someone is ~15 shared
        -- occurrences. The ATTENDED edge is one per series (UNIQUE src,dst,type), so read each occurrence's
        -- attendee list from its source item instead.
        my_keys AS (SELECT key FROM nodes WHERE id IN (SELECT id FROM me)),
        occ AS (
            SELECT si.id AS occurrence, lower(a->>'email') AS email
            FROM source_items si, jsonb_array_elements(si.raw_meta->'attendees') a
            WHERE si.source = 'gcal' AND COALESCE(a->>'response', '') <> 'declined'),
        events AS (
            SELECT {canon.format(x='p.id')} AS p, count(DISTINCT o.occurrence) AS n
            FROM occ mine JOIN occ o ON o.occurrence = mine.occurrence AND o.email <> mine.email
            JOIN nodes p ON p.type = 'Person' AND p.key = o.email
            WHERE mine.email IN (SELECT key FROM my_keys) GROUP BY 1)
        SELECT t.p, count(*) AS msgs,
               count(*) FILTER (WHERE t.they_sent) AS from_them, count(*) FILTER (WHERE t.i_sent) AS from_me,
               count(DISTINCT date_trunc('week', si.occurred_at)) AS weeks,
               count(DISTINCT si.source) AS channels, COALESCE(max(ev.n), 0) AS events
        FROM theirs t JOIN nodes msg ON msg.id = t.m JOIN source_items si ON si.id = msg.source_item_id
        LEFT JOIN events ev ON ev.p = t.p
        GROUP BY t.p
    """), {"head": head}).mappings().all()
    scored = []
    for r in rows:
        two_way = r["from_them"] > 0 and r["from_me"] > 0
        if not two_way and r["events"] == 0:
            continue  # one-directional: mail you received and never answered, or a list you're on
        strength = (math.log1p(r["msgs"]) * 1.0 + math.log1p(r["weeks"]) * 1.5
                    + (r["channels"] + (1 if r["events"] else 0) - 1) * 1.2 + math.log1p(r["events"]) * 0.8)
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


def handle_names(s: Session) -> dict[str, str]:
    """Phone number / address -> contact name, for every Person a contact card has named."""
    rows = s.execute(text("""
        SELECT key, name FROM nodes WHERE type = 'Person' AND props->>'named_by' = 'contacts'
    """)).all()
    out = {}
    for key, name in rows:
        out[key] = name
        digits = re.sub(r"\D", "", key)
        if len(digits) >= 10:
            out[digits[-10:]] = name  # message text may carry the number without +1
    return out


PHONE = re.compile(r"\+?\d{10,15}")


def name_handles(t: str | None, named: dict[str, str]) -> str | None:
    """'[14:39] +16174075277: Yes' -> '[14:39] Sam Lee: Yes' where the number is a saved contact."""
    if not t:
        return t
    return PHONE.sub(lambda m: named.get(m.group(), named.get(re.sub(r"\D", "", m.group())[-10:], m.group())), t)


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
            "Project": "Projects", "Course": "Courses", "Repo": "Code", "File": "Files", "Topic": "Topics"}
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
        g AS (SELECT it.dst AS t, count(DISTINCT mine.m) AS msgs FROM mine
              JOIN edges it ON it.src = mine.m AND it.type = 'IN_THREAD' GROUP BY it.dst)
        SELECT g.t, g.msgs,
               (SELECT count(DISTINCT x.src) FROM edges it2 JOIN edges x ON x.dst = it2.src AND x.type = 'SENT'
                WHERE it2.dst = g.t AND it2.type = 'IN_THREAD') AS people
        FROM g WHERE g.msgs >= 2
    """), {"me": me}).all()
    gl = [{"id": t, "strength": math.log1p(msgs) + math.log1p(people)} for t, msgs, people in groups if people >= 3]
    if gl:
        out["Groups"] = gl

    # Events: series the seed attended, by occurrences (so a weekly meeting outranks a one-off).
    keys = [k for (k,) in s.execute(text("SELECT key FROM nodes WHERE id = ANY(:me)"), {"me": me})]
    events = s.execute(text("""
        SELECT n.id, count(DISTINCT si.id) AS occ
        FROM source_items si
        CROSS JOIN LATERAL jsonb_array_elements(si.raw_meta->'attendees') a
        JOIN nodes n ON n.type = 'Event' AND n.key = 'gcal:' || COALESCE(si.raw_meta->>'series_key', si.external_id)
        WHERE si.source = 'gcal' AND lower(a->>'email') = ANY(:keys) AND COALESCE(a->>'response','') <> 'declined'
        GROUP BY n.id
    """), {"keys": keys}).all()
    el = [{"id": e, "strength": math.log1p(occ)} for e, occ in events]
    if el:
        out["Events"] = el

    # Organizations: the domains of the people the seed actually has relationships with, weighted by them.
    if people:
        orgs = s.execute(text("""
            SELECT e.dst AS org, sum(p.w) AS w, count(*) AS n
            FROM (SELECT unnest(CAST(:ids AS bigint[])) AS id, unnest(CAST(:ws AS float8[])) AS w) p
            JOIN edges e ON e.src = p.id AND e.type = 'MEMBER_OF'
            JOIN nodes o ON o.id = e.dst
            WHERE o.key NOT IN ('gmail.com','icloud.com','yahoo.com','hotmail.com','outlook.com','me.com','aol.com')
            GROUP BY e.dst
        """), {"ids": [p["id"] for p in people], "ws": [p["strength"] for p in people]}).all()
        ol = [{"id": o, "strength": float(w) + math.log1p(n)} for o, w, n in orgs]
        if ol:
            out["Organizations"] = ol

    for items in out.values():
        items.sort(key=lambda x: x["strength"], reverse=True)
        n = len(items)
        for i, it in enumerate(items):
            it["strength"] = round(1 - i / max(1, n), 3)  # rank-normalized so sizes spread
    return out


def _hub(cat: str, total: int, strength: float) -> dict:
    return {"id": HUB_ID_BASE - list(CATEGORY.values()).index(cat), "type": "Category", "name": cat,
            "key": f"category:{cat}", "degree": total, "kind": "category", "category": cat,
            "total": total, "strength": strength}


def view(s: Session, seed: int, per: int = 6) -> dict:
    """The starting view: seed -> category hubs -> each hub's strongest `per` members, plus the strongest
    ties among the visible members (not every connection: just the top ones, which is what makes it readable)."""
    head = _canon_head(s, seed)
    cats = scored_members(s, head, per)
    # Order categories by how much they matter: summed strength of their top members, normalized by size.
    order = sorted(cats, key=lambda c: -sum(m["strength"] for m in cats[c][:10]))
    shown: list[int] = [head]
    nodes_extra: list[dict] = []
    edges: list[dict] = []
    for c in order:
        hub = _hub(c, len(cats[c]), round(sum(m["strength"] for m in cats[c][:10]) / 10, 3))
        nodes_extra.append(hub)
        edges.append({"id": hub["id"] * 10, "src": head, "dst": hub["id"], "type": "HAS", "weight": hub["total"]})
        for m in cats[c][:per]:
            shown.append(m["id"])
            edges.append({"id": -(len(edges) + 1) * 7, "src": hub["id"], "dst": m["id"], "type": "IN_CATEGORY",
                          "weight": 1})
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
    g["nodes"] = [*g["nodes"], *nodes_extra]
    g["edges"] = edges + _ties(s, [x for x in shown if x != head], per_node=2)
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
            edges.append({"id": -(900000 + m["id"]), "src": node, "dst": m["id"], "type": "IN_CATEGORY", "weight": 1})
    else:
        ntype = s.execute(text("SELECT type FROM nodes WHERE id = :n"), {"n": node}).scalar()
        if ntype == "Person":
            for p, _st, _ in relationships(s, _canon_head(s, node), 40):
                if p not in seen and p != head and len(new) < k:
                    new.append(p)
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
