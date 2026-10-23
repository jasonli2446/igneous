"""One rule for every hosted model call (#179): text only from opted-in sources, names only of what appears in them.

A synthetic graph mixes an opted-in source (gmail) with one that never is (imessage, carrying PRIVATE markers).
Every model-calling stage runs against it with a recording provider in place of Bedrock.
"""
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from igneous import llm
from igneous.db import SessionLocal
from igneous.providers import ChatResult

PRIVATE = ("PRIVATEMARKER", "Pat Private", "+15550001111")
T0 = datetime(2025, 1, 15, tzinfo=UTC)
TABLES = ("chunks", "edge_evidence", "canonical", "edges", "nodes", "source_items", "llm_cache")


def _minimal(schema: dict):
    """The smallest answer that fits a JSON schema (first enum value, empty strings and lists, zeros)."""
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        return {k: _minimal(v) for k, v in schema.get("properties", {}).items()}
    return {"array": [], "boolean": False, "number": 0, "integer": 0}.get(kind, "")


class Recorder:
    def __init__(self):
        self.prompts: list[str] = []

    def __call__(self, messages, schema=None, **_options):
        self.prompts.append("\n".join(str(m["content"]) for m in messages))
        return ChatResult(json.dumps(_minimal(schema or {})), {})

    def leaked(self) -> list[str]:
        return [marker for p in self.prompts for marker in PRIVATE if marker in p]


def _wipe(s):
    for table in ("edge_verdicts", "edges_dropped", "thread_registry", "thread_parents"):
        if s.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar():
            s.execute(text(f"TRUNCATE {table}"))
    s.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
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


def _item(s, source, external_id, title, body, when) -> int:
    return s.execute(text("""
        INSERT INTO source_items (source, external_id, content_hash, title, body_text, occurred_at)
        VALUES (:s, :e, :e, :t, :b, :w) RETURNING id"""),
        {"s": source, "e": external_id, "t": title, "b": body, "w": when}).scalar_one()


@pytest.fixture
def graph(test_db):
    """Two threads (Atlas, Atlas platform) with mail from Sam (gmail) and texts from Pat (imessage), over 8 months."""
    with SessionLocal() as s:
        _wipe(s)
        g: dict = {"items": {}}
        g["me"] = _node(s, "Person", "email:me@shared.test", "Me Myself", {"self": True})
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
            _edge(s, gm, thread, "PART_OF", "threads", item=gid)
            iid = _item(s, "imessage", f"i{i}", "Chat with Pat Private",
                        f"PRIVATEMARKER Robotics Lab and Atlas plans with Pat Private, day {i}", when)
            im = _node(s, "Message", f"imessage:i{i}", "Chat with Pat Private", item=iid)
            _edge(s, g["pat"], im, "SENT", item=iid)
            _edge(s, im, g["me"], "RECEIVED", item=iid)
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
    yield g
    with SessionLocal() as s:
        _wipe(s)


def _run_model_stages(monkeypatch, sources: str) -> Recorder:
    from igneous.graph import profile, resolve_threads, threads, verify

    rec = Recorder()
    monkeypatch.setattr(llm, "chat", rec)
    monkeypatch.setattr(llm.settings, "llm_enabled", True)
    monkeypatch.setattr(llm.settings, "llm_sources", sources)
    monkeypatch.setattr(llm.settings, "local_sources_model_enabled", False)
    with SessionLocal() as s:
        verify.run(s, log=None)
        resolve_threads.run(s, log=None)
        threads.umbrellas(s, ["thread:atlas", "thread:atlas-platform", "thread:pat-only"])
        profile.run(s, log=None)
        s.commit()
    return rec


def test_visible_nodes_are_those_grounded_in_opted_in_records(graph):
    g = graph
    ids = [g["me"], g["sam"], g["sam2"], g["pat"], g["atlas"], g["platform"], g["pat_only"], g["items"]["g0"],
           g["items"]["i0"]]
    with SessionLocal() as s:
        assert llm.visible_nodes(s, ids, frozenset({"gmail"})) == {
            g["me"], g["sam"], g["sam2"], g["atlas"], g["platform"], g["items"]["g0"]}
        assert llm.visible_nodes(s, ids, frozenset()) == set()
        assert g["pat"] in llm.visible_nodes(s, ids, frozenset({"imessage"}))


def test_nothing_opted_in_means_no_hosted_call(graph, monkeypatch):
    rec = _run_model_stages(monkeypatch, "")
    assert rec.prompts == []


def test_prompts_carry_only_opted_in_records(graph, monkeypatch):
    rec = _run_model_stages(monkeypatch, "gmail")
    joined = "\n".join(rec.prompts)
    # Each stage asked about what it may show...
    assert "Robotics Lab: Atlas update" in joined          # verifier and thread-pair evidence
    assert "Term: Robotics Lab" in joined                  # umbrella check
    assert "Sam Shared" in joined                          # names that appear in opted-in mail
    # ...and nothing from the texts: no snippet, no name, no number, no correction about Pat, no local resume.
    assert rec.leaked() == []
    assert "resume.pdf" not in joined and "Resume 2026" not in joined  # drive isn't opted in either


def test_opting_in_a_document_source_lets_its_resume_be_read(graph, monkeypatch):
    rec = _run_model_stages(monkeypatch, "gmail,drive")
    assert any("Robotics Lab research assistant" in p and "PRIVATEMARKER" not in p for p in rec.prompts)
    assert rec.leaked() == []


def test_a_cached_resume_still_counts_with_the_model_off(graph, monkeypatch):
    """Turning the model off used to drop the resume backbone on the next rebuild (extract returned early)."""
    import hashlib

    from igneous.graph import profile

    with SessionLocal() as s:
        doc = s.execute(text("SELECT id, source, title, occurred_at, body_text FROM source_items "
                             "WHERE external_id = 'drive/resume'")).one()
        key = "profile-extract:v3:" + hashlib.sha256(doc.body_text.encode()).hexdigest()
        s.execute(text("INSERT INTO llm_cache (cache_key, response) VALUES (:k, CAST(:r AS jsonb))"),
                  {"k": key, "r": json.dumps({"is_about_owner": True, "person_name": "Me Myself"})})
        s.commit()
        monkeypatch.setattr(llm.settings, "llm_enabled", False)
        assert profile.extract(s, doc)["person_name"] == "Me Myself"


def test_lightrag_export_holds_only_opted_in_records(graph, monkeypatch):
    from igneous.interop.lightrag import export

    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    with SessionLocal() as s:
        kg = export(s)
    assert kg["chunks"] and all(c["file_path"].startswith("gmail/") for c in kg["chunks"])
    assert "PRIVATEMARKER" not in json.dumps(kg) and "Pat Private" not in json.dumps(kg)
    monkeypatch.setattr(llm.settings, "llm_sources", "")
    with SessionLocal() as s:
        assert export(s)["chunks"] == []


def test_whole_corpus_tools_refuse_unless_every_source_is_opted_in(graph, monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    with SessionLocal() as s, pytest.raises(SystemExit, match="imessage"):
        llm.require_whole_corpus(s)
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail,imessage,drive,fs")
    monkeypatch.setattr(llm.settings, "local_sources_model_enabled", True)
    with SessionLocal() as s:
        llm.require_whole_corpus(s)


def test_sources_page_says_what_a_model_may_read(test_db, monkeypatch):
    from fastapi.testclient import TestClient

    from igneous.main import app

    monkeypatch.setattr(llm.settings, "llm_sources", "gmail,takeout_maps,fs")
    monkeypatch.setattr(llm.settings, "local_sources_model_enabled", False)
    body = TestClient(app).get("/sources").json()
    by_id = {row["id"]: row["model"] for row in body["sources"]}
    assert body["model"]["sources"] == ["gmail", "takeout_maps"]
    assert by_id["gmail"]["allowed"] == "yes"
    assert by_id["takeout"]["allowed"] == "partly"
    assert by_id["chatgpt"]["id"] == "ai_chats" and by_id["chatgpt"]["allowed"] == "no"
    assert by_id["fs"]["allowed"] == "no" and "IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED" in by_id["fs"]["note"]
    assert "IGNEOUS_LLM_SOURCES" in by_id["imessage"]["note"]


def test_thread_namer_sees_only_terms_its_shown_records_contain(monkeypatch):
    """Shared terms are mined from every source; a term found only in texts never reaches the model."""
    from types import SimpleNamespace

    from igneous.graph import threads

    rec = Recorder()
    monkeypatch.setattr(llm, "chat", rec)
    monkeypatch.setattr(llm.settings, "llm_enabled", True)
    session = SimpleNamespace(execute=lambda *_a, **_k: SimpleNamespace(scalar=lambda: None))
    when = datetime(2025, 3, 1, tzinfo=UTC)
    c = {"terms": ["Atlas", "Zephyr"], "start": when, "end": when, "sources": {"gmail": 1, "imessage": 1},
         "items": {1: SimpleNamespace(id=1, source="gmail", title="Atlas kickoff", body="Atlas plan", occurred_at=when),
                   2: SimpleNamespace(id=2, source="imessage", title="Chat", body="PRIVATEMARKER Zephyr",
                                      occurred_at=when)}}
    monkeypatch.setattr(llm.settings, "llm_sources", "")
    assert threads._judge(session, c)["judged"] is False and rec.prompts == []
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    threads._judge(session, c)
    assert len(rec.prompts) == 1 and "Shared terms: Atlas\n" in rec.prompts[0]
    assert "Zephyr" not in rec.prompts[0] and rec.leaked() == []
