"""A graph that mixes an opted-in source (gmail) with one that never is (imessage, whose records and the person
only they name carry the PRIVATE markers), for the tests of the rule every outbound path follows: hosted model
prompts (test_model_policy.py) and MCP tool results (test_mcp_policy.py)."""
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from igneous.db import SessionLocal
from igneous.providers import ChatResult

PRIVATE = ("PRIVATEMARKER", "Pat Private", "+15550001111")
T0 = datetime(2025, 1, 15, tzinfo=UTC)
TABLES = ("chunks", "edge_evidence", "canonical", "edges", "nodes", "source_items", "llm_cache")


def _minimal(schema: dict):
    """The smallest answer that fits a JSON schema (first enum value or alternative, empty strings and lists,
    zeros)."""
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return None if {"type": "null"} in schema["anyOf"] else _minimal(schema["anyOf"][0])
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = kind[0]
    if kind == "object":
        return {k: _minimal(v) for k, v in schema.get("properties", {}).items()}
    return {"array": [], "boolean": False, "number": 0, "integer": 0}.get(kind, "")


class Recorder:
    def __init__(self):
        self.prompts: list[str] = []

    def __call__(self, messages, schema=None, **_options):
        self.prompts.append("\n".join(str(m["content"]) for m in messages))
        return ChatResult(json.dumps(_minimal(schema or {})), {"input_tokens": 0, "output_tokens": 0})

    def leaked(self) -> list[str]:
        return [marker for p in self.prompts for marker in PRIVATE if marker in p]


def _wipe(s):
    s.execute(text(f"TRUNCATE edge_verdicts, edges_dropped, thread_registry, thread_parents, overrides, "
                   f"{', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    s.commit()


def _node(s, type_, key, name, props=None, item=None) -> int:
    return s.execute(text("""
        INSERT INTO nodes (type, key, name, props, source_item_id) VALUES (:t, :k, :n, CAST(:p AS jsonb), :i)
        RETURNING id"""), {"t": type_, "k": key, "n": name, "p": json.dumps(props or {}), "i": item}).scalar_one()


def _edge(s, src, dst, type_, extractor="spine", item=None) -> int:
    return s.execute(text("""
        INSERT INTO edges (src, dst, type, extractor, extractor_version, source_item_id)
        VALUES (:a, :b, :t, :x, '1', :i) RETURNING id"""),
        {"a": src, "b": dst, "t": type_, "x": extractor, "i": item}).scalar_one()


def _named(s, node, item, name) -> None:
    """Record that `item` gives `node` the name `name`, as the spine does for every person a record names (a mail
    header's display name, a chat's handle)."""
    s.execute(text("""INSERT INTO spine_node_support (node_id, source_item_id, name, rank) VALUES (:n, :i, :name, 3)
                      ON CONFLICT DO NOTHING"""), {"n": node, "i": item, "name": name})


def _item(s, source, external_id, title, body, when) -> int:
    return s.execute(text("""
        INSERT INTO source_items (source, external_id, content_hash, title, body_text, occurred_at)
        VALUES (:s, :e, :e, :t, :b, :w) RETURNING id"""),
        {"s": source, "e": external_id, "t": title, "b": body, "w": when}).scalar_one()


def build() -> dict:
    """Two threads (Atlas, Atlas platform) with mail from Sam (gmail) and texts from Pat (imessage), over 8 months."""
    with SessionLocal() as s:
        _wipe(s)
        g: dict = {"items": {}}
        g["me"] = _node(s, "Person", "me", "Me Myself", {"self": True})  # the owner, as spine and self create it
        g["sam"] = _node(s, "Person", "email:sam@shared.test", "Sam Shared")
        g["sam2"] = _node(s, "Person", "email:sam.shared@other.test", "Sam Shared")
        g["pat"] = _node(s, "Person", "phone:+15550001111", "Pat Private")
        span = {"start": T0.isoformat(), "end": (T0 + timedelta(days=240)).isoformat()}
        g["atlas"] = _node(s, "Project", "thread:atlas", "Atlas", {"derived": "threads", "kind": "project", **span})
        g["platform"] = _node(s, "Project", "thread:atlas-platform", "Atlas platform",
                              {"derived": "threads", "kind": "project", **span})
        g["pat_only"] = _node(s, "Project", "thread:pat-only", "Pat trip",
                              {"derived": "threads", "kind": "trip", **span})
        for i in range(8):
            when = T0 + timedelta(days=30 * i)
            thread = g["atlas"] if i < 4 else g["platform"]
            gid = _item(s, "gmail", f"g{i}", f"Robotics Lab: Atlas update {i}",
                        f"Atlas status from the Robotics Lab, week {i}. Next steps for the demo.", when)
            gm = _node(s, "Message", f"gmail:g{i}", f"Robotics Lab: Atlas update {i}", item=gid)
            _edge(s, g["sam"], gm, "SENT", item=gid)
            _edge(s, gm, g["me"], "RECEIVED", item=gid)
            _named(s, g["sam"], gid, "Sam Shared")
            _named(s, g["me"], gid, "Me Myself")
            _edge(s, gm, thread, "PART_OF", "threads", item=gid)
            iid = _item(s, "imessage", f"i{i}", "Chat with Pat Private",
                        f"PRIVATEMARKER Robotics Lab and Atlas plans with Pat Private, day {i}", when)
            im = _node(s, "Message", f"imessage:i{i}", "Chat with Pat Private", item=iid)
            _edge(s, g["pat"], im, "SENT", item=iid)
            _edge(s, im, g["me"], "RECEIVED", item=iid)
            _named(s, g["pat"], iid, "Pat Private")
            _edge(s, im, thread, "PART_OF", "threads", item=iid)
            _edge(s, im, g["pat_only"], "PART_OF", "threads", item=iid)
            s.execute(text("INSERT INTO chunks (source_item_id, node_id, ord, text, meta) VALUES "
                           "(:i, :n, 0, :t, '{}'), (:j, :m, 0, :u, '{}')"),
                      {"i": gid, "n": gm, "t": f"Atlas status {i}", "j": iid, "m": im, "u": f"PRIVATEMARKER {i}"})
            _edge(s, gm, g["sam"], "MENTIONS", "llm")
            _edge(s, im, g["pat"], "MENTIONS", "llm")
            g["items"][f"g{i}"], g["items"][f"i{i}"] = gm, im
        s2 = _item(s, "gmail", "g-other", "Hello from my other address", "Same Sam, other address.", T0)
        m2 = _node(s, "Message", "gmail:g-other", "Hello from my other address", item=s2)
        _edge(s, g["sam2"], m2, "SENT", item=s2)
        _edge(s, m2, g["me"], "RECEIVED", item=s2)
        _named(s, g["sam2"], s2, "Sam Shared")
        for person in ("sam", "pat"):
            _edge(s, g[person], g["atlas"], "INVOLVED_IN", "threads")
            _edge(s, g[person], g["platform"], "INVOLVED_IN", "threads")
        _edge(s, g["pat"], g["pat_only"], "INVOLVED_IN", "threads")
        _edge(s, g["sam"], g["sam2"], "SAME_AS", "er")
        _edge(s, g["sam"], g["pat"], "SAME_AS", "er")
        # Resumes: a local file (never opted in here) and a Drive document.
        resume = "Experience\n" + "Robotics Lab research assistant, 2024-2025. " * 12
        _item(s, "fs", "home/resume.pdf", "resume.pdf", "PRIVATEMARKER " + resume, T0)
        _item(s, "drive", "drive/resume", "Resume 2026", resume, T0)
        s.commit()
        # Corrections the user made: one about Pat (who appears only in texts), one about Sam.
        for who, reason in (("phone:+15550001111", "Pat Private was never on it"), ("email:sam@shared.test", "yes")):
            s.execute(text("""INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, reason, source)
                              VALUES (:a, 'Project:thread:atlas', 'INVOLVED_IN', 'threads', 'user', 'drop', :r, 'user')"""),
                      {"a": f"Person:{who}", "r": reason})
        s.commit()
    return g


def wipe() -> None:
    with SessionLocal() as s:
        _wipe(s)


