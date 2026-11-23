"""Setting up the MCP server: registering this checkout's server with Claude Code, and checking every tool through it.

`install` runs `claude mcp add` with the command a client starts: this checkout's `igneous mcp`, and the database and
opted-in sources it should serve when they differ from api/.env. No credential goes into the client's configuration:
the server reads the database's password from api/.env and the model key from the environment or from what
`igneous connect bedrock` saved, when it starts.

`check` starts that same command as a client would, calls each tool once over the protocol with arguments it takes from
the answers before (the owner from `me`, their closest node, a record of it), and reports each call's time and outcome.
`ask` calls a paid model, so it runs only when asked for. The tools that add to the graph (`remember` and `link`, served
with writes on) are listed, never called: a check changes nothing. The calls also bring what the tools read into the
database's memory, so the first question of a demo doesn't wait on the disk.
"""
import asyncio
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

WRITES = frozenset({"remember", "link"})  # the tools that add to the graph (IGNEOUS_MCP_WRITE): `check` never calls


def command(database: str | None = None, sources: str | None = None, write: bool | None = None) -> list[str]:
    """The command an MCP client runs to start this checkout's server: the virtualenv's `igneous` script (it starts in
    about two seconds, where `uv run` first syncs the environment), with the database and sources to serve, and
    whether its agent may add facts (`write`; None: as IGNEOUS_MCP_WRITE says)."""
    exe = Path(sys.executable).with_name("igneous")
    args = [str(exe if exe.exists() else Path(sys.argv[0]).resolve()), "mcp"]
    if database:
        args += ["--database", database]
    if sources is not None:
        args += ["--sources", sources]
    if write is not None:
        args += ["--write" if write else "--read-only"]
    return args


def install(name: str = "igneous", database: str | None = None, sources: str | None = None,
            scope: str = "user", write: bool | None = None) -> list[str]:
    """Register the server with Claude Code under `name`, replacing a server registered under that name before.
    Returns the command registered. Raises RuntimeError when the `claude` command is missing or refuses."""
    claude = shutil.which("claude")
    cmd = command(database, sources, write)
    if claude is None:
        raise RuntimeError("Claude Code's `claude` command isn't on your PATH. Install Claude Code, or register the "
                           f"server in your MCP client with this command: {' '.join(cmd)}")
    subprocess.run([claude, "mcp", "remove", "--scope", scope, name], capture_output=True, text=True)
    done = subprocess.run([claude, "mcp", "add", "--scope", scope, name, "--", *cmd], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"`claude mcp add` failed: {(done.stderr or done.stdout).strip()}")
    return cmd


@dataclass
class Call:
    tool: str
    args: dict
    seconds: float
    ok: bool  # it answered; False for a failure, None when there was nothing to call it with
    note: str
    answer: dict | None = field(default=None, repr=False)


def _note(answer: dict) -> str:
    """One line on what a tool answered: how many things of each list, and what it withheld."""
    parts = [f"{len(v)} {k}" for k, v in answer.items() if isinstance(v, list) and k != "sources"]
    withheld = answer.get("withheld") or {}
    for what in ("records", "nodes"):
        if withheld.get(what):
            parts.append(f"withheld {sum(withheld[what].values())} {what}")
    if answer.get("note"):
        parts.append(answer["note"])
    return ", ".join(parts) or "ok"


async def _calls(target, ask: bool, query: str | None) -> list[Call]:
    from mcp import Client

    out: list[Call] = []
    async with Client(target) as client:
        async def call(tool: str, **args) -> dict | None:
            t = time.perf_counter()
            r = await client.call_tool(tool, args)
            seconds = time.perf_counter() - t
            body = r.content[0].text if r.content else ""
            if r.is_error:
                out.append(Call(tool, args, seconds, False, body))
                return None
            answer = json.loads(body) if body else {}
            out.append(Call(tool, args, seconds, True, _note(answer), answer))
            return answer

        listed = {t.name for t in (await client.list_tools()).tools}
        me = await call("me") or {}
        owner = me.get("node_id")
        near = next((n["node_id"] for n in me.get("closest") or []), None)
        found = await call("find", name=query or me.get("name") or "a") or {}
        start = owner or next((n["node_id"] for n in found.get("results") or []), None)
        near = near or next((n["node_id"] for n in found.get("results") or [] if n["node_id"] != start), None)
        if start:
            await call("open_node", node_id=near or start)
            await call("timeline", node_id=near or start, limit=20)
            await call("commitments", node_id=start, limit=20, status="all")
            if near and near != start:
                await call("why_connected", a=start, b=near)
            listing = await call("sources", node_id=near or start, limit=5) or {}
            item = next((i["item_id"] for i in listing.get("items") or []), None)
            if item:
                await call("read_item", item_id=item, max_chars=500)
            if owner:
                await call("correspondents", person_node_id=owner, limit=10)
        await call("search", query=query or "meeting", k=5)
        if ask:
            await call("ask", question=query or "What happened most recently?")
        called = {c.tool for c in out}
        for tool in sorted(listed - called - ({"ask"} if not ask else set())):
            out.append(Call(tool, {}, 0.0, None, "served; not called: it adds to the graph" if tool in WRITES
                            else "not called: no node or record is shared to call it with"))
    return out


def check(target=None, database: str | None = None, sources: str | None = None, ask: bool = False,
          query: str | None = None, write: bool | None = None) -> list[Call]:
    """Call every tool once through `target` (an in-process server in tests), else the server `command` starts. The
    tools that write are listed, never called."""
    if target is None:
        from mcp.client.stdio import StdioServerParameters

        exe, *args = command(database, sources, write)
        target = StdioServerParameters(command=exe, args=args)
    return asyncio.run(_calls(target, ask, query))
