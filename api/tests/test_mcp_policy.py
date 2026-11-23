"""What the MCP server hands its client follows the rule every hosted model prompt follows (igneous/disclosure.py):
text only from opted-in sources, names only of what those records name, counts for the rest. Every tool is called on
every node and record of a graph that mixes an opted-in source (gmail) with one that never is (imessage, which alone
names Pat Private), through the protocol, as a client calls it."""
import asyncio
import json
import os
import stat
import subprocess
import sys

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mixed_sources import PRIVATE, build, every_call, wipe
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from igneous import llm, mcp_client, mcp_server
from igneous.config import settings
from igneous.db import SessionLocal, read_only_session


@pytest.fixture
def graph(test_db, monkeypatch):
    def no_model(*_args, **_kwargs):
        raise AssertionError("no tool but ask calls a model")

    monkeypatch.setattr(settings, "llm_sources", "gmail")
    monkeypatch.setattr(llm, "chat", no_model)
    monkeypatch.setattr(llm, "structured", no_model)  # search's filters too: only the rules and a cached reading
    yield build()
    wipe()


def call(tool: str, **args) -> dict:
    result = asyncio.run(mcp_server.server.call_tool(tool, args))
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def said(tool: str, **args) -> str:
    """Everything a call hands the client, an answer or an error."""
    try:
        return asyncio.run(mcp_server.server.call_tool(tool, args)).content[0].text
    except ToolError as e:
        return str(e)


def failure(tool: str, **args) -> str:
    """The message of a call that must fail (the client gets it as an `isError` result)."""
    with pytest.raises(ToolError) as e:
        asyncio.run(mcp_server.server.call_tool(tool, args))
    return str(e.value)


def test_no_tool_discloses_text_or_names_outside_the_opt_in(graph):
    for tool, args in every_call(graph):
        answer = said(tool, **args)
        assert not [marker for marker in PRIVATE if marker in answer], (tool, args, answer)


def test_what_is_opted_in_is_shared_and_the_rest_is_counted(graph):
    g = graph
    assert {r["name"] for r in call("find", name="Sam")["results"]} == {"Sam Shared"}
    assert call("find", name="Pat")["results"] == []
    assert call("find", name="Pat")["withheld"]["nodes"] == {"Person": 1, "Project": 1}  # Pat and "Pat trip"
    gmail = call("sources", node_id=g["atlas"], limit=50)
    assert {i["source"] for i in gmail["items"]} == {"gmail"} and gmail["withheld"]["records"] == {"imessage": 4}
    text_ = call("read_item", item_id=gmail["items"][0]["item_id"])["text"]
    assert text_.startswith("Atlas status from the Robotics Lab")
    with SessionLocal() as s:
        texted = s.execute(text("SELECT source_item_id FROM nodes WHERE id = :i"), {"i": g["items"]["i0"]}).scalar()
    private = call("read_item", item_id=texted)
    assert "text" not in private and private["withheld"]["records"] == {"imessage": 1}
    pair = call("why_connected", a=g["me"], b=g["pat"])
    assert pair["b"]["name"] is None and pair["conversations"] == [] and pair["shared"] == []
    assert call("why_connected", a=g["me"], b=g["sam"])["b"]["name"] == "Sam Shared"


def test_with_nothing_opted_in_only_structure_and_counts_leave(graph, monkeypatch):
    monkeypatch.setattr(settings, "llm_sources", "")
    for tool, args in every_call(graph):
        answer = said(tool, **args)
        for name in ("Sam Shared", "Atlas", "Robotics", "Pat Private", "Me Myself", *PRIVATE):
            assert name not in answer, (tool, args, answer)


def test_ask_answers_from_opted_in_records_and_cites_ids_to_follow(graph, monkeypatch):
    def answer(_system, user, _schema, **_options):
        cited = int(user.split("[c", 1)[1].split("]", 1)[0])
        return {"answerable": True, "answer": "The demo is next.", "cited": [cited]}, {}

    monkeypatch.setattr(settings, "reranker_enabled", False)
    monkeypatch.setattr(llm, "structured", answer)
    r = call("ask", question="What are the next steps for the Atlas demo?")
    assert r["answerable"] and r["citations"]
    for c in r["citations"]:
        assert c["source"] == "gmail" and c["item_id"] and c["node_id"] and c["chunk_id"]
    assert not [marker for marker in PRIVATE if marker in json.dumps(r)]


def test_research_shares_only_opted_in_records_and_names(graph, monkeypatch):
    """The research tool's agent calls every read tool on every node and record (scripted: no key); its briefing,
    written from what came back, carries nothing from outside the opt-in, and cites opted-in mail."""
    from scripted_model import ScriptedAgent, ScriptedWriter

    from igneous.agent import research

    calls = [(t, a) for t, a in every_call(graph) if not any(m in json.dumps(a) for m in PRIVATE)]
    turns = [calls[i:i + 30] for i in range(0, len(calls), 30)]
    monkeypatch.setattr(research, "BUDGET", research.Budget(turns=len(turns) + 1, tool_calls=len(calls),
                                                            seconds=3600, unproductive=len(turns) + 1))
    monkeypatch.setattr(llm, "unavailable", lambda: None)
    monkeypatch.setattr(llm, "tool_turn", ScriptedAgent(lambda t, _r: turns[t - 1] if t <= len(turns) else "done"))
    monkeypatch.setattr(llm, "structured", ScriptedWriter())
    answer = said("research", question="Brief me on Atlas and the people in it")
    briefing = json.loads(answer)
    assert briefing["sources"] and {s["source"] for s in briefing["sources"]} == {"gmail"}
    assert briefing["research"]["tool_calls"] == len(calls)
    assert not [marker for marker in PRIVATE if marker in answer]


def test_ask_without_the_model_fails_with_the_reason(graph, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise llm.LLMDisabled("no Bedrock key")

    monkeypatch.setattr(settings, "reranker_enabled", False)
    monkeypatch.setattr(llm, "structured", unavailable)
    assert "no Bedrock key" in failure("ask", question="Atlas next steps?")


def test_tools_read_through_sessions_that_refuse_writes(graph):
    with read_only_session() as s, pytest.raises(DBAPIError, match="read-only transaction"):
        s.execute(text("INSERT INTO llm_cache (cache_key, response) VALUES ('x', '{}')"))
    with read_only_session() as s:  # and stay read-only in the transaction after one ends
        s.rollback()
        with pytest.raises(DBAPIError, match="read-only transaction"):
            s.execute(text("DELETE FROM llm_cache"))


def test_a_read_only_session_joins_a_tests_transaction_without_changing_it(db):
    with read_only_session() as s:
        assert s.execute(text("SELECT current_setting('transaction_read_only')")).scalar() == "on"
    db.execute(text("INSERT INTO llm_cache (cache_key, response) VALUES ('after', '{}')"))


def test_unknown_ids_are_errors_not_empty_answers(graph):
    for tool, args in (("open_node", {"node_id": 99_999}), ("timeline", {"node_id": 99_999}),
                       ("why_connected", {"a": graph["me"], "b": 99_999}), ("sources", {"node_id": 99_999}),
                       ("commitments", {"node_id": 99_999}), ("correspondents", {"person_node_id": 99_999}),
                       ("read_item", {"item_id": 99_999}), ("search", {"query": "x", "person_node_id": 99_999})):
        assert "99999" in failure(tool, **args), tool
    assert "same node" in failure("why_connected", a=graph["me"], b=graph["me"])
    assert "not a person" in failure("correspondents", person_node_id=graph["atlas"])


@pytest.mark.parametrize(("tool", "args"), [
    ("open_node", {"node_id": 0}), ("open_node", {"node_id": -3}), ("find", {"name": ""}),
    ("find", {"name": "x" * 201}), ("search", {"query": "x", "k": 0}), ("search", {"query": "x", "k": 21}),
    ("sources", {"node_id": 1, "limit": 51}), ("sources", {"node_id": 1, "offset": -1}),
    ("read_item", {"item_id": 1, "max_chars": 10}), ("read_item", {"item_id": 0}),
    ("correspondents", {"person_node_id": 1, "limit": 61}), ("why_connected", {"a": 1}),
    ("timeline", {"node_id": 1, "limit": 201}), ("commitments", {"node_id": 1, "status": "late"}),
    ("ask", {"question": ""}), ("open_node", {"node_id": "seven"}), ("research", {"question": ""}),
    ("research", {"question": "x" * 2001}),
])
def test_arguments_are_checked_before_any_database_work(monkeypatch, tool, args):
    def forbidden():
        raise AssertionError("invalid arguments must not reach the database")

    monkeypatch.setattr(mcp_server, "read_only_session", forbidden)
    with pytest.raises(ToolError):
        asyncio.run(mcp_server.server.call_tool(tool, args))


def test_check_calls_every_tool_through_the_protocol(graph):
    calls = mcp_client.check(target=mcp_server.server, query="Atlas")
    assert {c.tool for c in calls} == {t.name for t in asyncio.run(mcp_server.server.list_tools())} - mcp_client.PAID
    assert all(c.ok for c in calls), [(c.tool, c.note) for c in calls if not c.ok]


def test_check_calls_the_paid_tools_when_asked(graph, monkeypatch):
    from scripted_model import ScriptedAgent, ScriptedWriter

    writer = ScriptedWriter()

    def structured(system, user, schema, **options):
        if "answerable" in schema["properties"]:  # ask's
            return {"answerable": True, "answer": "Next steps.", "cited": [int(user.split("[c", 1)[1].split("]")[0])]}, {}
        return writer(system, user, schema, **options)

    monkeypatch.setattr(settings, "reranker_enabled", False)
    monkeypatch.setattr(llm, "unavailable", lambda: None)
    monkeypatch.setattr(llm, "structured", structured)
    monkeypatch.setattr(llm, "tool_turn", ScriptedAgent(
        lambda turn, _r: [("find", {"name": "Atlas"}), ("sources", {"node_id": graph["atlas"]})] if turn == 1 else "x"))
    calls = {c.tool: c for c in mcp_client.check(target=mcp_server.server, query="Atlas", paid=True)}
    assert set(calls) == {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    assert calls["ask"].ok and calls["research"].ok, (calls["ask"].note, calls["research"].note)
    assert calls["research"].args == {"question": "Brief me on Atlas."} and writer.prompts


def test_install_registers_the_command_and_no_credential(tmp_path, monkeypatch):
    log = tmp_path / "claude.log"
    fake = tmp_path / "claude"
    fake.write_text(f"#!{sys.executable}\nimport sys\nopen({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "secret-key-value")
    registered = mcp_client.install("igneous-demo", database="igneous_persona", sources="email,fs")
    remove, add = log.read_text().splitlines()
    assert remove == "mcp remove --scope user igneous-demo"
    assert add.startswith("mcp add --scope user igneous-demo -- ")
    assert add.endswith("mcp --database igneous_persona --sources email,fs") and add.endswith(" ".join(registered[1:]))
    assert "secret-key-value" not in add and "igneous:igneous" not in add and "AWS" not in add


def test_the_server_command_starts_and_lists_its_tools(test_db):
    """The registered command, run as a client runs it: a subprocess speaking the protocol on stdin and stdout."""
    exe, *args = mcp_client.command()
    server = subprocess.Popen([exe, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)
    try:
        def send(message: dict) -> None:
            server.stdin.write(json.dumps(message) + "\n")
            server.stdin.flush()

        def reply(request_id: int) -> dict:
            while line := server.stdout.readline():
                if (message := json.loads(line)).get("id") == request_id:
                    return message
            raise AssertionError(f"the server ended without answering: {server.stderr.read()}")

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        assert reply(1)["result"]["serverInfo"]["name"] == "igneous"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = reply(2)["result"]["tools"]
        assert sorted(t["name"] for t in tools) == sorted(t.name for t in asyncio.run(mcp_server.server.list_tools()))
    finally:
        server.stdin.close()
        server.wait(timeout=30)
        server.stdout.close()
        server.stderr.close()
