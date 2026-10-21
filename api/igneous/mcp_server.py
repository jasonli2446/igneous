"""MCP server: the graph as tools for Claude Code / Claude Desktop / Cursor (stdio). Read-only by design (CO-2).

    uv run igneous mcp          # stdio; see README for the client config

Low-level tools (search, get_node, neighbors, evidence) let an agent explore; `ask` runs the server-side
retrieval pipeline (graph expansion + cite-or-abstain) so it doesn't spend turns redoing it. Every result
carries node / chunk ids so the agent can cite. Tool results go to whatever model the MCP client uses:
that's an outbound path for user content (CO-1), the same as a configured model provider.
"""
from mcp.server.mcpserver import MCPServer
from sqlalchemy import text

from igneous.db import SessionLocal
from igneous.graph.traversal import people
from igneous.rag import qa
from igneous.rag.constraints import apply
from igneous.rag.search import Filters, hybrid

server = MCPServer(
    "igneous",
    instructions=(
        "Search and explore a personal knowledge graph built from the user's email, files, and calendar. "
        "Use `ask` for questions that need an answer; use `search`, `get_node`, `neighbors`, and `evidence` to "
        "explore. Cite results by their chunk_id or node_id. Person nodes are entity-resolved: one node per "
        "human, with all their addresses folded in."
    ),
)


def _clip(t: str | None, n: int = 600) -> str:
    return (t or "")[:n]


@server.tool()
def search(query: str, person: str | None = None, k: int = 8) -> list[dict]:
    """Hybrid keyword + semantic search over all content. `person` (an email address) restricts to items they
    sent or received. Returns chunks with chunk_id, node_id, title, date, and a text excerpt."""
    with SessionLocal() as s:
        hits = hybrid(s, query, apply(s, query, Filters(person=person)), k=min(k, 20))
    return [{"chunk_id": h["chunk_id"], "node_id": h["node_id"], "title": h["title"], "source": h["source"],
             "date": h["occurred_at"].isoformat() if h["occurred_at"] else None, "excerpt": _clip(h["text"])}
            for h in hits]


@server.tool()
def find_entity(name: str, type: str | None = None) -> list[dict]:
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
    """Who a person exchanges the most email with, by message count (entity-resolved)."""
    with SessionLocal() as s:
        g = people(s, person_node_id, limit=min(limit, 60))
    if not g["nodes"]:
        return []
    me = g["nodes"][0]["id"]  # people() returns the (canonical) seed first
    weight = {e["dst"] if e["src"] == me else e["src"]: e["weight"] for e in g["edges"] if me in (e["src"], e["dst"])}
    return sorted(({"node_id": n["id"], "name": n["name"], "messages_exchanged": weight.get(n["id"], 0)}
                   for n in g["nodes"][1:]), key=lambda r: -r["messages_exchanged"])


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


def main():
    server.run("stdio")


if __name__ == "__main__":
    main()
