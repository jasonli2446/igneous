"""A scripted stand-in for the hosted model in the research agent: no key, no network.

`ScriptedAgent` replaces `llm.tool_turn`. Each turn it asks its plan what to do next, given the tool results so far:
a list of (tool, arguments) to call, or a string to end with. Every turn it sends starts with a thinking block that
carries a signature, as a current model's turns do, so a test can check that the conversation hands it back unchanged.
It keeps every conversation it was sent.

`ScriptedWriter` replaces `llm.structured` for the briefing: it cites what the prompt shows, by rules, and can add
statements a test expects to be dropped. Both keep every prompt they were sent, for the tests of what a prompt carries.
"""
import copy
import json
import re
from collections.abc import Callable

from igneous.providers import TurnResult

Plan = Callable[[int, list[dict]], list[tuple[str, dict]] | str]  # (turn, results of the last turn's calls)


def last_results(messages: list[dict]) -> list[dict]:
    """The answers to the last turn's tool calls, parsed ({"error": text} for an error)."""
    if not messages or messages[-1]["role"] != "user" or isinstance(messages[-1]["content"], str):
        return []
    out = []
    for block in messages[-1]["content"]:
        out.append({"error": block["content"]} if block.get("is_error") else json.loads(block["content"]))
    return out


class ScriptedAgent:
    def __init__(self, plan: Plan, usage: dict | None = None):
        self.plan = plan
        self.usage = usage or {"input_tokens": 1000, "output_tokens": 100}
        self.sent: list[dict] = []  # {system, messages, tools, options} of each turn

    def __call__(self, system: str, messages: list[dict], tools: list[dict], **options) -> TurnResult:
        self.sent.append({"system": system, "messages": copy.deepcopy(messages), "tools": tools, "options": options})
        turn = len(self.sent)
        decision = self.plan(turn, last_results(messages))
        thinking = {"type": "thinking", "thinking": "", "signature": f"signature-of-turn-{turn}"}
        if isinstance(decision, str):
            return TurnResult([thinking, {"type": "text", "text": decision}], "end_turn", dict(self.usage))
        calls = [{"type": "tool_use", "id": f"toolu_{turn}_{i}", "name": name, "input": args}
                 for i, (name, args) in enumerate(decision)]
        return TurnResult([thinking, {"type": "text", "text": f"Turn {turn}: looking."}, *calls], "tool_use",
                          dict(self.usage))

    def prompts(self) -> list[str]:
        """Everything each turn sent the model, as text."""
        return [s["system"] + "\n" + json.dumps(s["messages"], ensure_ascii=False) for s in self.sent]


RECORD = re.compile(r"^\[i(\d+)\] \(([^)]*)\) ?(.*)$", re.M)
NODE = re.compile(r"^node_id (\d+): (.+?)(?: \((\w[\w ]*)\))?$", re.M)


def shown(prompt: str) -> list[dict]:
    """The records a briefing prompt shows: {item_id, date, title}."""
    out = []
    for m in RECORD.finditer(prompt):
        head = m.group(2).split(", ")
        out.append({"item_id": int(m.group(1)), "date": head[1] if len(head) > 1 else None, "title": m.group(3)})
    return out


class ScriptedWriter:
    """Writes a briefing from the prompt by rules: a summary citing the first and last records shown, each person the
    prompt names whose first name a shown record holds, one timeline entry per record, and an open item for each
    record that says what someone will do. With `about`, only the records that mention it count. `extra` entries are
    added as they are (statements a check must drop)."""

    def __init__(self, extra: dict | None = None, about: str | None = None):
        self.extra = extra or {}
        self.about = about
        self.prompts: list[str] = []

    def __call__(self, system: str, user: str, schema: dict, **_options) -> tuple[dict, dict]:
        self.prompts.append(system + "\n" + user)
        records = shown(user)
        blocks = {r["item_id"]: block for r, block in zip(records, re.split(r"\n\n---\n\n", user.split(
            "Records read:\n\n", 1)[1]), strict=False)}
        if self.about:
            records = [r for r in records if self.about.casefold() in blocks[r["item_id"]].casefold()]
            blocks = {r["item_id"]: blocks[r["item_id"]] for r in records}
        out: dict = {"summary": [], "people": [], "timeline": [], "open_items": []}
        if records:
            first, last = records[0], records[-1]
            out["summary"].append({"text": f"{len(records)} records, from {first['title']} to {last['title']}.",
                                   "cites": list(dict.fromkeys([first["item_id"], last["item_id"]]))})
        for m in NODE.finditer(user):
            if m.group(3) != "Person":
                continue
            holding = [i for i, block in blocks.items() if m.group(2).split()[0] in block]
            if holding:
                out["people"].append({"node_id": int(m.group(1)), "name": m.group(2),
                                      "role": f"in {len(holding)} of the records read", "cites": holding[:3]})
        for r in records:
            if r["date"]:
                out["timeline"].append({"date": r["date"], "event": r["title"] or "a record", "cites": [r["item_id"]]})
            for sentence in re.findall(r"[^.!?\n]*(?:\bwill\b|'ll\b|\bnext steps?\b)[^.!?\n]*", blocks[r["item_id"]],
                                       re.I):
                out["open_items"].append({"item": sentence.strip(), "owner": None, "due": None,
                                          "cites": [r["item_id"]]})
                break
        for section, entries in self.extra.items():
            out[section] = out[section] + entries
        return out, {"input_tokens": 2000, "output_tokens": 300}
