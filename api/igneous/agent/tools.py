"""The research agent's tools: the MCP server's read tools, called through the server as a client calls them.

What a tool returns goes into a hosted model's prompt, so the agent uses the MCP tools themselves (mcp_server.py): each
reads through a read-only session and answers through igneous/disclosure.py, the rule every prompt follows (text only
from the opted-in sources, names only of what their records name, counts for the rest). Calling them through the
server's `call_tool` checks the arguments as a client's call is checked, and an id of nothing, an impossible request or
a crash comes back as the tool's error, which the model reads like any other result.

The tools an agent needs to brief on a person, project, organization or question are these. Finding an entity is
`find`; getting one, its members and a person's activity are `open_node` (what a node is made of, what it is part of,
its message counts and dates); a thread's messages are `sources` (any node's records, newest first, in pages); how two
things are linked, and the group chats two people share, are `why_connected`; `read_item`, `search`, `commitments` and
`timeline` keep their names; `me` and `correspondents` round them out. `ask` is not one: the agent is the model.

`Ledger` keeps what the agent has read: each record whose text a result showed (a search excerpt, a snippet among a
node's records or a link's evidence, a promise read from it, the record itself) and each node a result named. A
briefing cites only records in it.
"""
import asyncio
import copy
import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache

from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from igneous import mcp_server

log = logging.getLogger(__name__)

TOOLS = ("me", "find", "open_node", "why_connected", "sources", "read_item", "search", "commitments", "timeline",
         "correspondents")
# The most one call may return, so that a few calls can't spend the budget: (argument, largest value). The model sees
# the cap as the argument's maximum, and a larger value is lowered to it.
CAPS = {"read_item": ("max_chars", 8000), "sources": ("limit", 20), "search": ("k", 10),
        "commitments": ("limit", 30), "timeline": ("limit", 30), "correspondents": ("limit", 20)}


@lru_cache(maxsize=1)
def definitions() -> tuple[dict, ...]:
    """The tools as the model is told about them: each MCP tool's name, description and argument schema, the caps
    above as maxima. Callers must not change them."""
    listed = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}
    out = []
    for name in TOOLS:
        schema = copy.deepcopy(listed[name].input_schema)
        if name in CAPS:
            arg, cap = CAPS[name]
            prop = schema["properties"][arg]
            prop["maximum"] = min(prop.get("maximum", cap), cap)
            prop["default"] = min(prop["default"], cap)
        out.append({"name": name, "description": listed[name].description, "input_schema": schema})
    return tuple(out)


def _capped(name: str, args: dict) -> dict:
    if name not in CAPS:
        return args
    arg, cap = CAPS[name]
    default = next(d for d in definitions() if d["name"] == name)["input_schema"]["properties"][arg]["default"]
    value = args.get(arg, default)
    return {**args, arg: min(value, cap) if isinstance(value, int) else value}


@dataclass
class Outcome:
    """One tool call: the tool's answer, as an MCP client gets it, or its error message."""
    tool: str
    args: dict
    answer: dict | None = None
    error: str | None = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None

    def content(self) -> str:
        """What the model reads: the answer as compact JSON, or the error."""
        if self.error is not None:
            return self.error
        return json.dumps(self.answer, ensure_ascii=False, separators=(",", ":"), default=str)

    def note(self) -> str:
        """One line on what came back: how many of each list, how much text, what was withheld; or the error."""
        if self.error is not None:
            return self.error[:300]
        answer = self.answer or {}
        parts = [f"{len(v)} {k}" for k, v in answer.items() if isinstance(v, list) and v]
        if isinstance(answer.get("text"), str):
            parts.append(f"{len(answer['text'])} of {answer.get('length', len(answer['text']))} characters")
        for what, counts in (answer.get("withheld") or {}).items():
            if isinstance(counts, dict):
                parts.append(f"withheld {sum(counts.values())} {what}")
        return ", ".join(parts) or "nothing"


def call(name: str, args: dict) -> Outcome:
    """Run one of TOOLS with `args` through the MCP server, as a client's call runs."""
    if name not in TOOLS:
        return Outcome(name, args, error=f"There is no tool {name!r}; the tools are {', '.join(TOOLS)}.")
    args = _capped(name, args)
    start = time.perf_counter()
    try:
        result = asyncio.run(mcp_server.server.call_tool(name, args))
    except UnexpectedToolError as e:  # a crash: its details stay in the log
        log.warning("research tool %s(%s) failed", name, args, exc_info=e.__cause__ or e)
        return Outcome(name, args, error=f"{name} failed; try another way.", seconds=time.perf_counter() - start)
    except ToolError as e:  # an id of nothing, an impossible request, arguments out of bounds
        return Outcome(name, args, error=str(e), seconds=time.perf_counter() - start)
    text = result.content[0].text if result.content else "{}"
    if result.is_error:
        return Outcome(name, args, error=text, seconds=time.perf_counter() - start)
    return Outcome(name, args, answer=json.loads(text), seconds=time.perf_counter() - start)


def _day(value) -> str | None:
    return str(value)[:10] if value else None


def _card(r: dict, text: str | None) -> dict:
    return {"item_id": r["item_id"], "source": r["source"], "title": r.get("title"), "date": _day(r.get("date")),
            "text": text}


def _promise(c: dict) -> str:
    """A promise as the agent read it: who, what, by when, its status."""
    by = f" by {c['by_when']}" if c.get("by_when") else ""
    return f"Promise ({c.get('status') or 'open'}): {c.get('who') or 'someone'} will {c.get('what') or '?'}{by}."


HEADER = ("from", "to", "cc", "organizer", "attendees", "chat", "group", "start", "end")  # of a record's metadata


def _who(value) -> str:
    if isinstance(value, list):
        return ", ".join(filter(None, (_who(v) for v in value)))
    if isinstance(value, dict):
        return str(value.get("name") or value.get("email") or "")
    return str(value)


def _header(meta: dict | None) -> str:
    """Who a record is from and to, where and when, on one line: the part of its metadata the tool showed."""
    return "; ".join(f"{k}: {who}" for k in HEADER if (who := _who((meta or {}).get(k) or "")))


def records(tool: str, answer: dict) -> Iterator[dict]:
    """The records a tool's answer showed text of, each {item_id, source, title, date, text}. A record read in full
    keeps who it is from and to above its text. A timeline names records (an item_id for each link) without their
    text, and a conversation's excerpt has no record id: neither is a record read."""
    if tool == "search":
        for r in answer.get("results") or []:
            yield _card(r, r.get("excerpt"))
    elif tool == "sources":
        for r in answer.get("items") or []:
            yield _card(r, r.get("snippet"))
    elif tool == "read_item" and "text" in answer:
        yield _card(answer, "\n".join(filter(None, (_header(answer.get("meta")), answer["text"]))))
    elif tool == "commitments":
        for c in answer.get("commitments") or []:
            ev = c["evidence"]
            yield {"item_id": ev["source_item_id"], "source": ev["source"], "title": None,
                   "date": _day(ev.get("occurred_at")), "text": _promise(c)}
    elif tool == "why_connected":
        for r in answer.get("shared") or []:
            yield _card(r, r.get("snippet"))
        for link in (answer.get("direct_links") or []) + (answer.get("path") or []):
            for r in link.get("evidence") or []:
                yield _card(r, r.get("snippet"))


def _nodes_in(x) -> Iterator[tuple[int, str, str | None]]:
    if isinstance(x, dict):
        if isinstance(x.get("node_id"), int) and isinstance(x.get("name"), str):
            yield x["node_id"], x["name"], x.get("type")
        for value in x.values():
            yield from _nodes_in(value)
    elif isinstance(x, list):
        for value in x:
            yield from _nodes_in(value)


def named(tool: str, answer: dict) -> Iterator[tuple[int, str, str | None]]:
    """The nodes a tool's answer named, in the order it named them: (node_id, name, type)."""
    yield from _nodes_in(answer)
    if tool == "timeline":  # nodes there are {id, type, name}
        ends = [answer.get("a"), answer.get("b")] + [e.get("counterpart") for e in answer.get("edges") or []]
        for n in ends:
            if n and isinstance(n.get("name"), str):
                yield n["id"], n["name"], n.get("type")


def _flat(text: str | None) -> str:
    return " ".join((text or "").split())


@dataclass
class Read:
    """A record the agent read: what it is, and each passage of it the results showed, in the order they came."""
    item_id: int
    source: str
    title: str | None
    date: str | None
    texts: list[str] = field(default_factory=list)

    def take(self, text: str | None) -> bool:
        """Keep `text` unless a passage already holds it (spacing aside: a snippet is the record's opening on one
        line), in place of the passages it holds; True when it is new."""
        flat = _flat(text)
        if not flat or any(flat in _flat(t) for t in self.texts):
            return False
        self.texts = [t for t in self.texts if _flat(t) not in flat] + [str(text).strip()]
        return True



@dataclass
class Learned:
    """What one tool call added to the ledger."""
    items: list[Read] = field(default_factory=list)  # records read for the first time
    passages: int = 0  # new text of records read before
    nodes: int = 0

    def __bool__(self) -> bool:
        return bool(self.items or self.passages or self.nodes)


class Ledger:
    """Everything the agent has read: records (`items`, by item_id) and named nodes (`nodes`: node_id -> {name,
    type})."""

    def __init__(self):
        self.items: dict[int, Read] = {}
        self.nodes: dict[int, dict] = {}

    def add(self, outcome: Outcome) -> Learned:
        learned = Learned()
        if outcome.answer is None:
            return learned
        for r in records(outcome.tool, outcome.answer):
            read = self.items.get(r["item_id"])
            if read is None:
                read = self.items[r["item_id"]] = Read(r["item_id"], r["source"], r["title"], r["date"])
                read.take(r["text"])
                learned.items.append(read)
                continue
            read.title, read.date = read.title or r["title"], read.date or r["date"]
            learned.passages += read.take(r["text"])
        for node_id, name, type_ in named(outcome.tool, outcome.answer):
            if node_id not in self.nodes:
                self.nodes[node_id] = {"name": name, "type": type_}
                learned.nodes += 1
            elif type_ and not self.nodes[node_id]["type"]:
                self.nodes[node_id]["type"] = type_
        return learned
