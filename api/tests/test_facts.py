"""Facts you or an agent add (#100): one write path (graph/facts.py) for POST /facts and the MCP tools `remember` and
`link`. A fact is a correction intent: it shows in the explorer with its origin, holds through every rebuild, and is
undone in one click; the write tools exist only with writes on, and answer by the rule every MCP answer follows."""
import asyncio
import json
import subprocess

import pytest
from fastapi.testclient import TestClient
from life_threads import trips  # noqa: F401
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import Implementation
from mixed_sources import PRIVATE, build, wipe
from pipeline_data import local_models  # noqa: F401
from sqlalchemy import text

from igneous import llm, mcp_client, mcp_server, pipeline
from igneous.config import settings
from igneous.graph import derived_invariants, identity
from igneous.main import app

client = TestClient(app)
WRITES = {"remember", "link"}


@pytest.fixture
def writes(monkeypatch):
    """The server with writes on (IGNEOUS_MCP_WRITE)."""
    monkeypatch.setattr(settings, "mcp_write", True)
    mcp_server.serve_writes(True)
    yield
    mcp_server.serve_writes(False)


def as_agent(tool: str, agent: str = "claude-code", **args) -> dict:
    """`tool` called through the protocol by a client that calls itself `agent`, as Claude Code does: the answer, or
    {"error": its message}."""
    async def call():
        async with Client(mcp_server.server, client_info=Implementation(name=agent, version="1")) as c:
            result = await c.call_tool(tool, args)
            body = result.content[0].text
            return {"error": body} if result.is_error else json.loads(body)
    return asyncio.run(call())


def node(s, key: str) -> int | None:
    return s.execute(text("SELECT id FROM nodes WHERE key = :k"), {"k": key}).scalar()


def fact(subject, relation, object_, **more) -> dict:
    response = client.post("/facts", json={"subject": subject, "relation": relation, "object": object_, **more})
    assert response.status_code == 200, response.text
    return response.json()


# --- Done when: a fact from the explorer and one from an agent show with their origin, survive a rebuild, undo ------

@pytest.mark.usefixtures("trips")
def test_a_fact_from_the_explorer_and_one_from_an_agent_show_with_their_origin_hold_and_undo(db, writes):
    pipeline.run(db)
    me = identity.self_id(db)
    bo, fay = (identity.head(db, node(db, k)) for k in ("bo@pals.test", "fay@pals.test"))
    lisbon = db.execute(text("SELECT id FROM nodes WHERE type = 'Project' AND name = 'Lisbon trip'")).scalar_one()
    # From the explorer: Bo came to Lisbon too. From Claude Code: Fay hosts a game night the graph never found.
    mine = fact(bo, "INVOLVED_IN", lisbon, note="he flew in for the last weekend")
    theirs = as_agent("remember", subject=fay, relation="INVOLVED_IN", object={"name": "Board Game Night", "kind": "hobby"},
                      note="Fay said she hosts it every Thursday")
    assert (mine["applied"], mine["by"]) == ("now", "user") and mine["id"] and mine["override_id"]
    assert (theirs["applied"], theirs["by"]) == ("now", "agent:claude-code") and theirs["to"]["new"]
    assert theirs["from"] == {"node_id": fay, "type": "Person", "name": "Fay Gold"}

    def drawn() -> dict:
        """Each fact's link as the explorer draws it when its person opens: [(type, origin)]."""
        night = node(db, "added:board game night")
        out = {}
        for person, other in ((bo, lisbon), (fay, night)):
            g = client.get("/graph/expand", params={"seed": me, "node": person, "visible": [me]}).json()
            out[person] = [(e["type"], e["origin"]) for e in g["edges"] if {e["src"], e["dst"]} == {person, other}]
        return out

    def why() -> list:
        night = node(db, "added:board game night")
        return [(e["origin"], e["stated"], e["how"], e["reason"]) for a, b in ((bo, lisbon), (fay, night)) if night
                for e in client.get("/graph/explain", params={"a": a, "b": b}).json()["direct"] if e["origin"] != "derived"]

    shown = {bo: [("INVOLVED_IN", "user")], fay: [("INVOLVED_IN", "agent:claude-code")]}
    said = [("user", True, "You added this.", "he flew in for the last weekend"),
            ("agent:claude-code", True, "Added by claude-code.", "Fay said she hosts it every Thursday")]
    assert drawn() == shown and why() == said
    pipeline.run(db)  # igneous pipeline: both hold, the new project too
    assert drawn() == shown and why() == said
    assert [v for v in derived_invariants.check(db) if v.code == "rejected_pair_linked"] == []
    # Undo: yours by its override id; the agent's in one click (Remove), which takes its new project with it.
    assert client.delete(f"/overrides/{mine['override_id']}").json()["applied"] == "now"
    agents = client.get("/graph/explain", params={"a": fay, "b": theirs["to"]["node_id"]}).json()["direct"]
    removed = client.delete(f"/graph/link/{agents[0]['id']}").json()
    assert removed["applied"] == "now" and node(db, "added:board game night") is None
    assert drawn() == {bo: [], fay: []}
    pipeline.run(db)
    assert drawn() == {bo: [], fay: []} and node(db, "added:board game night") is None
    # The removal is undone in one click too: the fact and its project come back.
    assert client.delete(f"/overrides/{removed['override_id']}").json()["applied"] == "now"
    assert drawn() == {bo: [], fay: [("INVOLVED_IN", "agent:claude-code")]}


# --- Facts: what they may say, the new things they bring in, and that they are statements --------------------------

def test_a_new_thing_is_in_the_graph_while_a_fact_names_it(db):
    ids = [db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Person', :k, :k) RETURNING id"),
                      {"k": k}).scalar_one() for k in ("mo@x.test", "ana@x.test")]
    first = fact(ids[0], "INVOLVED_IN", {"name": "Board Game Night", "kind": "hobby"}, note="every Thursday")
    night = node(db, "added:board game night")
    assert first["nodes"] == [night] and first["dst"] == night and first["id"]
    assert db.execute(text("SELECT name, props FROM nodes WHERE id = :i"), {"i": night}).one() == (
        "Board Game Night", {"added_by": "user", "original_name": "Board Game Night", "kind": "hobby"})
    second = fact(ids[1], "INVOLVED_IN", {"name": "board game  night"})  # the same new thing: one node
    assert second["nodes"] == [night]
    again = fact(ids[0], "INVOLVED_IN", {"name": "Board Game Night", "kind": "hobby"}, note="said twice")
    assert again["already"] and again["override_id"] == first["override_id"]
    assert db.execute(text("SELECT count(*) FROM overrides")).scalar() == 2
    renamed = client.post(f"/graph/entity/{night}/rename", json={"name": "Thursday games"}).json()
    assert renamed["applied"] == "now"
    assert client.get(f"/graph/entity/{night}").json()["added_by"] == "user"
    out = client.delete(f"/graph/link/{first['id']}").json()  # another fact still names it: it stays, renamed
    assert db.execute(text("SELECT name FROM nodes WHERE id = :i"), {"i": night}).scalar() == "Thursday games"
    client.delete(f"/overrides/{second['override_id']}")  # no fact names it now
    assert node(db, "added:board game night") is None
    client.delete(f"/overrides/{out['override_id']}")  # the first fact back: so is the thing, and your name for it
    assert db.execute(text("SELECT name FROM nodes WHERE key = 'added:board game night'")).scalar() == "Thursday games"


def test_an_agents_fact_is_a_statement_taken_out_never_judged_wrong(db):
    person = db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Person', 'pat@x.test', 'Pat') RETURNING id"
                             )).scalar_one()
    trip = db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Project', 'thread:trip', 'Trip') RETURNING id"
                           )).scalar_one()
    derived = db.execute(text("""INSERT INTO edges (src, dst, type, extractor) VALUES (:p, :t, 'INVOLVED_IN', 'threads')
                                 RETURNING id"""), {"p": person, "t": trip}).scalar_one()
    theirs = fact(person, "RELATED_TO", trip, agent="helper", note="Pat planned it")
    assert theirs["by"] == "agent:helper"
    assert client.post("/graph/edge/wrong", json={"edge": theirs["id"]}).status_code == 400
    wrong = client.post("/graph/edge/wrong", json={"edge": derived}).json()  # your rejection of the pair...
    assert wrong["pairs"][0]["still_linked"]  # ...keeps the agent's statement, as it keeps yours
    assert [v for v in derived_invariants.check(db) if v.code == "rejected_pair_linked"] == []
    out = client.delete(f"/graph/link/{theirs['id']}").json()
    assert out["pairs"][0]["still_linked"] is False and db.execute(text(
        "SELECT count(*) FROM edges WHERE extractor LIKE 'agent:%'")).scalar() == 0
    client.delete(f"/overrides/{out['override_id']}")
    assert db.execute(text("SELECT extractor, props->>'why' FROM edges WHERE extractor LIKE 'agent:%'")).one() == (
        "agent:helper", "Pat planned it")


# --- The MCP write tools: only with writes on, checked first, and answering by the rule ---------------------------

def listed(*args: str) -> set[str]:
    """The tools the registered command serves, run as a client runs it."""
    exe, *rest = mcp_client.command()
    server = subprocess.Popen([exe, *rest, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    try:
        for message in ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}):
            server.stdin.write(json.dumps(message) + "\n")
            server.stdin.flush()
        while line := server.stdout.readline():
            if (message := json.loads(line)).get("id") == 2:
                return {t["name"] for t in message["result"]["tools"]}
        raise AssertionError(f"the server ended without answering: {server.stderr.read()}")
    finally:
        server.stdin.close()
        server.wait(timeout=30)
        server.stdout.close()
        server.stderr.close()


def test_the_write_tools_exist_only_when_writes_are_on(test_db, monkeypatch):
    assert not WRITES & {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    with pytest.raises(ToolError, match="Unknown tool"):
        asyncio.run(mcp_server.server.call_tool("link", {"a": 1, "b": 2, "why": "x"}))
    assert not WRITES & listed() and WRITES <= listed("--write")
    monkeypatch.setenv("IGNEOUS_MCP_WRITE", "true")
    assert WRITES <= listed() and not WRITES & listed("--read-only")


@pytest.mark.parametrize("tool, args", [
    ("remember", {"subject": 1, "relation": "SAME_AS", "object": 2, "note": "x"}),
    ("remember", {"subject": 1, "relation": "INVOLVED_IN", "object": 2, "note": ""}),
    ("remember", {"subject": 1, "relation": "INVOLVED_IN", "object": 2, "note": "x" * 501}),
    ("remember", {"subject": 0, "relation": "INVOLVED_IN", "object": 2, "note": "x"}),
    ("remember", {"subject": 1, "relation": "INVOLVED_IN", "object": {"name": ""}, "note": "x"}),
    ("remember", {"subject": 1, "relation": "INVOLVED_IN", "object": {"name": "x", "type": "Person"}, "note": "x"}),
    ("remember", {"subject": 1, "relation": "INVOLVED_IN", "object": 2}),
    ("link", {"a": 0, "b": 2, "why": "x"}), ("link", {"a": 1, "b": 2}), ("link", {"a": "one", "b": 2, "why": "x"}),
])
def test_write_arguments_are_checked_before_any_database_work(writes, monkeypatch, tool, args):
    def forbidden():
        raise AssertionError("invalid arguments must not reach the database")

    monkeypatch.setattr(mcp_server, "SessionLocal", forbidden)
    with pytest.raises(ToolError):
        asyncio.run(mcp_server.server.call_tool(tool, args))


@pytest.fixture
def mixed(test_db, monkeypatch, writes):
    """The mixed-source graph (gmail opted in, imessage never: test_mcp_policy.py), with writes on."""
    def no_model(*_args, **_kwargs):
        raise AssertionError("a fact never calls a model")

    monkeypatch.setattr(settings, "llm_sources", "gmail")
    monkeypatch.setattr(llm, "chat", no_model)
    monkeypatch.setattr(llm, "structured", no_model)
    yield build()
    wipe()


def facts_recorded() -> list[tuple]:
    from igneous.db import SessionLocal

    with SessionLocal() as s:
        return [tuple(r) for r in s.execute(text("""SELECT node_keys, value, data->>'by' FROM overrides
                                                    WHERE op = 'link' ORDER BY id"""))]


def test_an_agents_fact_never_names_what_the_opt_in_hides(mixed, monkeypatch):
    g = mixed
    hidden = as_agent("remember", subject=g["pat"], relation="INVOLVED_IN", object=g["atlas"], note="Pat went")
    assert "isn't shared with you" in hidden["error"] and facts_recorded() == []
    linked = as_agent("link", a=g["sam"], b=g["atlas"], why="Sam leads the Atlas demo")
    assert (linked["from"]["name"], linked["to"]["name"], linked["by"]) == ("Sam Shared", "Atlas", "agent:claude-code")
    new = as_agent("remember", agent="Cursor", subject=g["me"], relation="INVOLVED_IN",
                   object={"name": "Board games", "kind": "hobby"}, note="the user plays on Fridays")
    assert new["by"] == "agent:Cursor" and new["to"]["new"] and "name" not in new["to"]
    answers = [json.dumps(a) for a in (hidden, linked, new)]
    # What the facts brought in is named only as any name is: here, by no record the client may read.
    found = as_agent("find", name="Board games")
    assert found["results"] == [] and found["withheld"]["nodes"] == {"Project": 1}
    answers += [json.dumps(as_agent(t, **a)) for t, a in (("open_node", {"node_id": g["me"]}),
                                                         ("why_connected", {"a": g["me"], "b": new["to"]["node_id"]}),
                                                         ("why_connected", {"a": g["sam"], "b": g["atlas"]}))]
    assert not [m for a in answers for m in PRIVATE if m in a]
    # With nothing opted in, only the user and new things can be stated about, and nothing is named.
    monkeypatch.setattr(settings, "llm_sources", "")
    assert "isn't shared with you" in as_agent("link", a=g["sam"], b=g["atlas"], why="again")["error"]
    owner = as_agent("remember", subject=g["me"], relation="RELATED_TO", object={"name": "Climbing"}, note="it's hers")
    assert owner["from"] == {"node_id": g["me"], "type": "Person", "name": None}
    assert owner["withheld"]["nodes"] == {"Person": 1}
    assert [(k, t, by) for k, t, by in facts_recorded()] == [
        (["Person:email:sam@shared.test", "Project:thread:atlas"], "RELATED_TO", "agent:claude-code"),
        (["Person:me", "Project:added:board games"], "INVOLVED_IN", "agent:Cursor"),
        (["Person:me", "Project:added:climbing"], "RELATED_TO", "agent:claude-code")]


def test_check_lists_the_write_tools_and_calls_none(mixed):
    calls = mcp_client.check(target=mcp_server.server, query="Atlas")
    writers = {c.tool: c for c in calls if c.tool in WRITES}
    assert set(writers) == WRITES and all(c.ok is None and "not called" in c.note for c in writers.values())
    assert all(c.ok for c in calls if c.tool not in WRITES | {"ask"}) and facts_recorded() == []


def test_install_registers_writes_when_asked(tmp_path, monkeypatch):
    import os
    import stat
    import sys

    log = tmp_path / "claude.log"
    fake = tmp_path / "claude"
    fake.write_text(f"#!{sys.executable}\nimport sys\nopen({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    assert mcp_client.install("igneous-write", database="igneous_persona", write=True)[-1] == "--write"
    assert log.read_text().splitlines()[-1].endswith("mcp --database igneous_persona --write")
