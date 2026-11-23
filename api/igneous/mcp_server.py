"""MCP server: the graph as tools for Claude Code, Claude Desktop and Cursor, over stdio.

    uv run igneous mcp           # what a client starts; `igneous mcp install` registers it with Claude Code

The tools call the readers the explorer and the HTTP API call (graph/traversal.py, graph/find.py, graph/summary.py,
items.py, timeline.py, graph/history.py, rag/), each through a read-only database session. What a tool returns reaches
the model its client runs, an outbound path for the user's records like a hosted model's prompt (CO-1), so every answer
goes through igneous/disclosure.py: text only from the sources opted in to hosted models (IGNEOUS_LLM_SOURCES), names
only of the people and things those records name, and counts of the rest (`withheld`). With nothing opted in, the
default, the tools return the graph's structure and counts only.

`ask` is the one tool that calls a hosted model (Claude on Bedrock, which costs money): it answers from opted-in
records as Ask does and, like every model call, keeps the model's reading of the question in the model cache. No other
tool writes anything.

Arguments are checked against each tool's schema before any database work. A tool given the id of nothing, or asked
for something it can't do, fails with a message (the result's `isError`): the one error shape.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from typing import TYPE_CHECKING, Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import source_catalog
from igneous.db import read_only_session
from igneous.graph import identity

if TYPE_CHECKING:
    from igneous.disclosure import Disclosure

# The model client, search and embedding stacks load on first use, not at import: an MCP client connects only once the
# server answers `initialize`, and a slow start makes the first tool call race the connection.

server = MCPServer(
    "igneous",
    log_level="WARNING",  # stderr is the client's log of this server: problems only
    instructions=(
        "A personal knowledge graph of the user's life, built from their email, texts, calendar, Drive, GitHub, files "
        "and AI chats: people (one node per human, all addresses folded in), group chats, and life threads (trips, "
        "classes, jobs, projects) under the organizations and schools on their resume. Start with `me` for the big "
        "picture; `find` turns a name into a node_id; `open_node` shows what something is made of; `why_connected` "
        "explains a link with the records behind it; `sources` and `read_item` read those records; `correspondents` "
        "ranks someone's relationships; `timeline` gives dated activity; `commitments` gives cited promises. Answer "
        "questions about structure (who went, who's on it, what's under it) with these, usually in 2-4 calls. Use "
        "`search` or `ask` only for what was said. Cite node_ids and item_ids. A result's `withheld` counts the records "
        "and names the user hasn't opted in to share with you: never guess at them. Everything is read-only."
    ),
)

NodeId = Annotated[int, Field(ge=1, description="a node_id another tool returned")]
ItemId = Annotated[int, Field(ge=1, description="an item_id another tool returned")]
NO_OWNER = ("No owner: no source says which person is the user (several people's mailboxes, or none of the user's own "
            "accounts connected). Use `find` to start from someone.")
# What you wrote and talked about, not what you browsed: page visits and search history only on request.
CONTENT_SOURCES = frozenset(source_catalog.CONTENT)
ACTIVITY = ("messages", "first", "last", "recent", "people", "total_people", "sources")


@contextmanager
def _reading() -> Iterator[tuple[Session, "Disclosure"]]:
    """A read-only session and the rule for this answer; an id of nothing is the tool's error."""
    from igneous.disclosure import Disclosure  # loads the model client: on the first call, not at start

    with read_only_session() as s:
        try:
            yield s, Disclosure(s)
        except identity.NotFound as e:
            raise ToolError(f"no node {e.node_id}") from e


def _day(value: date | datetime | str | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()[:10] if isinstance(value, date | datetime) else str(value)[:10]


def _when(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _facts(s: Session, ids: list[int]) -> dict[int, dict]:
    """What each node is beyond its name: its section (`category`), the dates a thread spans, how many records it
    holds. A thread shown as a node is a group chat."""
    from igneous.graph.traversal import category

    out = {}
    for r in s.execute(text("""
            SELECT id, type, props->>'kind' AS kind, props->>'start' AS start, props->>'end' AS end,
                   props->>'records' AS records FROM nodes WHERE id = ANY(:i)"""), {"i": ids}).mappings():
        f = {"type": "Group chat" if r["type"] == "Thread" else r["type"], "category": category(r["type"], r["kind"])}
        if r["start"]:
            f["from"], f["to"] = _day(r["start"]), _day(r["end"])
        if r["records"]:
            f["records"] = int(r["records"])
        out[r["id"]] = f
    return out


def _briefs(s: Session, d: "Disclosure", ids: list[int]) -> list[dict]:
    """The nodes among `ids` that may be named, each with what it is (`_facts`), in order; the rest counted."""
    ids = list(dict.fromkeys(ids))
    facts = _facts(s, ids)
    return d.nodes({"node_id": i, **facts[i]} for i in ids if i in facts)


def _history(s: Session, d: "Disclosure", me: int) -> list[dict]:
    """Where you worked and studied, newest first: each role your resume or LinkedIn lists (an internship, then the
    job: one organization's link holds them, props.roles, graph/profile.py), with its own dates, and each link you
    stated yourself. A title is record text, shared only from an opted-in record (the one that lists the role), or
    when you stated the link yourself."""
    rows = s.execute(text("""
        SELECT e.dst AS org, e.extractor, e.props, e.valid_from, e.valid_to, si.source
        FROM edges e LEFT JOIN source_items si ON si.id = e.source_item_id
        WHERE e.src = :me AND e.type = 'WORKED_AT' AND e.extractor IN ('profile', 'user')
          AND COALESCE(e.props->>'verdict', '') <> 'drop'
        ORDER BY e.id"""), {"me": me}).mappings().all()
    listed = {int(r["source_item_id"]) for row in rows for r in (row["props"] or {}).get("roles") or []
              if r.get("source_item_id") is not None}
    sources = dict(s.execute(text("SELECT id, source FROM source_items WHERE id = ANY(:i)"),
                             {"i": sorted(listed)}).all()) if listed else {}
    named = d.names(r["org"] for r in rows)
    out = []
    for r in rows:
        if r["org"] not in named:
            d.withhold_nodes("Organization")
            continue
        props = r["props"] or {}
        roles = [(role, sources.get(role.get("source_item_id"))) for role in props.get("roles") or []] or [
            ({"title": props.get("title"), "start": r["valid_from"], "end": r["valid_to"],
              "school": props.get("school")}, r["source"])]
        for role, source in roles:
            out.append({"node_id": r["org"], "name": named[r["org"]], "school": bool(role.get("school")),
                        "title": role.get("title") if r["extractor"] == "user" or d.readable(source) else None,
                        "from": _day(role.get("start")), "to": _day(role.get("end")) or "present"})
    return sorted(out, key=lambda h: h["from"] or "", reverse=True)


@server.tool()
def me() -> dict:
    """The big picture: who the user is, where they work and study, and the most meaningful parts of their life now
    (their sustained projects, trips and classes, their closest people, recency-weighted): the view the explorer opens
    on. Start here."""
    from igneous.graph.traversal import view

    with _reading() as (s, d):
        head = identity.self_id(s)
        if head is None:
            raise ToolError(NO_OWNER)
        g = view(s, head)
        return d.report({"node_id": head, "name": d.name(head), "history": _history(s, d, head),
                         "closest": _briefs(s, d, [n["id"] for n in g["nodes"] if n["id"] != head])})


@server.tool()
def find(name: Annotated[str, Field(min_length=1, max_length=200, description="a name, or part of one")]) -> dict:
    """A name to graph nodes, best match first: people (every address and number they use), projects, trips, classes,
    organizations, group chats, events, places and files, ranked by how well the name matches and how close each is
    to the user."""
    from igneous.graph.find import find as find_nodes

    with _reading() as (s, d):
        hits = find_nodes(s, name, None, limit=20)
        named = d.nodes({"node_id": h["id"], "type": h["type"], "category": h["category"], "context": h["context"]}
                        for h in hits)
        return d.report({"results": named[:12]})


@server.tool()
def open_node(node_id: NodeId) -> dict:
    """What a node is made of, the way the explorer opens it: an organization's projects and people; a project's or
    trip's people, group chats and documents; a group chat's members; a person's closest people, group chats and
    projects. Also what it is part of, and its activity (message counts and dates)."""
    from igneous.graph.summary import gather
    from igneous.graph.traversal import expand

    with _reading() as (s, d):
        n = identity.node(s, node_id)
        opened = expand(s, identity.self_id(s), n["id"], [], k=8)
        # What it is part of, as the explorer draws it: an organization's own thread (graph/backbone.py) is the
        # organization.
        parents = s.execute(text("""
            SELECT DISTINCT CASE WHEN p.type = 'Project' AND p.props ? 'org' THEN (p.props->>'org')::bigint ELSE p.id END
            FROM edges e JOIN nodes p ON p.id = e.dst
            WHERE e.src = :i AND e.type = 'PART_OF' AND p.type IN ('Project', 'Organization')
              AND COALESCE(e.props->>'verdict', '') <> 'drop'
            ORDER BY 1"""), {"i": n["id"]}).scalars().all()
        stats = gather(s, n["id"])["stats"]
        facts = _facts(s, [n["id"]]).get(n["id"], {})
        return d.report({"node_id": n["id"], "name": d.name(n["id"]), **facts,
                         "activity": {k: stats[k] for k in ACTIVITY if stats.get(k) not in (None, [])},
                         "part_of": _briefs(s, d, list(parents)),
                         "contains": _briefs(s, d, [x["id"] for x in opened["nodes"] if x["id"] != n["id"]])})


def _record(card: dict) -> dict:
    """An item card as a tool shows it (its text already checked readable)."""
    return {"item_id": card["id"], "source": card["source"], "title": card.get("title"), "date": _day(card.get("date")),
            "snippet": card.get("snippet")}


def _link(d: "Disclosure", e: dict) -> dict | None:
    """A link between two nodes, both of which may be named; else None (counted)."""
    a, b = e.get("from", e["src"]), e.get("to", e["dst"])
    named = d.names([a, b])
    if a not in named or b not in named:
        d.withhold_nodes("link")
        return None
    return {"type": e["type"], "from": {"node_id": a, "name": named[a]}, "to": {"node_id": b, "name": named[b]},
            "made_by": e["extractor"], "origin": e.get("origin"), "stated": e.get("stated"), "how": e.get("how"),
            "checked": e.get("verdict"), "reason": e.get("reason"),
            "evidence": [_record(c) for c in d.records(e.get("evidence") or [])]}


@server.tool()
def why_connected(a: NodeId, b: NodeId) -> dict:
    """Why two nodes are linked: each direct link (what kind, the rule, AI check or person that made it, its reason,
    the records behind it), the conversations and events they share, their latest shared records, and the
    corrections the user made to the pair. When they aren't directly linked, the shortest path between them."""
    from igneous.graph.traversal import explain

    with _reading() as (s, d):
        try:
            r = explain(s, a, b, named={})
        except ValueError as e:
            raise ToolError(str(e)) from e
        conversations = []
        for c in r["conversations"]:  # a conversation is named by its own records and quoted from them
            if not d.readable(c["source"]):
                d.withhold_records(c["source"], c["messages"])
                continue
            conversations.append({"thread_id": c["thread_id"], "name": c["thread"], "messages": c["messages"],
                                  "people": c["people"], "first": _day(c["first"]), "last": _day(c["last"]),
                                  "source": c["source"], "excerpt": c["excerpt"]})
        events = [{"node_id": e["node_id"], "name": e["name"], "date": _day(e["at"])}
                  for e in d.nodes({"node_id": e["id"], "type": "Event", "at": e["at"]} for e in r["events"])]
        corrections = [{"from": {"node_id": c["src"], "name": d.name(c["src"])},
                        "to": {"node_id": c["dst"], "name": d.name(c["dst"])}, "rejected": _day(c["updated_at"]),
                        "override_id": c["override_id"]} for c in r["corrections"]]
        direct = [x for x in (_link(d, e) for e in r.get("direct") or []) if x]
        path = [x for x in (_link(d, e) for e in r.get("path") or []) if x]
        if r.get("path") and len(path) < len(r["path"]):
            path = []  # a path with a hop that can't be named would be a gap, not a route
        return d.report({"a": {"node_id": r["a"]["id"], "name": d.name(r["a"]["id"])},
                         "b": {"node_id": r["b"]["id"], "name": d.name(r["b"]["id"])},
                         "messages_shared": r["messages"], "conversations": conversations, "events": events,
                         "shared": [_record(c) for c in d.records(r.get("shared") or [])],
                         "direct_links": direct, "path": path or None, "corrections": corrections})


@server.tool()
def timeline(node_id: NodeId, other_node_id: NodeId | None = None,
             limit: Annotated[int, Field(ge=1, le=200)] = 50) -> dict:
    """Dated activity for a node, or the direct relationship history between `node_id` and `other_node_id`, newest
    first: each link's kind, direction, dates and the record it came from (an item_id for `read_item`), no record
    text. Merged identities count as one."""
    from igneous.timeline import timeline as dated_links

    with _reading() as (s, d):
        r = dated_links(s, a=node_id, b=other_node_id, limit=limit)
        named = d.names(e["counterpart"]["id"] for e in r["edges"])
        edges = []
        for e in r["edges"]:
            other = e["counterpart"]
            if other["id"] in named:
                edges.append({**e, "counterpart": {**other, "name": named[other["id"]]}})
            else:
                d.withhold_nodes(other["type"])
        return d.report({**r, "a": {**r["a"], "name": d.name(r["a"]["id"])},
                         "b": {**r["b"], "name": d.name(r["b"]["id"])} if r["b"] else None, "edges": edges})


@server.tool()
def commitments(node_id: NodeId, status: Literal["open", "resolved", "all"] = "open",
                limit: Annotated[int, Field(ge=1, le=200)] = 50) -> dict:
    """Promises the model found in a node's records (a person's sent and received messages, a project's or
    organization's records), newest first, each with who, what, by when, its status and the exact span it was read
    from. For a person, check `who`: a promise in a message they received may be someone else's. Never calls a model."""
    from igneous.graph.history import commitments as found

    with _reading() as (s, d):
        head = identity.node(s, node_id)["id"]
        claims = d.records(found(s, status=status, limit=limit, node_id=head), source=lambda c: c["evidence"]["source"])
        return d.report({"node_id": head, "commitments": [
            {**c, "evidence": {**c["evidence"], "occurred_at": _when(c["evidence"]["occurred_at"])}} for c in claims]})


@server.tool()
def sources(node_id: NodeId, limit: Annotated[int, Field(ge=1, le=50)] = 10,
            offset: Annotated[int, Field(ge=0, le=10_000)] = 0) -> dict:
    """The records a node is made of, newest first (a person's messages with the user and the events they attended, a
    project's emails, chats, files and events, a message's own record), with item_ids and a snippet. `read_item` reads
    one in full; `offset` pages on while `more` is true."""
    from igneous.items import sources_of

    with _reading() as (s, d):
        r = sources_of(s, node_id, limit, offset, named={})
        return d.report({"items": [_record(c) for c in d.records(r["items"])], "more": r["more"]})


@server.tool()
def read_item(item_id: ItemId, offset: Annotated[int, Field(ge=0)] = 0,
              max_chars: Annotated[int, Field(ge=200, le=50_000)] = 6000) -> dict:
    """One record in full: an email, a chat day, a file, an event, an AI conversation. Long records come in pages:
    `text` holds `max_chars` characters from `offset`, and `more` says whether there is more."""
    from igneous.items import read

    with _reading() as (s, d):
        r = read(s, item_id, named={})
        if r is None:
            raise ToolError(f"no item {item_id}")
        out = {"item_id": r["id"], "source": r["source"], "date": _when(r["occurred_at"])}
        if not d.readable(r["source"]):
            d.withhold_records(r["source"])
            return d.report(out)
        body = r["text"] or ""
        return d.report({**out, "title": r["title"], "meta": r["meta"], "length": len(body),
                         "text": body[offset:offset + max_chars], "more": offset + max_chars < len(body)})


@server.tool()
def correspondents(person_node_id: NodeId, limit: Annotated[int, Field(ge=1, le=60)] = 20) -> dict:
    """Someone's real relationships, closest first: two-way, weighted toward recent contact, one-on-one and small-group
    talk counting most (a big group chat says little about any one member). Automated senders are left out."""
    from igneous.graph.traversal import relationships

    with _reading() as (s, d):
        head = identity.node(s, person_node_id)
        if head["type"] != "Person":
            raise ToolError(f"node {person_node_id} is a {head['type']}, not a person")
        ranked = [{"node_id": p, "type": "Person", "closeness": round(strength, 3), "messages": r["msgs"],
                   "weeks_in_touch": r["weeks"], "last": _day(r.get("last_at"))}
                  for p, strength, r in relationships(s, head["id"], limit)]
        return d.report({"node_id": head["id"], "name": d.name(head["id"]), "correspondents": d.nodes(ranked)})


def _filters(d: "Disclosure", applied: dict) -> dict:
    """The filters a search or an answer used (constraints.describe), with people named only when they may be."""
    def who(p: dict | None) -> dict | None:
        return {"node_id": p["id"], "name": d.name(p["id"])} if p else None
    return {**applied, "person": who(applied.get("person")), "mentions": who(applied.get("mentions")),
            "since": _when(applied.get("since")), "until": _when(applied.get("until"))}


@server.tool()
def search(query: Annotated[str, Field(min_length=1, max_length=500)], person_node_id: NodeId | None = None,
           include_browsing: bool = False, k: Annotated[int, Field(ge=1, le=20)] = 8) -> dict:
    """Keyword + semantic search over the records the user shares with you (email, texts, calendar, files, GitHub,
    AI chats): passages best first, each with an item_id to cite. Filters the query states in plain words apply ("from
    Dana", "in my calendar", "in May 2025"). `person_node_id` keeps records that person sent or received; browsing and
    search history are left out unless `include_browsing`."""
    from dataclasses import replace

    from igneous.rag.constraints import apply, describe
    from igneous.rag.search import Filters, hybrid

    with _reading() as (s, d):
        if person_node_id is not None:
            person_node_id = identity.node(s, person_node_id)["id"]
        shareable = d.sources if include_browsing else d.sources & CONTENT_SOURCES
        if not shareable:
            return d.report({"results": [], "note": "No source the user shares with you holds such records: nothing "
                                                    "is opted in (IGNEOUS_LLM_SOURCES)."})
        f = apply(s, query, Filters(person_id=person_node_id), model=False)  # only `ask` calls a model
        within = shareable & set(f.sources) if f.sources else shareable
        if not within:
            return d.report({"results": [], "filters": _filters(d, describe(s, f)),
                             "note": "The query asks for sources the user doesn't share with you."})
        f = replace(f, sources=tuple(sorted(within)))
        hits = d.records(hybrid(s, query, f, k=k))
        return d.report({"results": [{"item_id": h["source_item_id"], "chunk_id": h["chunk_id"], "node_id": h["node_id"],
                                      "source": h["source"], "title": h["title"], "date": _day(h["occurred_at"]),
                                      "excerpt": h["text"][:600]} for h in hits],
                         "filters": _filters(d, describe(s, f))})


@server.tool()
def ask(question: Annotated[str, Field(min_length=1, max_length=2000)]) -> dict:
    """A cited answer to a question about what the records say, or `answerable: false`. Runs Ask server-side: search,
    graph expansion and a cite-or-abstain answer from the hosted model, over the sources the user opted in. Calls a
    paid model: prefer the graph tools and `search` for anything they can answer."""
    from igneous.llm import ProviderError
    from igneous.rag import qa

    with _reading() as (s, d):
        try:
            r = qa.answer(s, question)
        except ProviderError as e:  # off, not configured, unreachable, or no usable answer
            raise ToolError(f"ask is unavailable: {e}. The graph tools and `search` don't need the model.") from e
        cited = d.records(r["citations"])
        return d.report({"answerable": r["answerable"] and bool(cited), "answer": r["answer"],
                         "citations": [{"item_id": c["source_item_id"], "node_id": c["node_id"],
                                        "chunk_id": c["chunk_id"], "source": c["source"], "title": c["title"],
                                        "date": _day(c["occurred_at"])} for c in cited],
                         "filters": _filters(d, r.get("filters") or {})})


def main(database: str | None = None, sources: str | None = None) -> None:
    """Serve over stdio. `database` names another database on the configured server, and `sources` replaces
    IGNEOUS_LLM_SOURCES, for this server only (`igneous mcp --database --sources`)."""
    import sys
    import threading

    from igneous import db
    from igneous.config import settings

    if database:
        db.use_database(database)
    if sources is not None:
        settings.llm_sources = sources
    if problem := db.schema_status():
        print(f"igneous mcp: {problem}", file=sys.stderr)  # stdout is the protocol channel
        raise SystemExit(2)
    # Rank every relationship once in the background, so the first `me` doesn't wait on it.
    threading.Thread(target=_warm, daemon=True).start()
    server.run("stdio")


def _warm() -> None:
    try:
        me()
    except Exception:  # best effort: the tool call itself reports any real error
        pass


if __name__ == "__main__":
    main()
