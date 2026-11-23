"""The research agent (#92): its loop and budget, the ledger of what it read, the briefing's citations, its runs
(POST /research, GET /research/{id}, the cache by graph version), the MCP tool and the CLI. A scripted model stands in
for the hosted one (tests/scripted_model.py)."""
import asyncio
import itertools
import json
import os
import time
from datetime import UTC, datetime

import click
import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from mixed_sources import PRIVATE, _edge, _node, build, wipe
from scripted_model import ScriptedAgent, ScriptedWriter, shown
from sqlalchemy import text
from typer.testing import CliRunner

from igneous import jobs, llm, mcp_server
from igneous.agent import report, research, tools
from igneous.config import settings
from igneous.db import SessionLocal
from igneous.graph import dirty
from igneous.main import app

QUESTION = "Brief me on the Harbor Survey project"


@pytest.fixture
def model(monkeypatch):
    """The hosted model, scripted: `model(plan, writer)` puts them in its place and returns them."""
    monkeypatch.setattr(llm, "unavailable", lambda: None)

    def install(plan, writer: ScriptedWriter | None = None):
        agent, writer = ScriptedAgent(plan), writer or ScriptedWriter()
        monkeypatch.setattr(llm, "tool_turn", agent)
        monkeypatch.setattr(llm, "structured", writer)
        return agent, writer
    return install


def failing(error: Exception):
    def plan(_turn, _results):
        raise error
    return plan


# ---- a project, as the pipeline and the verifier leave it ----

PEOPLE = {"me": ("me", "Mira Castellanos", "mira@harbor.test"), "dana": ("email:dana@harbor.test", "Dana Kimura",
                                                                        "dana@harbor.test"),
          "theo": ("email:theo@harbor.test", "Theo Marsh", "theo@harbor.test")}
MAILS = [  # (ref, sender, subject, body, when)
    ("m1", "dana", "Harbor Survey kickoff", "Dana here. I'll lead the field sampling for the Harbor Survey, and Theo "
     "draws the maps.", datetime(2025, 3, 1, 9, tzinfo=UTC)),
    ("m2", "theo", "Harbor Survey maps", "The first Harbor Survey maps are attached: six stations along the pier.",
     datetime(2025, 3, 10, 14, tzinfo=UTC)),
    ("m3", "me", "Re: Harbor Survey report", "Thanks both. I will send the draft Harbor Survey report by April 20.",
     datetime(2025, 4, 2, 18, tzinfo=UTC)),
    ("m4", "dana", "Harbor Survey sampling done", "Sampling for the Harbor Survey is done. Next steps: the lab results, "
     "due April 15.", datetime(2025, 4, 5, 11, tzinfo=UTC)),
]


def build_project() -> dict:
    """Harbor Survey: four mails between you, Dana (who leads the sampling) and Theo (the maps), filed under the
    project, each with who sent it to whom; Dana and Theo checked to be on it."""
    with SessionLocal() as s:
        g: dict = {"items": {}}
        for who, (key, name, _address) in PEOPLE.items():
            g[who] = _node(s, "Person", key, name, {"self": True} if who == "me" else None)
        g["project"] = _node(s, "Project", "thread:harbor-survey", "Harbor Survey", {
            "derived": "threads", "kind": "project", "start": MAILS[0][4].isoformat(), "end": MAILS[-1][4].isoformat()})
        for ref, sender, subject, body, when in MAILS:
            others = [p for p in PEOPLE if p != sender]
            meta = {"from": [{"name": PEOPLE[sender][1], "email": PEOPLE[sender][2]}],
                    "to": [{"name": PEOPLE[p][1], "email": PEOPLE[p][2]} for p in others]}
            item = s.execute(text("""
                INSERT INTO source_items (source, external_id, content_hash, title, body_text, occurred_at, raw_meta)
                VALUES ('gmail', :e, :e, :t, :b, :w, CAST(:m AS jsonb)) RETURNING id"""),
                {"e": ref, "t": subject, "b": body, "w": when, "m": json.dumps(meta)}).scalar_one()
            message = _node(s, "Message", f"gmail:{ref}", subject, item=item)
            _edge(s, g[sender], message, "SENT", item=item)
            for p in others:
                _edge(s, message, g[p], "RECEIVED", item=item)
            _edge(s, message, g["project"], "PART_OF", "threads", item=item)
            g["items"][ref] = item
        for who in ("dana", "theo"):
            s.execute(text("""INSERT INTO edges (src, dst, type, props, extractor, extractor_version)
                              VALUES (:a, :b, 'INVOLVED_IN', '{"verdict": "keep"}', 'threads', '1')"""),
                      {"a": g[who], "b": g["project"]})
        s.commit()
    return g


@pytest.fixture
def project(committed_db, monkeypatch):
    monkeypatch.setattr(settings, "llm_sources", "gmail")
    return build_project()


def project_plan(turn: int, results: list[dict]):
    """Find the project, open it and list its records, see how each of its people is linked to it, read every record,
    then answer."""
    if turn == 1:
        return [("find", {"name": "Harbor Survey"})]
    if turn == 2:
        node = results[0]["results"][0]["node_id"]
        return [("open_node", {"node_id": node}), ("sources", {"node_id": node, "limit": 20}),
                ("commitments", {"node_id": node})]
    if turn == 3:
        opened, listed, _promises = results
        people = [("why_connected", {"a": opened["node_id"], "b": c["node_id"]}) for c in opened["contains"]
                  if c["type"] == "Person"]
        return people + [("read_item", {"item_id": i["item_id"]}) for i in listed["items"]]
    return "Dana leads the sampling and Theo the maps; your draft report is due April 20."


# ---- the loop and its budget, with the tools stubbed ----

class Tools:
    """Stands in for the MCP tools: each call reads a record it hasn't before, unless `same`."""

    def __init__(self, same: bool = False):
        self.same, self.calls = same, []

    def __call__(self, name: str, args: dict) -> tools.Outcome:
        self.calls.append((name, args))
        if args.get("node_id") == 99:
            return tools.Outcome(name, args, error="no node 99")
        n = 1 if self.same else len(self.calls)
        answer = {"items": [{"item_id": n, "source": "gmail", "title": f"Mail {n}", "date": "2025-01-01",
                             "snippet": f"text {n}"}], "more": False}
        return tools.Outcome(name, args, answer=answer, seconds=0.01)


def stubbed(monkeypatch, same: bool = False) -> Tools:
    calls = Tools(same)
    monkeypatch.setattr(tools, "call", calls)
    monkeypatch.setattr(tools, "definitions", lambda: ({"name": "sources", "description": "records",
                                                        "input_schema": {"type": "object", "properties": {}}},))
    return calls


def explore(budget=research.BUDGET, clock=time.monotonic):
    steps: list[dict] = []
    return research.explore(QUESTION, steps.append, budget, clock), steps


def test_the_agent_answers_when_it_has_what_it_needs(model, monkeypatch):
    calls = stubbed(monkeypatch)
    agent, _ = model(lambda turn, _r: [("sources", {"node_id": turn})] if turn < 3 else "Done: Dana leads it.")
    found, steps = explore()
    assert (found.stopped, found.turns, found.tool_calls, found.notes) == ("answered", 3, 2, "Done: Dana leads it.")
    assert [s["kind"] for s in steps] == ["turn", "tool", "turn", "tool", "turn", "stop"]
    assert steps[1]["read"] == [{"item_id": 1, "title": "Mail 1"}] and len(calls.calls) == 2
    assert found.tokens == {"input_tokens": 3000, "output_tokens": 300}
    sent = agent.sent[0]
    assert sent["messages"] == [{"role": "user", "content": QUESTION}]
    assert "You have 12 turns and 30 tool" in sent["system"] and sent["options"]["timeout"] >= research.TURN_TIMEOUT


def test_the_conversation_is_append_only(model, monkeypatch):
    """Each turn sends the conversation before it unchanged, then the model's last turn as it came (its thinking block
    and signature too), then one result for each tool call, an error marked as one."""
    stubbed(monkeypatch)
    agent, _ = model(lambda turn, _r: [("sources", {"node_id": 1}), ("sources", {"node_id": 99})] if turn < 3
                     else "done")
    explore()
    histories = [s["messages"] for s in agent.sent]
    assert len(histories) == 3
    for before, after in itertools.pairwise(histories):
        assert after[:len(before)] == before
        assistant, results = after[len(before):]
        assert assistant["role"] == "assistant" and assistant["content"][0]["type"] == "thinking"
        assert assistant["content"][0]["signature"] == f"signature-of-turn-{len(before) // 2 + 1}"
        asked = [b["id"] for b in assistant["content"] if b["type"] == "tool_use"]
        assert [r["tool_use_id"] for r in results["content"]] == asked
        assert [r.get("is_error", False) for r in results["content"]] == [False, True]
        assert results["content"][1]["content"] == "no node 99"


@pytest.mark.parametrize(("limit", "expect"), [
    ({"turns": 3}, ("turns", 3, 3)),
    ({"tool_calls": 5}, ("tool_calls", 2, 5)),  # 3 calls a turn: the second turn runs 2 of its 3
    ({"tokens": 2500}, ("tokens", 3, 3)),  # 1,100 tokens a turn: under 2,500 after two turns, not after three
    ({"unproductive": 2}, ("unproductive", 3, 3)),  # the same record every call: turns 2 and 3 learn nothing
])
def test_each_budget_stops_the_research(model, monkeypatch, limit, expect):
    calls = stubbed(monkeypatch, same="unproductive" in limit)
    per_turn = 3 if "tool_calls" in limit else 1
    model(lambda turn, _r: [("sources", {"node_id": turn * 10 + i}) for i in range(per_turn)])
    found, steps = explore(research.Budget(**limit))
    assert (found.stopped, found.turns, found.tool_calls) == expect
    assert steps[-1] == {"kind": "stop", "reason": expect[0]} and len(calls.calls) == expect[2]


def test_the_issues_budget():
    assert research.BUDGET == research.Budget(turns=12, tool_calls=30, tokens=200_000, seconds=120, unproductive=2)


def test_a_spent_budget_still_answers_every_call(model, monkeypatch):
    stubbed(monkeypatch)
    agent, _ = model(lambda turn, _r: [("sources", {"node_id": i}) for i in range(4)] if turn == 1 else "done")
    found, steps = explore(research.Budget(tool_calls=3))
    assert found.stopped == "tool_calls" and found.tool_calls == 3 and len(agent.sent) == 1
    assert sum(s["kind"] == "tool" for s in steps) == 3


def test_time_stops_the_research(model, monkeypatch):
    stubbed(monkeypatch)
    now = [0.0]

    def clock() -> float:
        now[0] += 25.0  # every look at the clock is 25 s later
        return now[0]

    model(lambda turn, _r: [("sources", {"node_id": turn})])
    found, _steps = explore(research.Budget(seconds=120), clock)
    assert found.stopped == "time" and found.turns == 2


def test_a_model_failure_after_the_first_turn_stops_the_research_there(model, monkeypatch):
    stubbed(monkeypatch)

    def plan(turn, _results):
        if turn == 2:
            raise llm.ProviderUnavailable("Bedrock is rate-limiting; try again shortly")
        return [("sources", {"node_id": turn})]

    model(plan)
    found, steps = explore()
    assert found.stopped == "model_error" and found.turns == 1 and found.ledger.items
    assert "rate-limiting" in steps[-1]["detail"]
    model(failing(llm.LLMDisabled("no Bedrock key")))
    with pytest.raises(llm.LLMDisabled):
        explore()


def test_the_offered_tools_are_the_mcp_read_tools_with_caps():
    offered = {t["name"]: t for t in tools.definitions()}
    assert set(offered) == set(tools.TOOLS) and not {"ask", "research"} & set(offered)
    listed = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}
    assert all(offered[n]["description"] == listed[n].description for n in offered)
    for name, (arg, cap) in tools.CAPS.items():
        prop = offered[name]["input_schema"]["properties"][arg]
        assert prop["maximum"] == cap and prop["default"] <= cap
    assert tools._capped("read_item", {"item_id": 1, "max_chars": 50_000})["max_chars"] == 8000
    assert tools._capped("timeline", {"node_id": 1})["limit"] == 30


# ---- the ledger and the briefing's citations ----

def test_brief_me_on_a_project_gives_its_people_timeline_and_open_items(project, model):
    """The issue's "Done when", with the scripted model: a project's briefing names its people and their parts, gives
    its timeline and its open items, and every citation is a record the agent read."""
    g = project
    _agent, writer = model(project_plan, ScriptedWriter(about="Harbor Survey"))
    steps: list[dict] = []
    out = research.brief(QUESTION, steps.append)
    briefing = out["report"]
    assert {(p["name"], p["node_id"]) for p in briefing["people"]} == {("Dana Kimura", g["dana"]),
                                                                        ("Theo Marsh", g["theo"])}
    assert [e["cites"] for e in briefing["timeline"]] == [[g["items"][ref]] for ref, *_ in MAILS]
    m = g["items"]
    assert [e["cites"] for e in briefing["open_items"]] == [[m["m4"]], [m["m3"]], [m["m1"]]]
    assert "draft Harbor Survey report by April 20" in briefing["open_items"][1]["item"]
    read = {r["item_id"] for s in steps if s["kind"] == "tool" for r in s["read"]}
    cites = [c for section in report.SECTIONS for e in briefing[section] for c in e["cites"]]
    assert cites and set(cites) <= read == set(m.values())
    assert {s["item_id"] for s in briefing["sources"]} == set(cites)
    assert briefing["checked"] == {"records_read": 4, "records_cited": 4, "statements": 10, "kept": 10,
                                   "dropped": 0, "invalid_citations": 0}
    assert out["usage"]["stopped"] == "answered" and out["usage"]["tool_calls"] == 10
    assert steps[-1] == {"kind": "report", **briefing["checked"]}
    prompt = writer.prompts[0]
    assert "from: Dana Kimura; to: Mira Castellanos, Theo Marsh" in prompt  # who sent each record read in full
    assert "Dana leads the sampling" in prompt  # the agent's notes
    assert "Dana Kimura: in 4 of the records read" in report.as_text(briefing)


def test_a_briefing_cites_only_records_the_agent_read(project, model):
    """A citation stands only when it names a record the agent saw text of; a statement left with none is dropped;
    a person's node_id stands only when a tool named that node."""
    g = project

    def only_the_kickoff(turn, _results):
        if turn == 1:
            return [("find", {"name": "Harbor Survey"})]
        return [("read_item", {"item_id": g["items"]["m1"]})] if turn == 2 else "done"

    writer = ScriptedWriter(extra={
        "summary": [{"text": "Something no record says.", "cites": []}],
        "open_items": [{"item": "From a mail it never read.", "owner": None, "due": None,
                        "cites": [g["items"]["m3"]]}],
        "people": [{"node_id": g["theo"], "name": "Theo", "role": "named by no tool", "cites": [g["items"]["m3"]]},
                   {"node_id": 999_999, "name": "Dana", "role": "leads the sampling", "cites": [g["items"]["m1"]]}],
    })
    model(only_the_kickoff, writer)
    briefing = research.brief(QUESTION)["report"]
    assert [r["item_id"] for r in shown(writer.prompts[0])] == [g["items"]["m1"]]
    cited = {c for section in report.SECTIONS for e in briefing[section] for c in e["cites"]}
    assert cited == {g["items"]["m1"]} and [s["item_id"] for s in briefing["sources"]] == [g["items"]["m1"]]
    said = json.dumps(briefing)
    for dropped in ("Something no record says", "never read", "named by no tool"):
        assert dropped not in said
    assert briefing["people"] == [{"node_id": None, "name": "Dana", "role": "leads the sampling",
                                   "cites": [g["items"]["m1"]]}]
    assert briefing["checked"]["dropped"] == 3 and briefing["checked"]["invalid_citations"] == 2


def test_the_ledger_holds_what_the_tools_showed(project):
    """A record enters the ledger when a result shows its text (a snippet, an excerpt, the record), never from a
    timeline, which names records without their text; read in full, its text and senders replace its snippet."""
    g = project
    ledger = tools.Ledger()
    dated = tools.call("timeline", {"node_id": g["project"]})
    assert dated.ok and dated.answer["edges"] and not ledger.add(dated).items and ledger.nodes
    listed = tools.call("sources", {"node_id": g["project"], "limit": 20})
    learned = ledger.add(listed)
    assert [r.item_id for r in learned.items] == [g["items"][ref] for ref in ("m4", "m3", "m2", "m1")]
    full = ledger.add(tools.call("read_item", {"item_id": g["items"]["m3"]}))
    assert not full.items and full.passages == 1
    assert ledger.items[g["items"]["m3"]].texts == [
        "from: Mira Castellanos; to: Dana Kimura, Theo Marsh\nThanks both. I will send the draft Harbor Survey report "
        "by April 20."]
    assert not ledger.add(tools.call("read_item", {"item_id": g["items"]["m3"]}))  # read again: nothing new
    missing = tools.call("open_node", {"node_id": 99_999})
    assert not missing.ok and "99999" in missing.error and not ledger.add(missing)
    assert "no tool 'ask'" in tools.call("ask", {"question": "x"}).error  # not one of the agent's tools


def test_with_no_record_read_the_briefing_is_empty_and_no_model_writes_it(project, model):
    _agent, writer = model(lambda turn, _r: [("find", {"name": "nothing like it"})] if turn == 1 else "nothing")
    briefing = research.brief(QUESTION)["report"]
    assert briefing["sources"] == [] and briefing["checked"]["records_read"] == 0 and writer.prompts == []


# ---- runs: the routes, the cache, the MCP tool, the CLI ----

def wait(run_id: str) -> dict:
    for _ in range(600):
        with SessionLocal() as s:
            run = research.get(s, run_id)
        if run["status"] != "running":
            return run
        time.sleep(0.05)
    raise AssertionError("research never finished")


def test_post_research_runs_in_the_background_with_a_live_trace_cached_by_graph_version(project, model):
    agent, writer = model(project_plan)
    client = TestClient(app)
    started = client.post("/research", json={"question": QUESTION})
    assert started.status_code == 200, started.text
    run = started.json()
    assert run["status"] == "running" and run["cached"] is False and run["question"] == QUESTION
    assert wait(run["id"])["status"] == "succeeded"
    got = client.get(f"/research/{run['id']}").json()
    kinds = [s["kind"] for s in got["trace"]]
    assert kinds == ["turn", "tool", "turn", "tool", "tool", "tool", "turn"] + ["tool"] * 6 + ["turn", "stop", "report"]
    assert [s["at"] for s in got["trace"]] == sorted(s["at"] for s in got["trace"])
    assert got["report"]["timeline"] and got["usage"]["tool_calls"] == 10 and got["error"] is None
    turns = len(agent.sent)
    again = client.post("/research", json={"question": "  brief me on the HARBOR survey project "}).json()
    assert again["id"] == run["id"] and again["cached"] is True and again["status"] == "succeeded"
    assert len(agent.sent) == turns and len(writer.prompts) == 1  # answered without a model call
    with SessionLocal() as s:
        dirty.touch(s)  # a rebuild published, or a correction: the graph changed
        s.commit()
    newer = client.post("/research", json={"question": QUESTION}).json()
    assert newer["id"] != run["id"] and wait(newer["id"])["status"] == "succeeded"


def test_another_opt_in_is_another_briefing(project, model, monkeypatch):
    model(project_plan)
    first = research.run(QUESTION)
    assert research.run(QUESTION)["id"] == first["id"]
    monkeypatch.setattr(settings, "llm_sources", "gmail,drive")
    assert research.run(QUESTION)["id"] != first["id"]


def test_research_routes_refuse_what_they_cannot_do(committed_db, monkeypatch, model):
    model(lambda _turn, _r: "nothing")
    client = TestClient(app)
    monkeypatch.setattr(settings, "llm_sources", "")
    refused = client.post("/research", json={"question": QUESTION})
    assert refused.status_code == 422 and "IGNEOUS_LLM_SOURCES" in refused.json()["detail"]
    monkeypatch.setattr(settings, "llm_sources", "gmail")
    monkeypatch.setattr(llm, "unavailable", lambda: "model calls are turned off (IGNEOUS_LLM_ENABLED=false)")
    off = client.post("/research", json={"question": QUESTION})
    assert off.status_code == 503 and "IGNEOUS_LLM_ENABLED" in off.json()["detail"]
    assert client.post("/research", json={"question": "  "}).status_code == 422
    assert client.get("/research/nosuchrun").status_code == 404
    with SessionLocal() as s:
        assert s.execute(text("SELECT count(*) FROM research")).scalar() == 0


def test_a_failed_run_says_why_and_is_not_reused(project, model):
    model(failing(llm.ProviderUnavailable("Bedrock is rate-limiting")))
    failed = research.run(QUESTION)
    assert failed["status"] == "failed" and "rate-limiting" in failed["error"] and failed["report"] is None
    model(project_plan)
    assert research.run(QUESTION)["status"] == "succeeded"


def test_a_run_whose_process_stopped_is_failed_and_asked_again(project, model):
    model(project_plan)
    with SessionLocal() as s:
        s.execute(text("""INSERT INTO research (id, question, key, version, owner_host, owner_pid)
                          VALUES ('gone', :q, :k, :v, :h, 999999999)"""),
                  {"q": QUESTION, "k": research._key(QUESTION), "v": dirty.version(s), "h": jobs.HOST})
        s.commit()
    run = research.run(QUESTION)
    assert run["id"] != "gone" and run["status"] == "succeeded"
    with SessionLocal() as s:
        assert research.get(s, "gone")["status"] == "failed"
        s.execute(text("""INSERT INTO research (id, question, key, version, owner_host, owner_pid)
                          VALUES ('dead', 'q', 'k', 1, :h, 999999999), ('alive', 'q', 'k2', 1, :h, :p)"""),
                  {"h": jobs.HOST, "p": os.getpid()})
        s.commit()
        research._MINE.add("alive")
        try:
            assert research.fail_interrupted(s) == 1
        finally:
            research._MINE.discard("alive")
        assert research.get(s, "dead")["error"] == research.INTERRUPTED
        assert research.get(s, "alive")["status"] == "running"


def test_the_mcp_research_tool_returns_the_cited_briefing(project, model):
    agent, _ = model(project_plan, ScriptedWriter(about="Harbor Survey"))
    answer = json.loads(asyncio.run(mcp_server.server.call_tool("research", {"question": QUESTION})).content[0].text)
    assert answer["people"] and answer["timeline"] and answer["open_items"] and answer["sources"]
    assert answer["research"] == {"turns": 4, "tool_calls": 10, "seconds": answer["research"]["seconds"],
                                  "stopped": "answered"}
    assert answer["cached"] is False and answer["checked"]["invalid_citations"] == 0
    turns = len(agent.sent)
    again = json.loads(asyncio.run(mcp_server.server.call_tool("research", {"question": QUESTION})).content[0].text)
    assert again["cached"] is True and again["run_id"] == answer["run_id"] and len(agent.sent) == turns


def test_the_mcp_research_tool_says_why_it_cannot_run(project, model, monkeypatch):
    model(failing(llm.ProviderUnavailable("Bedrock is rate-limiting")))
    with pytest.raises(ToolError, match="research failed: Bedrock is rate-limiting"):
        asyncio.run(mcp_server.server.call_tool("research", {"question": QUESTION}))
    monkeypatch.setattr(llm, "unavailable", lambda: "no Bedrock key: set AWS_BEARER_TOKEN_BEDROCK")
    with pytest.raises(ToolError, match="research is unavailable: no Bedrock key"):
        asyncio.run(mcp_server.server.call_tool("research", {"question": QUESTION}))
    monkeypatch.setattr(settings, "llm_sources", "")
    with pytest.raises(ToolError, match="no source is opted in"):
        asyncio.run(mcp_server.server.call_tool("research", {"question": QUESTION}))


def test_the_cli_prints_each_step_and_the_briefing(project, model):
    from igneous.cli import app as cli

    model(project_plan, ScriptedWriter(about="Harbor Survey"))
    out = CliRunner().invoke(cli, ["research", QUESTION])
    assert out.exit_code == 0, out.output
    printed = click.unstyle(out.output)
    assert "  find(name='Harbor Survey') -> 1 results" in printed and "stopped: answered" in printed
    assert "  Dana Kimura: in 4 of the records read [i" in printed
    assert "10 statements, every one citing records the research read (4 of 4 read)" in printed
    cached = CliRunner().invoke(cli, ["research", QUESTION, "--json"])
    assert cached.exit_code == 0 and json.loads(cached.output)["cached"] is True


def test_the_mixed_graph_briefing_carries_nothing_private(test_db, model, monkeypatch):
    """On the graph that mixes gmail (opted in) with texts (never), a briefing on Atlas cites mail only."""
    monkeypatch.setattr(settings, "llm_sources", "gmail")
    g = build()
    try:
        def plan(turn, results):
            if turn == 1:
                return [("find", {"name": "Atlas"}), ("open_node", {"node_id": g["pat"]})]
            if turn == 2:
                return [("sources", {"node_id": g["atlas"], "limit": 20}), ("why_connected", {"a": g["me"],
                                                                                            "b": g["pat"]})]
            return "done"

        _agent, writer = model(plan)
        briefing = research.brief("Brief me on Atlas")["report"]
        assert briefing["sources"] and {s["source"] for s in briefing["sources"]} == {"gmail"}
        assert not [m for m in PRIVATE if m in json.dumps(briefing) + writer.prompts[0]]
    finally:
        wipe()
