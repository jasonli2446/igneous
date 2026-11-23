"""Plain-language summary of how one node connects to the rest of the graph (`GET /graph/node/{id}/summary`).

The facts come from edges and source items only, read the way the explorer reads them: a person is all of their
identities, and nothing hidden and no service is a connection. Each connection carries the sources of its own
evidence (the messages two people share, the records behind someone's organization).

A model writes the summary only under the rule every hosted call follows (igneous/llm.py): the prompt names the node
and its connections only when `llm.visible_nodes` allows it and every source behind the connection is opted in
(IGNEOUS_LLM_SOURCES); any other connection is a count. It carries no record text except a few subject lines from
opted-in records. Model summaries are cached in `llm_cache`, keyed by node and a hash of the exact prompt, so they
refresh when the node's connections change. With the model off, unconfigured, or failing, or nothing it may name, a
deterministic summary built from the same facts is returned instead.
"""
import hashlib
import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings
from igneous.graph import identity
from igneous.graph.organizations import PERSONAL_DOMAINS
from igneous.graph.traversal import category, label, names
from igneous.graph.visibility import group_conversation, shown, visible

log = logging.getLogger(__name__)
PROMPT_VERSION = "v2"
MAX_CONNECTIONS = 14
NO_SUBJECT = ("(no subject)", "")
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"],
          "additionalProperties": False}
SYSTEM = (
    "You write a short note, 2 to 4 plain sentences, about how one item in someone's personal knowledge graph "
    "connects to the rest of it. Use only the facts provided. Refer to people and things by name, never by id or "
    "number code. Do not speculate about motives or content you were not given. No markdown, no lists.")

LABEL = {"Thread": "conversation", "Organization": "organization", "Person": "person"}
# How someone's organization is known, by the extractor of the WORKED_AT or MEMBER_OF link.
ORG_WHY = {"organizations": "From their email address or contact card", "profile": "From your resume or LinkedIn",
           "user": "You said so", "linkedin": "From their LinkedIn"}


def _month(d: datetime | str | None) -> str:
    if d is None:
        return ""
    if isinstance(d, str):
        d = datetime.fromisoformat(d)
    return d.strftime("%b %Y")


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _conn(row, kind: str, why: str, sources) -> dict:
    return {"id": row["id"], "name": row["name"], "type": row["type"],
            "category": category(row["type"], row.get("kind")), "count": int(row.get("n") or 0),
            "last": row.get("last"), "kind": kind, "why": why, "sources": sorted(s for s in (sources or []) if s)}


# The visible messages a person's identities (:ids) sent or received.
_MINE = f"""
    WITH mine AS (SELECT DISTINCT m.id AS m FROM (
                      SELECT dst AS m FROM edges WHERE src = ANY(:ids) AND type = 'SENT'
                      UNION ALL SELECT src FROM edges WHERE dst = ANY(:ids) AND type = 'RECEIVED') x
                  JOIN nodes m ON m.id = x.m AND {visible('m')})
"""


def _person_connections(s: Session, ids: list[int]) -> tuple[dict, list[dict]]:
    stats = s.execute(text(_MINE + """
        SELECT count(*) AS n, min(si.occurred_at) AS first, max(si.occurred_at) AS last,
               count(*) FILTER (WHERE si.occurred_at > now() - interval '30 days') AS recent,
               array_agg(DISTINCT si.source) AS sources
        FROM mine JOIN nodes m ON m.id = mine.m JOIN source_items si ON si.id = m.source_item_id
    """), {"ids": ids}).mappings().one()
    out: list[dict] = []
    for r in s.execute(text(_MINE + f"""
        , pairs AS (SELECT COALESCE(c.canonical_id, e.src) AS id, mine.m FROM mine
                    JOIN edges e ON e.dst = mine.m AND e.type = 'SENT' LEFT JOIN canonical c ON c.node_id = e.src
                    UNION SELECT COALESCE(c.canonical_id, e.dst), mine.m FROM mine
                    JOIN edges e ON e.src = mine.m AND e.type = 'RECEIVED' LEFT JOIN canonical c ON c.node_id = e.dst)
        SELECT n.id, n.name, n.type, count(*) AS n, max(si.occurred_at) AS last, array_agg(DISTINCT si.source) AS sources
        FROM pairs JOIN nodes n ON n.id = pairs.id AND n.type = 'Person' AND {shown('n')}
        JOIN nodes msg ON msg.id = pairs.m JOIN source_items si ON si.id = msg.source_item_id
        WHERE pairs.id <> ALL(:ids)
        GROUP BY n.id, n.name, n.type ORDER BY count(*) DESC, n.id LIMIT 8
    """), {"ids": ids}).mappings():
        out.append(_conn(r, "person", f"{_plural(r['n'], 'message')} together, last {_month(r['last'])}", r["sources"]))
    for r in s.execute(text(_MINE + f"""
        SELECT t.id, t.name, t.type, count(*) AS n, max(si.occurred_at) AS last, array_agg(DISTINCT si.source) AS sources
        FROM mine JOIN edges it ON it.src = mine.m AND it.type = 'IN_THREAD'
        JOIN nodes t ON t.id = it.dst AND {group_conversation('t')}
        JOIN nodes msg ON msg.id = mine.m JOIN source_items si ON si.id = msg.source_item_id
        WHERE t.name <> ALL(:generic) GROUP BY t.id, t.name, t.type
        ORDER BY count(*) DESC, t.id LIMIT 4
    """), {"ids": ids, "generic": list(NO_SUBJECT)}).mappings():
        out.append(_conn(r, "thread", f"{_plural(r['n'], 'message')} in it, last {_month(r['last'])}", r["sources"]))
    # Their organizations, each with the sources of the evidence behind it (never those of their messages).
    for r in s.execute(text(f"""
        SELECT o.id, o.name, o.type, (array_agg(e.extractor ORDER BY e.extractor = 'organizations', e.id))[1] AS how,
               array_agg(DISTINCT si.source) FILTER (WHERE si.source IS NOT NULL) AS sources
        FROM edges e JOIN nodes o ON o.id = e.dst AND o.type = 'Organization' AND {visible('o')}
        LEFT JOIN edge_evidence ev ON ev.edge_id = e.id
        LEFT JOIN source_items si ON si.id IN (e.source_item_id, ev.source_item_id)
        WHERE e.src = ANY(:ids) AND e.type IN ('MEMBER_OF', 'WORKED_AT') AND o.key <> ALL(:personal)
        GROUP BY o.id, o.name, o.type ORDER BY o.id LIMIT 3
    """), {"ids": ids, "personal": sorted(PERSONAL_DOMAINS)}).mappings():
        out.append(_conn(r, "org", ORG_WHY.get(r["how"], "Linked to them"), r["sources"]))
    for r in s.execute(text(f"""
        SELECT ev.id, ev.name, ev.type, si.occurred_at AS last, array_agg(DISTINCT si.source) AS sources
        FROM edges e JOIN nodes ev ON ev.id = e.dst AND ev.type = 'Event' AND {visible('ev')}
        LEFT JOIN source_items si ON si.id = ev.source_item_id
        WHERE e.src = ANY(:ids) AND e.type = 'ATTENDED' GROUP BY ev.id, ev.name, ev.type, si.occurred_at
        ORDER BY si.occurred_at DESC NULLS LAST, ev.id LIMIT 3
    """), {"ids": ids}).mappings():
        out.append(_conn(r, "event", f"Attended{', ' + _month(r['last']) if r['last'] else ''}", r["sources"]))
    # Topics their messages are about, with the sources of exactly those messages.
    for r in s.execute(text(_MINE + f"""
        SELECT tp.id, tp.name, tp.type, count(*) AS n, array_agg(DISTINCT si.source) AS sources
        FROM mine JOIN edges a ON a.src = mine.m AND a.type = 'ABOUT' JOIN nodes tp ON tp.id = a.dst AND {visible('tp')}
        JOIN nodes msg ON msg.id = mine.m JOIN source_items si ON si.id = msg.source_item_id
        GROUP BY tp.id, tp.name, tp.type ORDER BY count(*) DESC, tp.id LIMIT 3
    """), {"ids": ids}).mappings():
        out.append(_conn(r, "topic", f"Comes up in {_plural(r['n'], 'message')}", r["sources"]))
    return {"messages": stats["n"], "first": stats["first"], "last": stats["last"], "recent": stats["recent"],
            "sources": list(stats["sources"] or [])}, out


def _member_connections(s: Session, node: dict) -> tuple[dict, list[dict]]:
    """Group-like nodes (conversation, event, organization, project): who and what is in them."""
    nid, ntype = node["id"], node["type"]
    stats: dict = {}
    out: list[dict] = []
    if ntype == "Thread":
        st = s.execute(text(f"""
            SELECT count(*) AS n, min(si.occurred_at) AS first, max(si.occurred_at) AS last,
                   array_agg(DISTINCT si.source) AS sources
            FROM edges it JOIN nodes m ON m.id = it.src AND {visible('m')} JOIN source_items si ON si.id = m.source_item_id
            WHERE it.dst = :n AND it.type = 'IN_THREAD'
        """), {"n": nid}).mappings().one()
        stats = {"messages": st["n"], "first": st["first"], "last": st["last"], "sources": list(st["sources"] or [])}
        rows = s.execute(text(f"""
            SELECT n.id, n.name, n.type, count(*) AS n, max(si.occurred_at) AS last, array_agg(DISTINCT si.source) AS sources
            FROM edges it JOIN nodes m ON m.id = it.src AND {visible('m')}
            JOIN edges x ON x.dst = it.src AND x.type = 'SENT' LEFT JOIN canonical c ON c.node_id = x.src
            JOIN nodes n ON n.id = COALESCE(c.canonical_id, x.src) AND n.type = 'Person' AND {shown('n')}
            JOIN source_items si ON si.id = m.source_item_id
            WHERE it.dst = :n AND it.type = 'IN_THREAD'
            GROUP BY n.id, n.name, n.type ORDER BY count(*) DESC, n.id LIMIT 8
        """), {"n": nid}).mappings().all()
        out += [_conn(r, "person", f"{_plural(r['n'], 'message')} here, last {_month(r['last'])}", r["sources"])
                for r in rows]
    else:
        types = {"Project": ("PART_OF", "INVOLVED_IN"), "Event": ("ATTENDED",),
                 "Organization": ("MEMBER_OF", "WORKED_AT")}.get(ntype, ("PART_OF", "MEMBER_OF", "ATTENDED"))
        rows = s.execute(text(f"""
            SELECT n.id, n.name, n.type, n.props->>'kind' AS kind, count(*) AS n, max(si.occurred_at) AS last,
                   array_agg(DISTINCT si.source) FILTER (WHERE si.source IS NOT NULL) AS sources,
                   CASE n.type WHEN 'Person' THEN 0 WHEN 'Thread' THEN 1 WHEN 'Event' THEN 2 ELSE 3 END AS ord
            FROM edges e LEFT JOIN canonical c ON c.node_id = e.src
            JOIN nodes n ON n.id = COALESCE(c.canonical_id, e.src) AND n.type <> 'Message' AND {shown('n')}
            LEFT JOIN source_items si ON si.id IN (n.source_item_id, e.source_item_id)
            WHERE e.dst = :n AND e.type = ANY(:types)
            GROUP BY n.id, n.name, n.type, n.props->>'kind' ORDER BY ord, count(*) DESC, n.id LIMIT 40
        """), {"n": nid, "types": list(types)}).mappings().all()
        per_kind: dict[str, int] = {}
        for r in rows:
            if per_kind.get(r["type"], 0) >= (8 if r["type"] == "Person" else 3):
                continue
            per_kind[r["type"]] = per_kind.get(r["type"], 0) + 1
            why = {"Person": "Involved", "Thread": "Conversation about it", "Event": "Related event"}.get(r["type"], "Related")
            out.append(_conn(r, r["type"].lower(), why, r["sources"]))
        stats = {"people": sum(1 for c in out if c["type"] == "Person"),
                 "sources": sorted({x for c in out for x in c["sources"]})}
        stats["total_people"] = s.execute(text(f"""
            SELECT count(DISTINCT n.id) FROM edges e LEFT JOIN canonical c ON c.node_id = e.src
            JOIN nodes n ON n.id = COALESCE(c.canonical_id, e.src) AND n.type = 'Person' AND {shown('n')}
            WHERE e.dst = :n AND e.type = ANY(:types)"""), {"n": nid, "types": list(types)}).scalar() or 0
    return stats, out


def _neighbor_connections(s: Session, nid: int) -> tuple[dict, list[dict]]:
    """Anything else (a message, file, place, topic): its direct neighbors that may be shown, people as their heads."""
    rows = s.execute(text(f"""
        SELECT n.id, n.name, n.type, n.props->>'kind' AS kind, count(*) AS n, max(si.occurred_at) AS last,
               array_agg(DISTINCT si.source) FILTER (WHERE si.source IS NOT NULL) AS sources
        FROM (SELECT dst AS o, source_item_id AS sid FROM edges WHERE src = :n
              UNION ALL SELECT src, source_item_id FROM edges WHERE dst = :n) x
        LEFT JOIN canonical c ON c.node_id = x.o JOIN nodes n ON n.id = COALESCE(c.canonical_id, x.o) AND n.id <> :n
             AND {shown('n')}
        LEFT JOIN source_items si ON si.id IN (n.source_item_id, x.sid)
        GROUP BY n.id, n.name, n.type, n.props->>'kind' ORDER BY count(*) DESC, n.id LIMIT 12
    """), {"n": nid}).mappings().all()
    out = [_conn(r, r["type"].lower(), "Connected", r["sources"]) for r in rows]
    return {"sources": sorted({x for c in out for x in c["sources"]})}, out


def gather(s: Session, node_id: int) -> dict:
    """Everything the summary may say, from edges and source items. Raises identity.NotFound."""
    n = identity.node(s, node_id)
    node = {"id": n["id"], "type": n["type"], "name": n["name"], "key": n["key"]}
    props = n["props"] or {}
    if n["type"] == "Project" and props.get("kind") == "role" and props.get("role_of"):
        # A role you had (graph/backbone.py): where, and when.
        node["role_of"] = s.execute(text("SELECT name FROM nodes WHERE id = :o"), {"o": props["role_of"]}).scalar()
        node["start"], node["end"] = props.get("start"), props.get("end")
    if node["type"] == "Person":
        stats, conns = _person_connections(s, identity.members(s, node["id"]))
    elif node["type"] in ("Thread", "Event", "Organization", "Project"):
        stats, conns = _member_connections(s, node)
    else:
        stats, conns = _neighbor_connections(s, node["id"])
    # Dedupe (a thread can surface twice) and keep the strongest few.
    seen: set[int] = set()
    conns = [c for c in conns if not (c["id"] in seen or seen.add(c["id"]))][:MAX_CONNECTIONS]
    named = names(s)
    for c in conns:
        c["last"] = c["last"].isoformat() if isinstance(c["last"], datetime) else c["last"]
        c["name"] = label(c["name"], c["type"], named)
    node["name"] = label(node["name"], node["type"], named)
    for k in ("first", "last"):
        if isinstance(stats.get(k), datetime):
            stats[k] = stats[k].isoformat()
    return {"node": node, "stats": stats, "connections": conns}


def snippets(s: Session, node_id: int, sources: frozenset[str], limit: int = 4) -> list[dict]:
    """A few recent subject lines of the node's visible records, from opted-in sources only: the only record text a
    prompt may carry."""
    if not sources:
        return []
    rows = s.execute(text(f"""
        SELECT DISTINCT si.id, si.source, si.occurred_at, left(si.title, 100) AS title FROM (
            SELECT dst AS m FROM edges WHERE src = ANY(:ids) AND type IN ('SENT', 'ATTENDED')
            UNION SELECT src FROM edges WHERE dst = ANY(:ids) AND type = 'RECEIVED'
            UNION SELECT it.src FROM edges it WHERE it.dst = ANY(:ids) AND it.type = 'IN_THREAD') mine
        JOIN nodes n ON n.id = mine.m AND {visible('n')} JOIN source_items si ON si.id = n.source_item_id
        WHERE si.source = ANY(CAST(:sources AS text[])) AND COALESCE(si.title, '') <> ''
        ORDER BY si.occurred_at DESC NULLS LAST, si.id DESC LIMIT :k
    """), {"ids": identity.members(s, node_id), "sources": sorted(sources), "k": limit}).mappings().all()
    return [dict(r) for r in rows]


def _allowed(conn: dict, sources: frozenset[str], visible_ids: set[int]) -> bool:
    """A connection the prompt may name: the policy lets it be named, and every source behind it is opted in."""
    return conn["id"] in visible_ids and bool(conn["sources"]) and all(x in sources for x in conn["sources"])


def prompt_for(facts: dict, sources: frozenset[str], snips: list[dict], visible_ids: set[int]) -> tuple[str, list[int]]:
    """The user prompt, plus the source items it drew on. `visible_ids` are the nodes the model policy lets it name
    (llm.visible_nodes); every other connection is a count."""
    node, stats = facts["node"], facts["stats"]
    lines = [f"Item: {node['name']} ({LABEL.get(node['type'], node['type'].lower())})"]
    if stats.get("messages"):
        lines.append(f"Messages: {stats['messages']:,}, from {_month(stats.get('first'))} to {_month(stats.get('last'))}"
                     f"; {stats.get('recent', 0)} in the last 30 days")
    if stats.get("total_people"):
        lines.append(f"People involved: {stats['total_people']}")
    named = [c for c in facts["connections"] if _allowed(c, sources, visible_ids)]
    counted = [c for c in facts["connections"] if not _allowed(c, sources, visible_ids)]
    for c in named:
        lines.append(f"- {c['type']} {c['name']}: {c['why']}")
    by_type: dict[str, int] = {}
    for c in counted:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
    for t, n in by_type.items():
        lines.append(f"- {n} other {t.lower()} connection{'s' if n != 1 else ''} (details withheld)")
    if snips:
        lines.append("Recent subjects:")
        lines += [f"- {_month(x['occurred_at'])}: {x['title']}" for x in snips]
    return "\n".join(lines), [x["id"] for x in snips]


def fallback(facts: dict) -> str:
    """Deterministic plain-language summary from the same facts, used whenever the model can't be."""
    node, stats, conns = facts["node"], facts["stats"], facts["connections"]
    name, kind = node["name"], node["type"]
    by = lambda k: [c for c in conns if c["kind"] == k or c["type"] == k.title()]  # noqa: E731
    people, threads = by("person"), by("thread")
    parts: list[str] = []
    if kind == "Person":
        if stats.get("messages"):
            parts.append(f"{name} appears in {_plural(stats['messages'], 'message')}, "
                         f"from {_month(stats.get('first'))} to {_month(stats.get('last'))}.")
        else:
            parts.append(f"{name} has no messages in the graph yet.")
        if people:
            top = ", ".join(f"{c['name']} ({c['count']:,})" for c in people[:3])
            parts.append(f"They talk most with {top}.")
        if threads:
            parts.append(f"Their busiest group conversation is \"{threads[0]['name']}\".")
        orgs = by("org") or by("organization")
        if orgs:
            parts.append(f"They are associated with {orgs[0]['name']}.")
    elif kind == "Thread":
        parts.append(f"\"{name}\" is a conversation of {_plural(stats.get('messages', 0), 'message')}"
                     + (f", from {_month(stats.get('first'))} to {_month(stats.get('last'))}." if stats.get("first") else "."))
        if people:
            parts.append(f"Most active: {', '.join(c['name'] for c in people[:3])}.")
    elif kind == "Project" and node.get("role_of"):
        span = (f"{_month(node['start'])} to {_month(node['end'])}" if node.get("start") and node.get("end")
                else f"since {_month(node['start'])}" if node.get("start") else "")
        parts.append(f"{name} is your role at {node['role_of']}" + (f", {span}." if span else "."))
        if conns:
            parts.append(f"Under it: {', '.join(c['name'] for c in conns[:3])}.")
    elif kind in ("Event", "Organization", "Project"):
        label_ = {"Event": "an event", "Organization": "an organization", "Project": "a project"}[kind]
        n = stats.get("total_people") or len(people)
        parts.append(f"{name} is {label_}" + (f" with {_plural(n, 'person', 'people')} linked to it." if n else "."))
        if people:
            parts.append(f"The most involved are {', '.join(c['name'] for c in people[:3])}.")
        if threads:
            parts.append(f"It shows up in conversations such as \"{threads[0]['name']}\".")
    else:
        parts.append(f"{name} is a {kind.lower()} in your graph.")
        if conns:
            parts.append(f"It is connected to {', '.join(c['name'] for c in conns[:3])}.")
    if len(parts) == 1 and not conns:
        parts.append("Nothing else is connected to it yet.")
    return " ".join(parts)


def _model_summary(system: str, user: str) -> str:
    from igneous.llm import structured
    out, _ = structured(system, user, SCHEMA, max_tokens=500, model=settings.llm_cheap_model)
    return " ".join(str(out["summary"]).split())


def summarize(s: Session, node_id: int, model=_model_summary) -> dict:
    """The endpoint body: {summary, highlights, sources, generated_by}. `model(system, user) -> str` is injectable for
    tests. Raises identity.NotFound."""
    facts = gather(s, node_id)
    from igneous.llm import allowed_sources, visible_nodes
    sources = allowed_sources()
    highlights = [{k: c[k] for k in ("id", "name", "type", "category", "why")} for c in facts["connections"]]
    body = {"summary": "", "highlights": highlights, "sources": [], "generated_by": "fallback"}
    node_id = facts["node"]["id"]
    may_name = visible_nodes(s, [node_id, *(c["id"] for c in facts["connections"])], sources) \
        if settings.llm_enabled and sources else set()
    # The prompt names the node itself, so the policy must let it be named, and it must have something to say.
    if node_id in may_name and any(_allowed(c, sources, may_name) for c in facts["connections"]):
        from igneous.llm import cache_lookup, cache_store

        user, used = prompt_for(facts, sources, snippets(s, node_id, sources), may_name)
        key = f"node-summary:{PROMPT_VERSION}:{node_id}:" + hashlib.sha256((SYSTEM + user).encode()).hexdigest()
        try:
            cached = cache_lookup(key)
            if cached is None:
                cached = {"summary": model(SYSTEM, user), "sources": used}
                if cached["summary"]:
                    try:
                        cache_store(key, cached)
                    except Exception as e:  # an answer we can't cache is still the answer
                        log.info("summary for node %s not cached: %s", node_id, type(e).__name__)
            if cached.get("summary"):
                return {**body, "summary": cached["summary"], "sources": cached.get("sources", []),
                        "generated_by": "model"}
        except Exception as e:  # LLMDisabled, provider errors, cache problems: never break the inspector
            log.info("summary for node %s fell back: %s: %s", node_id, type(e).__name__, str(e)[:120])
    body["summary"] = fallback(facts)
    return body
