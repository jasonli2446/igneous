"""The research agent's loop, and its runs.

`brief(question)` researches a question and writes the briefing. `explore` lets a hosted model read the graph through
the agent's tools (agent/tools.py), one turn at a time (llm.agent_turn), and keeps a ledger of what it read;
agent/report.py then writes the briefing from that ledger, every statement citing records the agent read.

The budget: 12 model turns, 30 tool calls, 200,000 tokens and 120 seconds, and the agent stops after 2 turns in a row
that learn nothing (no record read for the first time, no new passage of one, no node named for the first time).
Whatever stops it, the briefing is written from what it read. The conversation is append-only: each turn's content
goes in as the model sent it, then a result for each of its tool calls, and nothing earlier ever changes.

A run is a row in `research`. `start` runs it in the background (POST /research), `run` in the caller (the MCP tool,
the CLI). Its trace is written step by step as it happens, so GET /research/{id} shows it live and a reload or restart
still shows what happened; a run whose process stopped is marked failed. A finished briefing answers the same question
again, with no model call, while the graph stays at the version it read and the same sources, model and agent would
write it (`_key`).

Trace steps, each with `at` (seconds since the run started):
- {kind: "turn", turn, say, calls, tokens}: a model turn: what it said, how many tools it called, tokens so far;
- {kind: "tool", turn, tool, args, ok, note, read, seconds}: a tool call: its outcome in one line, the records it
  read for the first time ({item_id, title});
- {kind: "stop", reason, detail?}: why the research stopped: answered, turns, tool_calls, tokens, time, unproductive,
  max_tokens, refusal or model_error;
- {kind: "report", statements, kept, dropped, invalid_citations, records_read, records_cited}: the briefing written
  (agent/report.py's `checked`).
"""
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from igneous import jobs, llm
from igneous.agent import report, tools
from igneous.config import settings
from igneous.db import SessionLocal
from igneous.graph import dirty

log = logging.getLogger(__name__)

VERSION = 1  # the agent's prompts, tools and briefing; a change makes earlier briefings stale


@dataclass(frozen=True)
class Budget:
    turns: int = 12
    tool_calls: int = 30
    tokens: int = 200_000  # input and output, every turn's
    seconds: float = 120.0
    unproductive: int = 2  # turns in a row that learn nothing


BUDGET = Budget()
TURN_TIMEOUT = 30.0  # seconds a model turn may take at least, however little of the budget is left

SYSTEM = """You research a person's own records to brief them. Their email, chats, calendar, documents and AI chats
are joined into one graph of people, projects, trips, classes, organizations and group chats, and the tools read it.
The question names a person, a project, an organization or asks something: find what it is about, then gather what a
briefing needs:
- who is involved, and the part each plays;
- what happened when;
- what is still open: promises not yet kept, decisions or answers pending, next steps.

How to work:
- `find` turns a name into node_ids; `open_node` shows what a node is made of and what it is part of; `why_connected`
  says how two things are linked, with the records behind the link; `sources` lists a node's records, newest first;
  `read_item` reads one; `search` finds passages by what they say; `commitments` lists promises found in a node's
  records; `timeline` gives dated activity; `correspondents` ranks someone's relationships; `me` says who the user is.
- The briefing can cite only records you have seen text of: a search excerpt, a snippet among a node's records or in a
  link's evidence, a promise, a record you read. Read the records behind each point you will make, and the most
  important ones in full.
- Call several tools in one turn when they don't depend on each other. You have {turns} turns and {tool_calls} tool
  calls in all; don't repeat a call.
- A result's `withheld` counts records and names the user hasn't shared with you: never guess what they hold.
- When you have enough, or nothing more turns up, stop calling tools and write short notes: the people and their
  parts, the dated events and the open items, each with the item_ids that show it.

Today is {today}."""

NO_SOURCES = ("Research reads records with a hosted model, and no source is opted in for that. Add a source id to "
              "IGNEOUS_LLM_SOURCES in api/.env (for example gmail), restart, and ask again. The Sources page shows "
              "each source's id.")
INTERRUPTED = "Interrupted: the process running it stopped. Ask again."


class NothingShared(ValueError):
    """No source is opted in to the hosted model, so the agent could read nothing."""


@dataclass
class Exploration:
    """What the agent did: its ledger, its closing notes, why it stopped and what it used."""
    ledger: tools.Ledger
    notes: str = ""
    stopped: str = ""
    turns: int = 0
    tool_calls: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0


def _result(call: dict, content: str, error: bool = False) -> dict:
    out = {"type": "tool_result", "tool_use_id": call["id"], "content": content}
    return {**out, "is_error": True} if error else out


def explore(question: str, step: Callable[[dict], None], budget: Budget | None = None,
            clock: Callable[[], float] = time.monotonic) -> Exploration:
    """Let the model research `question` with the tools until it is done or the budget (BUDGET unless given) is
    spent; `step` gets each trace step. Raises the providers' errors when the first turn fails; a later failure stops
    the research there."""
    budget = budget or BUDGET
    start = clock()
    found = Exploration(tools.Ledger())
    system = SYSTEM.format(turns=budget.turns, tool_calls=budget.tool_calls, today=date.today().isoformat())
    offered = list(tools.definitions())
    history: list[dict] = [{"role": "user", "content": question}]
    idle = 0

    def spent() -> int:
        return sum(found.tokens.values())

    def stop(reason: str, detail: str | None = None) -> Exploration:
        found.stopped, found.seconds = reason, clock() - start
        step({"kind": "stop", "reason": reason, **({"detail": detail} if detail else {})})
        return found

    while True:
        left = budget.seconds - (clock() - start)
        if found.turns >= budget.turns:
            return stop("turns")
        if spent() >= budget.tokens:
            return stop("tokens")
        if left <= 0:
            return stop("time")
        try:
            turn = llm.agent_turn(system, history, offered, timeout=max(left, TURN_TIMEOUT))
        except llm.ProviderError as e:
            if found.turns == 0:
                raise
            return stop("model_error", str(e))
        found.turns += 1
        for k, v in turn.usage.items():
            found.tokens[k] = found.tokens.get(k, 0) + v
        history.append({"role": "assistant", "content": turn.content})
        calls = turn.tool_calls
        step({"kind": "turn", "turn": found.turns, "say": turn.text.strip()[:600] or None, "calls": len(calls),
              "tokens": spent()})
        if turn.stop_reason != "tool_use" or not calls:
            found.notes = turn.text.strip()
            return stop("answered" if turn.stop_reason in ("end_turn", "stop_sequence", None) else turn.stop_reason)
        results, learned = [], False
        for call in calls:
            if found.tool_calls >= budget.tool_calls or clock() - start >= budget.seconds:
                results.append(_result(call, "Not run: the research budget is spent.", error=True))
                continue
            found.tool_calls += 1
            outcome = tools.call(call["name"], call.get("input") or {})
            new = found.ledger.add(outcome)
            learned = learned or bool(new)
            step({"kind": "tool", "turn": found.turns, "tool": outcome.tool, "args": outcome.args, "ok": outcome.ok,
                  "note": outcome.note(), "read": [{"item_id": r.item_id, "title": r.title} for r in new.items],
                  "seconds": round(outcome.seconds, 2)})
            results.append(_result(call, outcome.content(), error=not outcome.ok))
        history.append({"role": "user", "content": results})
        idle = 0 if learned else idle + 1
        if idle >= budget.unproductive:
            return stop("unproductive")
        if found.tool_calls >= budget.tool_calls:
            return stop("tool_calls")


def brief(question: str, step: Callable[[dict], None] = lambda _step: None, budget: Budget | None = None) -> dict:
    """Research `question` and write the briefing: {report, usage}. `step` gets each trace step; `budget` is BUDGET
    unless given. Raises the providers' errors."""
    found = explore(question, step, budget)
    written, usage = report.write(question, found.ledger, found.notes)
    step({"kind": "report", **written["checked"]})
    return {"report": written, "usage": {
        "turns": found.turns, "tool_calls": found.tool_calls, "stopped": found.stopped,
        "seconds": round(found.seconds, 1), "tokens": found.tokens, "report_tokens": usage}}


# ---- runs ----

_MINE: set[str] = set()  # runs this process started


def _ready() -> None:
    """Raise NothingShared or LLMDisabled when a run could do nothing."""
    if not llm.allowed_sources():
        raise NothingShared(NO_SOURCES)
    if reason := llm.unavailable():
        raise llm.LLMDisabled(reason)


def _key(question: str) -> str:
    """What makes two briefings the same: the question (case and spacing aside), the sources the agent may read, the
    model and the agent."""
    basis = {"question": " ".join(question.split()).casefold(), "sources": sorted(llm.allowed_sources()),
             "model": settings.llm_model, "agent": VERSION}
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()


def _view(row, cached: bool = False) -> dict:
    keys = ("id", "question", "status", "version", "started_at", "finished_at", "trace", "report", "usage", "error")
    return {**{k: row[k] for k in keys}, "cached": cached}


def get(s, run_id: str) -> dict | None:
    """A run as GET /research/{id} shows it, or None."""
    row = s.execute(text("SELECT * FROM research WHERE id = :i"), {"i": run_id}).mappings().first()
    return _view(row) if row else None


def _open(question: str) -> tuple[dict, bool]:
    """The run that answers `question` on the graph as it is now: a finished briefing, a run under way, or a new run
    this process must do (True)."""
    _ready()
    key = _key(question)
    for _attempt in range(3):
        with SessionLocal() as s:
            version = dirty.version(s)
            row = s.execute(text("""
                SELECT * FROM research WHERE key = :k AND version = :v AND status <> 'failed'
                ORDER BY status = 'succeeded' DESC, started_at DESC LIMIT 1"""), {"k": key, "v": version}).mappings().first()
            if row and row["status"] == "running" and jobs.gone(row["owner_host"], row["owner_pid"], row["id"] in _MINE):
                s.execute(text("""UPDATE research SET status = 'failed', finished_at = now(), error = :e
                                  WHERE id = :i AND status = 'running'"""), {"i": row["id"], "e": INTERRUPTED})
                s.commit()
                continue
            if row:
                return _view(row, cached=row["status"] == "succeeded"), False
            run_id = uuid.uuid4().hex[:12]
            try:
                s.execute(text("""INSERT INTO research (id, question, key, version, owner_host, owner_pid)
                                  VALUES (:i, :q, :k, :v, :h, :p)"""),
                          {"i": run_id, "q": question, "k": key, "v": version, "h": jobs.HOST, "p": os.getpid()})
                s.commit()
            except IntegrityError:  # the same question started a moment ago: join that run
                s.rollback()
                continue
            _MINE.add(run_id)
            return get(s, run_id), True
    raise RuntimeError("couldn't start or find the research run for this question")


def _work(run_id: str, question: str, on_step: Callable[[dict], None] | None = None) -> None:
    started = time.monotonic()

    def step(s: dict) -> None:
        s = {"at": round(time.monotonic() - started, 1), **s}
        with SessionLocal() as ws:
            ws.execute(text("UPDATE research SET trace = trace || jsonb_build_array(CAST(:s AS jsonb)) WHERE id = :i"),
                       {"s": json.dumps(s, default=str), "i": run_id})
            ws.commit()
        if on_step is not None:
            on_step(s)

    status, out, error = "succeeded", {}, None
    try:
        out = brief(question, step)
    except llm.ProviderError as e:  # the model off, unreachable, or without a usable answer: say which
        status, error = "failed", str(e)[:500]
    except Exception as e:  # a crash: its details stay in the log
        log.exception("research run %s failed", run_id)
        status, error = "failed", f"research stopped on an error ({type(e).__name__}); its details are in the log"
    with SessionLocal() as s:
        s.execute(text("""
            UPDATE research SET status = :st, finished_at = now(), error = :e, report = CAST(:r AS jsonb),
                   usage = CAST(:u AS jsonb)
            WHERE id = :i"""), {"st": status, "e": error, "i": run_id,
                                "r": json.dumps(out["report"]) if "report" in out else None,
                                "u": json.dumps(out["usage"]) if "usage" in out else None})
        s.commit()
    _MINE.discard(run_id)


def start(question: str) -> dict:
    """POST /research: the briefing made on this graph for `question`, the run under way for it, or a new run in the
    background. Raises NothingShared, or LLMDisabled when the model is off or has no key."""
    found, new = _open(question)
    if new:
        threading.Thread(target=_work, args=(found["id"], question), daemon=True, name="research").start()
    return found


def run(question: str, on_step: Callable[[dict], None] | None = None) -> dict:
    """The finished run for `question`: the briefing made on this graph, the run under way for it once it ends, or a
    new run done here (`on_step` gets each step of its trace). Raises as `start` does."""
    found, new = _open(question)
    if new:
        _work(found["id"], question, on_step)
    deadline = time.monotonic() + BUDGET.seconds + 600
    while True:
        with SessionLocal() as s:
            now = get(s, found["id"])
        if now["status"] != "running" or time.monotonic() > deadline:
            return {**now, "cached": found["cached"]}
        time.sleep(0.5)


def fail_interrupted(s) -> int:
    """Mark failed the running research whose process stopped (an API restart or --reload). Safe to call from any
    process at any time."""
    rows = s.execute(text("SELECT id, owner_host, owner_pid FROM research WHERE status = 'running'")).all()
    dead = [r.id for r in rows if jobs.gone(r.owner_host, r.owner_pid, r.id in _MINE)]
    if dead:
        s.execute(text("""UPDATE research SET status = 'failed', finished_at = now(), error = :e
                          WHERE id = ANY(:ids) AND status = 'running'"""), {"ids": dead, "e": INTERRUPTED})
    s.commit()
    return len(dead)
