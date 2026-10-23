"""Plain-language summary of how one node connects to the rest of the graph (`GET /graph/node/{id}/summary`).

Grounding: the facts are gathered from edges and source items only. The model prompt carries node names, edge
types/counts, dates, and short subject-line snippets, and only for sources opted in with IGNEOUS_LLM_SOURCES
(#112). Connections that rest on any other source are reduced to counts. Model summaries are cached in
`llm_cache`, keyed by node and a hash of the exact prompt, so they refresh when the node's edges change. With the
model off, unconfigured, or failing, a deterministic summary built from the same facts is returned instead.
"""
import hashlib
import json
import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings
from igneous.graph.traversal import CATEGORY, _canon_head, _my_ids, handle_names, pretty_thread

log = logging.getLogger(__name__)
PROMPT_VERSION = "v1"
MAX_CONNECTIONS = 14
NO_SUBJECT = ("(no subject)", "")
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"],
          "additionalProperties": False}
SYSTEM = (
    "You write a short note, 2 to 4 plain sentences, about how one item in someone's personal knowledge graph "
    "connects to the rest of it. Use only the facts provided. Refer to people and things by name, never by id or "
    "number code. Do not speculate about motives or content you were not given. No markdown, no lists.")

LABEL = {"Thread": "conversation", "Organization": "organization", "Person": "person"}


def _month(d: datetime | str | None) -> str:
    if d is None:
        return ""
    if isinstance(d, str):
        d = datetime.fromisoformat(d)
    return d.strftime("%b %Y")


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _conn(row, kind: str, why: str, sources) -> dict:
    return {"id": row["id"], "name": row["name"], "type": row["type"], "category": _category(row["type"]),
            "count": int(row.get("n") or 0), "last": row.get("last"), "kind": kind, "why": why,
            "sources": sorted(s for s in (sources or []) if s)}


def _category(node_type: str) -> str | None:
    return CATEGORY.get("Group" if node_type == "Thread" else node_type)


def _person_connections(s: Session, ids: list[int], names: dict[str, str]) -> tuple[dict, list[dict]]:
    mine = """
        WITH mine AS (SELECT dst AS m FROM edges WHERE src = ANY(:ids) AND type = 'SENT'
                      UNION SELECT src FROM edges WHERE dst = ANY(:ids) AND type = 'RECEIVED')
    """
    stats = s.execute(text(mine + """
        SELECT count(*) AS n, min(si.occurred_at) AS first, max(si.occurred_at) AS last,
               count(*) FILTER (WHERE si.occurred_at > now() - interval '30 days') AS recent,
               array_agg(DISTINCT si.source) AS sources
        FROM mine JOIN nodes m ON m.id = mine.m JOIN source_items si ON si.id = m.source_item_id
    """), {"ids": ids}).mappings().one()
    out: list[dict] = []
    for r in s.execute(text(mine + """
        , pairs AS (SELECT COALESCE(c.canonical_id, p.p) AS id, mine.m FROM mine
                   JOIN (SELECT src AS p, dst AS m FROM edges WHERE type = 'SENT'
                         UNION ALL SELECT dst, src FROM edges WHERE type = 'RECEIVED') p ON p.m = mine.m
                   LEFT JOIN canonical c ON c.node_id = p.p)
        SELECT n.id, n.name, n.type, count(DISTINCT pairs.m) AS n, max(si.occurred_at) AS last,
               array_agg(DISTINCT si.source) AS sources
        FROM pairs JOIN nodes n ON n.id = pairs.id AND n.type = 'Person'
        JOIN nodes msg ON msg.id = pairs.m JOIN source_items si ON si.id = msg.source_item_id
        WHERE pairs.id <> ALL(:ids) AND COALESCE(n.props->>'kind', '') <> 'service'
        GROUP BY n.id, n.name, n.type ORDER BY count(DISTINCT pairs.m) DESC LIMIT 8
    """), {"ids": ids}).mappings():
        r = dict(r)
        out.append(_conn(r, "person", f"{_plural(r['n'], 'message')} together, last {_month(r['last'])}", r["sources"]))
    for r in s.execute(text(mine + """
        SELECT t.id, t.name, t.type, count(*) AS n, max(si.occurred_at) AS last, array_agg(DISTINCT si.source) AS sources
        FROM mine JOIN edges it ON it.src = mine.m AND it.type = 'IN_THREAD' JOIN nodes t ON t.id = it.dst
        JOIN nodes msg ON msg.id = mine.m JOIN source_items si ON si.id = msg.source_item_id
        WHERE t.name NOT IN ('(no subject)', '') GROUP BY t.id, t.name, t.type
        ORDER BY count(*) DESC LIMIT 4
    """), {"ids": ids}).mappings():
        r = dict(r)
        r["name"] = pretty_thread(r["name"], names) or r["name"]
        out.append(_conn(r, "thread", f"{_plural(r['n'], 'message')} in it, last {_month(r['last'])}", r["sources"]))
    node_sources = list(stats["sources"] or [])
    for r in s.execute(text("""
        SELECT o.id, o.name, o.type FROM edges e JOIN nodes o ON o.id = e.dst AND o.type = 'Organization'
        WHERE e.src = ANY(:ids) AND e.type IN ('MEMBER_OF', 'WORKED_AT')
          AND o.key NOT IN ('gmail.com','icloud.com','yahoo.com','hotmail.com','outlook.com','me.com','aol.com')
        LIMIT 3
    """), {"ids": ids}).mappings():
        out.append(_conn(dict(r), "org", "Their email domain", node_sources))
    for r in s.execute(text("""
        SELECT ev.id, ev.name, ev.type, si.occurred_at AS last, array_agg(DISTINCT si.source) AS sources
        FROM edges e JOIN nodes ev ON ev.id = e.dst AND ev.type = 'Event'
        LEFT JOIN source_items si ON si.id = ev.source_item_id
        WHERE e.src = ANY(:ids) AND e.type = 'ATTENDED' GROUP BY ev.id, ev.name, ev.type, si.occurred_at
        ORDER BY si.occurred_at DESC NULLS LAST LIMIT 3
    """), {"ids": ids}).mappings():
        out.append(_conn(dict(r), "event", f"Attended{', ' + _month(r['last']) if r['last'] else ''}", r["sources"]))
    for r in s.execute(text(mine + """
        SELECT tp.id, tp.name, tp.type, count(*) AS n FROM mine JOIN edges a ON a.src = mine.m AND a.type = 'ABOUT'
        JOIN nodes tp ON tp.id = a.dst GROUP BY tp.id, tp.name, tp.type ORDER BY count(*) DESC LIMIT 3
    """), {"ids": ids}).mappings():
        out.append(_conn(dict(r), "topic", f"Comes up in {_plural(r['n'], 'message')}", node_sources))
    return {"messages": stats["n"], "first": stats["first"], "last": stats["last"], "recent": stats["recent"],
            "sources": node_sources}, out


def _member_connections(s: Session, node: dict, names: dict[str, str]) -> tuple[dict, list[dict]]:
    """Group-like nodes (conversation, event, organization, project): who and what is in them."""
    nid, ntype = node["id"], node["type"]
    stats: dict = {}
    out: list[dict] = []
    if ntype == "Thread":
        st = s.execute(text("""
            SELECT count(*) AS n, min(si.occurred_at) AS first, max(si.occurred_at) AS last,
                   array_agg(DISTINCT si.source) AS sources
            FROM edges it JOIN nodes m ON m.id = it.src JOIN source_items si ON si.id = m.source_item_id
            WHERE it.dst = :n AND it.type = 'IN_THREAD'
        """), {"n": nid}).mappings().one()
        stats = {"messages": st["n"], "first": st["first"], "last": st["last"], "sources": list(st["sources"] or [])}
        rows = s.execute(text("""
            SELECT n.id, n.name, n.type, count(*) AS n, max(si.occurred_at) AS last, array_agg(DISTINCT si.source) AS sources
            FROM edges it JOIN edges x ON x.dst = it.src AND x.type = 'SENT'
            JOIN nodes n ON n.id = COALESCE((SELECT canonical_id FROM canonical WHERE node_id = x.src), x.src)
                        AND n.type = 'Person'
            JOIN nodes m ON m.id = it.src JOIN source_items si ON si.id = m.source_item_id
            WHERE it.dst = :n AND it.type = 'IN_THREAD' AND COALESCE(n.props->>'kind', '') <> 'service'
            GROUP BY n.id, n.name, n.type ORDER BY count(*) DESC LIMIT 8
        """), {"n": nid}).mappings().all()
        out += [_conn(dict(r), "person", f"{_plural(r['n'], 'message')} here, last {_month(r['last'])}", r["sources"])
                for r in rows]
    else:
        types = {"Project": ("PART_OF", "INVOLVED_IN"), "Event": ("ATTENDED",),
                 "Organization": ("MEMBER_OF", "WORKED_AT")}.get(ntype, ("PART_OF", "MEMBER_OF", "ATTENDED"))
        rows = s.execute(text("""
            SELECT n.id, n.name, n.type, count(*) AS n, max(si.occurred_at) AS last,
                   array_agg(DISTINCT si.source) AS sources,
                   CASE n.type WHEN 'Person' THEN 0 WHEN 'Thread' THEN 1 WHEN 'Event' THEN 2 ELSE 3 END AS ord
            FROM edges e JOIN nodes n ON n.id = COALESCE((SELECT canonical_id FROM canonical WHERE node_id = e.src), e.src)
            LEFT JOIN source_items si ON si.id = n.source_item_id
            WHERE e.dst = :n AND e.type = ANY(:types) AND n.type <> 'Message'
              AND COALESCE(n.props->>'kind', '') <> 'service'
            GROUP BY n.id, n.name, n.type ORDER BY ord, (SELECT count(*) FROM edges x WHERE x.src = n.id) DESC LIMIT 40
        """), {"n": nid, "types": list(types)}).mappings().all()
        per_kind: dict[str, int] = {}
        for r in rows:
            r = dict(r)
            if per_kind.get(r["type"], 0) >= (8 if r["type"] == "Person" else 3):
                continue
            per_kind[r["type"]] = per_kind.get(r["type"], 0) + 1
            if r["type"] == "Thread":
                r["name"] = pretty_thread(r["name"], names) or r["name"]
            why = {"Person": "Involved", "Thread": "Conversation about it", "Event": "Related event"}.get(r["type"], "Related")
            out.append(_conn(r, r["type"].lower(), why, r["sources"]))
        stats = {"people": sum(1 for c in out if c["type"] == "Person"),
                 "sources": sorted({x for c in out for x in c["sources"]})}
        stats["total_people"] = s.execute(text(
            "SELECT count(DISTINCT src) FROM edges e JOIN nodes n ON n.id = e.src AND n.type = 'Person' "
            "WHERE e.dst = :n AND e.type = ANY(:types)"), {"n": nid, "types": list(types)}).scalar() or 0
    return stats, out


def _neighbor_connections(s: Session, ids: list[int], names: dict[str, str]) -> tuple[dict, list[dict]]:
    """Anything else (a message, file, place, topic): its direct neighbors."""
    rows = s.execute(text("""
        SELECT n.id, n.name, n.type, count(*) AS n, max(si.occurred_at) AS last, array_agg(DISTINCT si.source) AS sources
        FROM (SELECT dst AS o FROM edges WHERE src = ANY(:ids) UNION ALL SELECT src FROM edges WHERE dst = ANY(:ids)) x
        JOIN nodes n ON n.id = x.o AND n.id <> ALL(:ids) LEFT JOIN source_items si ON si.id = n.source_item_id
        GROUP BY n.id, n.name, n.type ORDER BY count(*) DESC LIMIT 12
    """), {"ids": ids}).mappings().all()
    out = []
    for r in rows:
        r = dict(r)
        if r["type"] == "Thread":
            r["name"] = pretty_thread(r["name"], names) or r["name"]
        out.append(_conn(r, r["type"].lower(), "Connected", r["sources"]))
    return {"sources": sorted({x for c in out for x in c["sources"]})}, out


def gather(s: Session, node_id: int) -> dict | None:
    """Everything the summary may say, from edges and source items. None if the node doesn't exist."""
    node = s.execute(text("SELECT id, type, name, key FROM nodes WHERE id = :i"), {"i": node_id}).mappings().first()
    if node is None:
        return None
    node = dict(node)
    names = handle_names(s)
    if node["type"] == "Person":
        ids = _my_ids(s, _canon_head(s, node_id))
        stats, conns = _person_connections(s, ids, names)
    elif node["type"] in ("Thread", "Event", "Organization", "Project"):
        stats, conns = _member_connections(s, node, names)
    else:
        stats, conns = _neighbor_connections(s, [node_id], names)
    # Dedupe (a thread can surface twice) and keep the strongest few.
    seen: set[int] = set()
    conns = [c for c in conns if not (c["id"] in seen or seen.add(c["id"]))][:MAX_CONNECTIONS]
    for c in conns:
        c["last"] = c["last"].isoformat() if isinstance(c["last"], datetime) else c["last"]
    node["name"] = pretty_thread(node["name"], names) or node["name"] if node["type"] == "Thread" else node["name"]
    for k in ("first", "last"):
        if isinstance(stats.get(k), datetime):
            stats[k] = stats[k].isoformat()
    return {"node": node, "stats": stats, "connections": conns}


def snippets(s: Session, node_id: int, sources: frozenset[str], limit: int = 4) -> list[dict]:
    """A few recent subject lines from opted-in sources only: the only item text a prompt may carry."""
    if not sources:
        return []
    ids = _my_ids(s, _canon_head(s, node_id))
    rows = s.execute(text("""
        SELECT DISTINCT si.id, si.source, si.occurred_at, left(si.title, 100) AS title FROM (
            SELECT dst AS m FROM edges WHERE src = ANY(:ids) AND type IN ('SENT', 'ATTENDED')
            UNION SELECT src FROM edges WHERE dst = ANY(:ids) AND type = 'RECEIVED'
            UNION SELECT it.src FROM edges it WHERE it.dst = ANY(:ids) AND it.type = 'IN_THREAD') mine
        JOIN nodes n ON n.id = mine.m JOIN source_items si ON si.id = n.source_item_id
        WHERE si.source = ANY(CAST(:sources AS text[])) AND COALESCE(si.title, '') <> ''
        ORDER BY si.occurred_at DESC NULLS LAST LIMIT :k
    """), {"ids": ids, "sources": sorted(sources), "k": limit}).mappings().all()
    return [dict(r) for r in rows]


def _allowed(conn: dict, sources: frozenset[str]) -> bool:
    return bool(conn["sources"]) and all(x in sources for x in conn["sources"])


def prompt_for(facts: dict, sources: frozenset[str], snips: list[dict]) -> tuple[str, list[int]]:
    """The user prompt, plus the source items it drew on. Connections resting on non-opted-in sources are counts."""
    node, stats = facts["node"], facts["stats"]
    lines = [f"Item: {node['name']} ({LABEL.get(node['type'], node['type'].lower())})"]
    if stats.get("messages"):
        lines.append(f"Messages: {stats['messages']:,}, from {_month(stats.get('first'))} to {_month(stats.get('last'))}"
                     f"; {stats.get('recent', 0)} in the last 30 days")
    if stats.get("total_people"):
        lines.append(f"People involved: {stats['total_people']}")
    shown = [c for c in facts["connections"] if _allowed(c, sources)]
    hidden = [c for c in facts["connections"] if not _allowed(c, sources)]
    for c in shown:
        lines.append(f"- {c['type']} {c['name']}: {c['why']}")
    by_type: dict[str, int] = {}
    for c in hidden:
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
            parts.append(f"Their busiest conversation is \"{threads[0]['name']}\".")
        orgs = by("org") or by("organization")
        if orgs:
            parts.append(f"They are associated with {orgs[0]['name']}.")
    elif kind == "Thread":
        parts.append(f"\"{name}\" is a conversation of {_plural(stats.get('messages', 0), 'message')}"
                     + (f", from {_month(stats.get('first'))} to {_month(stats.get('last'))}." if stats.get("first") else "."))
        if people:
            parts.append(f"Most active: {', '.join(c['name'] for c in people[:3])}.")
    elif kind in ("Event", "Organization", "Project"):
        label = {"Event": "an event", "Organization": "an organization", "Project": "a project"}[kind]
        n = stats.get("total_people") or len(people)
        parts.append(f"{name} is {label}" + (f" with {_plural(n, 'person', 'people')} linked to it." if n else "."))
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


def summarize(s: Session, node_id: int, model=_model_summary) -> dict | None:
    """The endpoint body. `model(system, user) -> str` is injectable for tests."""
    facts = gather(s, node_id)
    if facts is None:
        return None
    from igneous.llm import allowed_sources
    sources = allowed_sources()
    highlights = [{k: c[k] for k in ("id", "name", "type", "category", "why")} for c in facts["connections"]]
    body = {"summary": "", "highlights": highlights, "sources": [], "generated_by": "fallback"}
    use_model = settings.llm_enabled and any(_allowed(c, sources) for c in facts["connections"])
    if use_model:
        user, used = prompt_for(facts, sources, snippets(s, node_id, sources))
        key = f"node-summary:{PROMPT_VERSION}:{node_id}:" + hashlib.sha256((SYSTEM + user).encode()).hexdigest()
        try:
            cached = s.execute(text("SELECT response FROM llm_cache WHERE cache_key = :k"), {"k": key}).scalar()
            if cached is None:
                answer = model(SYSTEM, user)
                if answer:
                    s.execute(text("INSERT INTO llm_cache (cache_key, response) VALUES (:k, CAST(:r AS jsonb)) "
                                   "ON CONFLICT (cache_key) DO NOTHING"),
                              {"k": key, "r": json.dumps({"summary": answer, "sources": used})})
                    s.commit()
                cached = {"summary": answer, "sources": used}
            if cached.get("summary"):
                return {**body, "summary": cached["summary"], "sources": cached.get("sources", []),
                        "generated_by": "model"}
        except Exception as e:  # LLMDisabled, provider errors, cache problems: never break the inspector
            log.info("summary for node %s fell back: %s: %s", node_id, type(e).__name__, str(e)[:120])
            s.rollback()
    body["summary"] = fallback(facts)
    return body
