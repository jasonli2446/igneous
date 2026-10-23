"""MCP server: the graph as tools for Claude Code / Claude Desktop / Cursor (stdio). Read-only by design (CO-2).

    uv run igneous mcp          # stdio; see README for the client config

Graph tools (me, find, open_node, why_connected, sources, read_item, and the lower-level get_node, neighbors,
correspondents, evidence) let an agent explore; `search` and `ask` read the text, and `ask` runs the server-side
retrieval pipeline (graph expansion + cite-or-abstain) so it doesn't spend turns redoing it. Every result
carries node / chunk ids so the agent can cite. Tool results go to whatever model the MCP client uses:
that's an outbound path for user content (CO-1), the same as a configured model provider.
"""
from mcp.server.mcpserver import MCPServer
from sqlalchemy import text

from igneous.db import SessionLocal

# The model client, search and embedding stacks load on first use, not at import: an MCP client connects only
# once the server answers `initialize`, and a slow start makes the first tool call race the connection.

server = MCPServer(
    "igneous",
    instructions=(
        "A personal knowledge graph of the user's life, built from their email, texts, calendar, Drive, GitHub, "
        "files and AI chats: people (one node per human, all addresses folded in), group chats, and inferred "
        "life threads (trips, classes, jobs, projects) under the organizations and schools on their resume. "
        "How to use it: `me` for the big picture; `find` to turn a name into a node_id; `open_node` for what "
        "something is made of (a trip's travellers and group chat, an org's projects, a person's world); "
        "`why_connected` to explain a link with the records behind it; `sources` and `read_item` to read those "
        "records. Questions about structure (who went, who's on it, what's under it, how are they linked) are "
        "answered by the graph tools directly, usually in 2-4 calls. Use `search` or `ask` only for what was "
        "said (a decision, a date, a detail in the text), not for facts the graph tools already returned. "
        "Cite node_ids and item_ids. Everything is read-only."
    ),
)


def _clip(t: str | None, n: int = 600) -> str:
    return (t or "")[:n]


# What you wrote and talked about, not what you browsed: page visits and search history only on request.
CONTENT_SOURCES = ("gmail", "email", "imessage", "gcal", "drive", "fs", "notes", "ai_chats", "github",
                   "takeout_chat", "takeout_tasks", "takeout_meet", "linkedin")


@server.tool()
def search(query: str, person_node_id: int | None = None, include_browsing: bool = False, k: int = 8) -> list[dict]:
    """Hybrid keyword + semantic search over every record (email, texts, calendar, files, GitHub, AI chats).
    `person_node_id` restricts to records that person sent or received (all their addresses). Browser and search
    history are skipped unless `include_browsing`. Returns item ids to cite, with a title, date and excerpt."""
    from igneous.rag.constraints import apply
    from igneous.rag.search import Filters, hybrid
    with SessionLocal() as s:
        f = Filters(person_id=person_node_id, sources=() if include_browsing else CONTENT_SOURCES)
        hits = hybrid(s, query, apply(s, query, f), k=min(k, 20))
    return [{"item_id": h.get("source_item_id"), "chunk_id": h["chunk_id"], "node_id": h["node_id"], "title": h["title"], "source": h["source"],
             "date": h["occurred_at"].isoformat() if h["occurred_at"] else None, "excerpt": _clip(h["text"])}
            for h in hits]


def find_entity(name: str, type: str | None = None) -> list[dict]:  # superseded by `find`; kept for callers
    """Find people, organizations, threads, or topics by name. Use the returned node_id with other tools."""
    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT DISTINCT ON (COALESCE(c.canonical_id, n.id)) COALESCE(c.canonical_id, n.id) AS node_id,
                   n.type, n.name
            FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id
            WHERE n.name ILIKE :q AND n.type <> 'Message' AND (CAST(:t AS text) IS NULL OR n.type = :t)
            LIMIT 15
        """), {"q": f"%{name}%", "t": type}).mappings().all()
    return [dict(r) for r in rows]


@server.tool()
def get_node(node_id: int) -> dict:
    """A node's properties, its connection counts by edge type, and (for messages/files) the source text."""
    with SessionLocal() as s:
        n = s.execute(text("SELECT id, type, name, key, source_item_id FROM nodes WHERE id = :id"),
                      {"id": node_id}).mappings().first()
        if not n:
            return {"error": f"no node {node_id}"}
        degree = dict(s.execute(text("SELECT type, count(*) FROM edges WHERE src = :id OR dst = :id GROUP BY 1"),
                                {"id": node_id}).all())
        src = None
        if n["source_item_id"]:
            src = s.execute(text("SELECT title, occurred_at, body_text FROM source_items WHERE id = :id"),
                            {"id": n["source_item_id"]}).mappings().first()
    return {"node_id": n["id"], "type": n["type"], "name": n["name"], "key": n["key"], "connections": degree,
            "source": {"title": src["title"], "date": src["occurred_at"].isoformat() if src["occurred_at"] else None,
                       "text": _clip(src["body_text"], 3000)} if src else None}


@server.tool()
def correspondents(person_node_id: int, limit: int = 20) -> list[dict]:
    """A person's real relationships, closest first: two-way, weighted toward recent contact, one-on-one and
    small-group talk counting most (a big group chat says little about any one member). Automated senders excluded."""
    from igneous.graph.traversal import _canon_head, relationships
    with SessionLocal() as s:
        head = _canon_head(s, person_node_id)
        rel = relationships(s, head, min(limit, 60))
        names = dict(s.execute(text("SELECT id, name FROM nodes WHERE id = ANY(:i)"), {"i": [p for p, _, _ in rel]}).all())
    return [{"node_id": p, "name": names.get(p), "closeness": st, "messages": r["msgs"], "weeks_in_touch": r["weeks"],
             "last": r["last_at"].date().isoformat() if r.get("last_at") else None} for p, st, r in rel]


@server.tool()
def neighbors(node_id: int, edge_type: str | None = None, limit: int = 25) -> list[dict]:
    """Directly connected nodes and the edge connecting them (e.g. a message's sender, recipients, thread)."""
    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT e.type AS edge, CASE WHEN e.src = :id THEN 'out' ELSE 'in' END AS direction,
                   n.id AS node_id, n.type, n.name
            FROM edges e JOIN nodes n ON n.id = CASE WHEN e.src = :id THEN e.dst ELSE e.src END
            WHERE (e.src = :id OR e.dst = :id) AND (CAST(:t AS text) IS NULL OR e.type = :t)
            LIMIT :lim
        """), {"id": node_id, "t": edge_type, "lim": min(limit, 100)}).mappings().all()
    return [dict(r) for r in rows]


@server.tool()
def evidence(node_id: int, limit: int = 10) -> list[dict]:
    """Model-derived facts about a node (mentions, topics), each with the exact quoted source span: the
    answer to "why does the graph believe this"."""
    from igneous.main import cluster  # same cluster expansion the web inspector uses

    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT e.type, e.confidence, e.props->>'quote' AS quote, e.props->>'role' AS role,
                   si.title, si.occurred_at,
                   (SELECT name FROM nodes WHERE id = CASE WHEN e.src = ANY(:ids) THEN e.dst ELSE e.src END) AS other
            FROM edges e JOIN source_items si ON si.id = e.source_item_id
            WHERE (e.src = ANY(:ids) OR e.dst = ANY(:ids)) AND e.extractor <> 'spine' AND e.span_start IS NOT NULL
            ORDER BY si.occurred_at DESC NULLS LAST LIMIT :lim
        """), {"ids": cluster(s, node_id), "lim": min(limit, 50)}).mappings().all()
    return [{**dict(r), "date": r["occurred_at"].isoformat() if r["occurred_at"] else None} for r in rows]


@server.tool()
def ask(question: str) -> dict:
    """Answer a question from the archive with citations, or say it can't be answered. Runs hybrid search,
    graph expansion, and a cite-or-abstain answer step server-side."""
    from igneous.llm import LLMDisabled
    from igneous.rag import qa

    try:
        with SessionLocal() as s:
            r = qa.answer(s, question)
    except LLMDisabled as e:
        # Usually the MCP client didn't pass AWS_BEARER_TOKEN_BEDROCK through (see README). The other tools
        # don't need the model, so say so instead of failing opaquely.
        return {"error": f"ask is unavailable: {e}. Use search/evidence instead, or pass the model "
                         "credentials in the MCP server's env."}
    return {"answerable": r["answerable"], "answer": r["answer"],
            "citations": [{"chunk_id": c["chunk_id"], "title": c["title"],
                           "date": c["occurred_at"].isoformat() if c["occurred_at"] else None} for c in r["citations"]]}


_VIEW: dict = {}  # the big picture per (owner, graph version): it only changes when the graph does


def _node_brief(s, ids: list[int]) -> list[dict]:
    """id, type, name and the one-line context (dates, size, kind) for each node, in the given order."""
    rows = {r["id"]: r for r in s.execute(text("""
        SELECT id, type, name, props->>'kind' AS kind, props->>'start' AS start, props->>'end' AS end,
               props->>'records' AS records, COALESCE((props->>'group')::bool, false) AS grp
        FROM nodes WHERE id = ANY(:i)"""), {"i": ids}).mappings()}
    from igneous.graph.traversal import handle_names, pretty_thread
    named = handle_names(s) if any(r["type"] == "Thread" for r in rows.values()) else {}
    out = []
    for i in ids:
        r = rows.get(i)
        if not r:
            continue
        name = (pretty_thread(r["name"], named) or r["name"]) if r["type"] == "Thread" else r["name"]
        b = {"node_id": r["id"], "type": "Group chat" if r["type"] == "Thread" and r["grp"] else r["type"], "name": name}
        if r["kind"]:
            b["kind"] = r["kind"]
        if r["start"]:
            b["when"] = f"{r['start'][:10]} to {(r['end'] or '')[:10]}"
        if r["records"]:
            b["records"] = int(r["records"])
        out.append(b)
    return out


@server.tool()
def me() -> dict:
    """The big picture: who the user is and the most meaningful parts of their life right now (where they work
    and study, their sustained projects, trips and classes, and their closest people, recency-weighted). The
    same view the explorer opens on. Start here."""
    from igneous.graph.traversal import view
    with SessionLocal() as s:
        seed = s.execute(text("SELECT id FROM nodes WHERE type = 'Person' AND props->>'self' = 'true' LIMIT 1")).scalar()
        if seed is None:
            return {"error": "no owner identified yet: run the pipeline"}
        from igneous.graph import dirty
        key = (seed, dirty.version(s))
        g = _VIEW.get(key) or _VIEW.setdefault(key, view(s, seed))
        head = g["seed"]
        name = s.execute(text("SELECT name FROM nodes WHERE id = :i"), {"i": head}).scalar()
        cat = {n["id"]: n.get("category") for n in g["nodes"]}
        ring = [n["id"] for n in g["nodes"] if n["id"] != head]
        work = s.execute(text("""
            SELECT o.name, e.props->>'title' AS title, e.valid_from::date AS start, e.valid_to::date AS "end",
                   (e.props->>'school') IS NOT NULL AS school
            FROM edges e JOIN nodes o ON o.id = e.dst
            WHERE e.src = :me AND e.type = 'WORKED_AT' AND e.extractor IN ('profile', 'user')
            ORDER BY e.valid_from DESC NULLS LAST"""), {"me": head}).mappings().all()
        brief = _node_brief(s, ring)
    for b in brief:
        b["category"] = cat.get(b["node_id"])
    return {"node_id": head, "name": name,
            "history": [{**dict(w), "start": str(w["start"]) if w["start"] else None,
                         "end": str(w["end"]) if w["end"] else "present"} for w in work],
            "closest": brief}


@server.tool()
def find(name: str) -> list[dict]:
    """Resolve a name to graph nodes, best match first: people (all their addresses and numbers), projects,
    trips, classes, organizations, group chats, events and files. Ranked by match and by closeness to the user."""
    from igneous.graph.find import find as _find
    with SessionLocal() as s:
        seed = s.execute(text("SELECT id FROM nodes WHERE type = 'Person' AND props->>'self' = 'true' LIMIT 1")).scalar()
        hits = _find(s, name, seed)
        return [{"node_id": h["id"], "type": h["type"], "name": h["name"], "context": h.get("context")}
                for h in hits[:12]]


@server.tool()
def open_node(node_id: int) -> dict:
    """What a node is made of, the way the explorer shows it when clicked: an organization's projects and
    people, a project's or trip's participants, group chats and key documents, a person's closest people, group
    chats, projects and events. Includes a short summary and the node's own facts."""
    from igneous.graph.summary import summarize
    from igneous.graph.traversal import expand
    with SessionLocal() as s:
        seed = s.execute(text("SELECT id FROM nodes WHERE type = 'Person' AND props->>'self' = 'true' LIMIT 1")).scalar()
        n = s.execute(text("SELECT id, type, name FROM nodes WHERE id = :i"), {"i": node_id}).mappings().first()
        if not n:
            return {"error": f"no node {node_id}"}
        g = expand(s, seed, node_id, [], k=8)
        parents = s.execute(text("""
            SELECT p.id FROM edges e JOIN nodes p ON p.id = e.dst
            WHERE e.src = :i AND e.type = 'PART_OF' AND p.type IN ('Project', 'Organization')
              AND COALESCE(e.props->>'verdict', '') <> 'drop'"""), {"i": node_id}).scalars().all()
        try:
            summary = (summarize(s, node_id) or {}).get("summary")
        except Exception:
            summary = None
        return {**(_node_brief(s, [node_id]) or [{"node_id": node_id, "name": n["name"], "type": n["type"]}])[0],
                "summary": summary, "part_of": _node_brief(s, list(parents)),
                "contains": _node_brief(s, [x["id"] for x in g["nodes"] if x["id"] != node_id])}


@server.tool()
def why_connected(a: int, b: int) -> dict:
    """Why two nodes are linked, with the evidence: each direct link (what kind, the rule or AI check that made
    it and its reason), the conversations and events they share, and the latest shared records. When they aren't
    directly linked, the shortest path between them."""
    from igneous.graph.traversal import explain
    with SessionLocal() as s:
        r = explain(s, a, b)

    def link(e: dict) -> dict:
        return {"type": e["type"], "from": e.get("src_name"), "to": e.get("dst_name"), "made_by": e["extractor"],
                "checked": e.get("verdict"), "reason": e.get("reason") or e.get("why"),
                "evidence": [{"item_id": c["id"], "source": c["source"], "title": c["title"],
                              "date": str(c["date"])[:10] if c["date"] else None} for c in e.get("evidence", [])]}
    return {"a": r["a"], "b": r["b"], "messages_shared": r["messages"],
            "conversations": [{"thread_id": c["thread_id"], "name": c["thread"], "messages": c["messages"],
                               "people": c["people"], "first": str(c["first"])[:10], "last": str(c["last"])[:10]}
                              for c in r["conversations"]],
            "events": [{"name": e["name"], "date": str(e["at"])[:10]} for e in r["events"]],
            "direct_links": [link(e) for e in r.get("direct") or []],
            "path": [link(e) for e in r.get("path") or []] or None}


@server.tool()
def sources(node_id: int, limit: int = 10) -> list[dict]:
    """The records a node is made of, newest first (a person's messages with the user, a project's emails,
    chats, files and events, a message's own text), with item ids to cite and a snippet. Use `read_item` for full text."""
    from igneous.items import node_sources
    with SessionLocal() as s:
        r = node_sources(node_id, limit=min(limit, 30), offset=0, s=s)
    return [{"item_id": c["id"], "source": c["source"], "title": c["title"],
             "date": str(c["date"])[:10] if c["date"] else None, "snippet": c["snippet"]} for c in r["items"]]


@server.tool()
def read_item(item_id: int) -> dict:
    """The full text of one source record (an email, a chat message, a file, an event, an AI chat)."""
    with SessionLocal() as s:
        r = s.execute(text("SELECT id, source, title, occurred_at, body_text FROM source_items WHERE id = :i"),
                      {"i": item_id}).mappings().first()
    if not r:
        return {"error": f"no item {item_id}"}
    return {"item_id": r["id"], "source": r["source"], "title": r["title"],
            "date": r["occurred_at"].isoformat() if r["occurred_at"] else None, "text": _clip(r["body_text"], 6000)}


def main():
    # Warm the big picture in the background so the first `me` doesn't wait on ranking every relationship.
    import threading
    threading.Thread(target=lambda: _safe(me), daemon=True).start()
    server.run("stdio")


def _safe(f):
    try:
        f()
    except Exception:  # warming is best effort; the tool call reports any real error
        pass


if __name__ == "__main__":
    main()
