"""Derived-graph exports carry real, opted-in evidence, including through LightRAG's actual loader (#191)."""
import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import text

from igneous import llm, providers
from igneous.db import SessionLocal
from igneous.interop.lightrag import export, write


@pytest.fixture
def s(test_db, monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    monkeypatch.setattr(llm.settings, "local_sources_model_enabled", False)

    def forbidden(*_a, **_kw):
        raise AssertionError("export must not call a model")

    monkeypatch.setattr(llm, "structured", forbidden)
    monkeypatch.setattr(providers, "chat", forbidden)
    with SessionLocal() as session:
        session.execute(text("TRUNCATE nodes, source_items, canonical RESTART IDENTITY CASCADE"))
        yield session
        session.rollback()


def node(s, kind, name, *, key=None, props=None, item=None):
    return s.execute(text("""
        INSERT INTO nodes (type,key,name,props,source_item_id) VALUES (:t,:k,:n,CAST(:p AS jsonb),:i) RETURNING id
    """), {"t": kind, "k": key or name, "n": name, "p": json.dumps(props or {}), "i": item}).scalar_one()


def record(s, body, *, source="gmail", external="record", chunks=None, year=2025):
    sid = s.execute(text("""
        INSERT INTO source_items (source,external_id,content_hash,title,body_text,occurred_at)
        VALUES (:s,:e,:e,:e,:b,:at) RETURNING id
    """), {"s": source, "e": external, "b": body, "at": datetime(year, 1, 1, tzinfo=UTC)}).scalar_one()
    mid = node(s, "Message", external, item=sid)
    for i, content in enumerate([body] if chunks is None else chunks):
        s.execute(text("INSERT INTO chunks (source_item_id,node_id,ord,text) VALUES (:s,:n,:i,:c)"),
                  {"s": sid, "n": mid, "i": i, "c": content})
    return sid, mid


def edge(s, a, b, kind, *, item=None, evidence=(), props=None, span=None):
    eid = s.execute(text("""
        INSERT INTO edges (src,dst,type,extractor,source_item_id,props,span_start,span_end)
        VALUES (:a,:b,:t,'fixture',:i,CAST(:p AS jsonb),:start,:end) RETURNING id
    """), {"a": a, "b": b, "t": kind, "i": item, "p": json.dumps(props or {}),
           "start": span[0] if span else None, "end": span[1] if span else None}).scalar_one()
    for sid in evidence:
        s.execute(text("INSERT INTO edge_evidence (edge_id,source_item_id,kind,detail) VALUES (:e,:s,'fixture','{}')"),
                  {"e": eid, "s": sid})
    return eid


def derived_graph(s):
    sid, message = record(s, "Sam Lee worked on Widget Tracker at Example University. "
                          "Pat Example introduced Sam Lee to Alex Example. We decided to release Friday; Sam will ship it.")
    sam = node(s, "Person", "Sam Lee")
    pat = node(s, "Person", "Pat Example")
    alex = node(s, "Person", "Alex Example")
    project = node(s, "Project", "Widget Tracker")
    org = node(s, "Organization", "Example University")
    group = node(s, "Thread", "Widget group")
    promise = node(s, "Commitment", "Ship Widget Tracker")
    decision = node(s, "Decision", "Release Friday")
    edge(s, pat, message, "SENT", item=sid)
    edge(s, sam, project, "INVOLVED_IN", evidence=[sid])
    edge(s, group, project, "PART_OF", item=sid)
    edge(s, project, org, "PART_OF", evidence=[sid])
    edge(s, sam, org, "WORKED_AT", item=sid)
    edge(s, sam, alex, "INTRODUCED", item=sid, props={"introducer": pat})
    edge(s, message, promise, "HAS_COMMITMENT", item=sid)
    edge(s, message, decision, "HAS_DECISION", item=sid)
    return sid, sam, project, org


def assert_contract(kg):
    refs = {c["source_id"] for c in kg["chunks"]}
    names = {e["entity_name"] for e in kg["entities"]}
    assert len(refs) == len(kg["chunks"]) and len(names) == len(kg["entities"])
    assert all(e["source_id"] in refs for e in kg["entities"])
    assert all(r["source_id"] in refs and r["src_id"] in names and r["tgt_id"] in names
               and r["src_id"] != r["tgt_id"] for r in kg["relationships"])
    pairs = [tuple(sorted((r["src_id"], r["tgt_id"]))) for r in kg["relationships"]]
    assert len(set(pairs)) == len(pairs)


def test_exports_projects_hierarchy_profile_and_history_with_evidence(s, tmp_path):
    derived_graph(s)
    before = s.execute(text("SELECT count(*) FROM chunks")).scalar()
    kg = export(s)
    assert_contract(kg)
    assert {e["entity_type"] for e in kg["entities"]} >= {
        "person", "project", "organization", "thread", "commitment", "decision"}
    assert len(kg["relationships"]) == 7
    assert any("Sam Lee took part in Widget Tracker" in r["description"] for r in kg["relationships"])
    assert any("Widget Tracker is part of Example University" in r["description"] for r in kg["relationships"])
    assert any("were introduced by Pat Example" in r["description"] for r in kg["relationships"])
    assert "email archive" not in json.dumps(kg) and "email(s)" not in json.dumps(kg)
    assert export(s) == kg  # deterministic ordering, no cache writes
    out = tmp_path / "kg.json"
    assert write(s, out) == {k: len(v) for k, v in kg.items()}
    assert json.loads(out.read_text()) == kg
    assert s.execute(text("SELECT count(*) FROM chunks")).scalar() == before


def test_private_hidden_dropped_and_unsupported_links_are_not_exported(s):
    sid, sam, project, org = derived_graph(s)
    private, _ = record(s, "PRIVATEMARKER body", source="imessage", external="private")
    secret = node(s, "Project", "PRIVATEMARKER project")
    hidden = node(s, "Project", "Hidden project", props={"hidden": True})
    dropped = node(s, "Project", "Dropped project")
    archived = node(s, "Project", "Archived project")
    unsupported = node(s, "Project", "Unsupported project")
    edge(s, sam, secret, "INVOLVED_IN", item=private)
    edge(s, sam, hidden, "INVOLVED_IN", item=sid)
    edge(s, sam, dropped, "INVOLVED_IN", item=sid, props={"verdict": "drop"})
    eid = edge(s, sam, archived, "INVOLVED_IN", item=sid)
    s.execute(text("INSERT INTO edges_dropped SELECT * FROM edges WHERE id=:i"), {"i": eid})
    s.execute(text("DELETE FROM edges WHERE id=:i"), {"i": eid})
    edge(s, sam, unsupported, "INVOLVED_IN")
    # More evidence and arbitrary free-text props must not leak content from another source.
    edge(s, project, org, "RELATED_TO", item=private, props={"reason": "PRIVATEMARKER reason"})
    mixed = node(s, "Project", "Mixed project")
    edge(s, sam, mixed, "INVOLVED_IN", item=private, evidence=[sid], props={"reason": "PRIVATEMARKER reason"})
    blob = json.dumps(export(s))
    for marker in ("PRIVATEMARKER", "Hidden project", "Dropped project", "Archived project", "Unsupported project"):
        assert marker not in blob
    assert "Mixed project" in blob


def test_introducer_name_must_itself_be_visible_in_opted_in_records(s):
    sid, sam, _p, _o = derived_graph(s)
    private, _ = record(s, "PRIVATEMARKER", source="imessage", external="private")
    hidden_intro = node(s, "Person", "PRIVATEMARKER introducer", item=private)
    other = node(s, "Person", "Other Person")
    edge(s, sam, other, "INTRODUCED", item=sid, props={"introducer": hidden_intro})
    assert "PRIVATEMARKER" not in json.dumps(export(s))


def test_limit_uses_newest_eligible_records_and_rejects_invalid_limits(s):
    a = node(s, "Person", "Sam Lee")
    for year, source in ((2024, "gmail"), (2025, "gmail"), (2026, "imessage")):
        b = node(s, "Project", f"Widget Tracker {year}")
        sid, _ = record(s, f"Plans for {year}", external=str(year), source=source, year=year)
        edge(s, a, b, "INVOLVED_IN", item=sid)
    kg = export(s, limit_messages=1)
    assert len(kg["chunks"]) == 1 and kg["chunks"][0]["file_path"] == "gmail/2025"
    assert len(kg["relationships"]) == 1
    for invalid in (0, -1):
        with pytest.raises(ValueError, match="at least 1"):
            export(s, invalid)


def test_unindexed_profile_evidence_gets_read_only_export_chunks(s, monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_sources", "linkedin")
    sid = s.execute(text("""INSERT INTO source_items (source,external_id,content_hash,title,body_text)
                            VALUES ('linkedin','profile','v1','Your LinkedIn','Sam Lee: Engineer at Acme Corp')
                            RETURNING id""")).scalar_one()
    a, b = node(s, "Person", "Sam Lee"), node(s, "Organization", "Acme Corp")
    edge(s, a, b, "WORKED_AT", item=sid)
    kg = export(s)
    assert_contract(kg)
    assert kg["chunks"][0]["content"] == "Sam Lee: Engineer at Acme Corp"
    assert kg["chunks"][0]["file_path"] == "linkedin/profile"
    assert s.execute(text("SELECT count(*) FROM chunks")).scalar() == 0


def test_span_uses_the_matching_chunk_instead_of_the_first(s):
    quote = "Sam will ship on Friday."
    body = "Introduction\n\n" + quote
    sid, message = record(s, body, chunks=["Introduction", quote])
    promise = node(s, "Commitment", "Ship Friday")
    edge(s, message, promise, "HAS_COMMITMENT", item=sid, span=(body.index(quote), len(body)))
    kg = export(s)
    assert len(kg["chunks"]) == 1 and kg["chunks"][0]["content"] == quote
    assert kg["chunks"][0]["chunk_order_index"] == 1


def test_profile_citation_prefers_the_chunk_naming_the_employer(s):
    sid, _ = record(s, "Sam Lee's resume\n\nEngineer at Acme Corp", chunks=["Sam Lee's resume", "Engineer at Acme Corp"])
    a, b = node(s, "Person", "Sam Lee"), node(s, "Organization", "Acme Corp")
    edge(s, a, b, "WORKED_AT", item=sid)
    kg = export(s)
    assert kg["chunks"][0]["content"] == "Engineer at Acme Corp"


def test_empty_opt_in_and_local_source_switch(s, monkeypatch):
    derived_graph(s)
    monkeypatch.setattr(llm.settings, "llm_sources", "")
    assert export(s) == {"chunks": [], "entities": [], "relationships": []}
    monkeypatch.setattr(llm.settings, "llm_sources", "fs")
    sid, _ = record(s, "Sam Lee built Widget Tracker", source="fs", external="resume.md")
    a, b = node(s, "Person", "Owner"), node(s, "Project", "Local Project")
    edge(s, a, b, "INVOLVED_IN", item=sid)
    assert not export(s)["chunks"]
    monkeypatch.setattr(llm.settings, "local_sources_model_enabled", True)
    assert export(s)["chunks"][0]["file_path"] == "fs/resume.md"


def test_aliases_fold_but_same_named_project_and_org_stay_distinct(s):
    sid, _ = record(s, "Sam Lee worked at the Lab and built its Lab project.")
    head = node(s, "Person", "Sam Lee", item=sid)
    alias = node(s, "Person", "samlee", key="github:samlee")
    s.execute(text("INSERT INTO canonical VALUES (:a,:b)"), {"a": alias, "b": head})
    project = node(s, "Project", "Lab")
    org = node(s, "Organization", "Lab")
    edge(s, alias, project, "INVOLVED_IN", item=sid)
    edge(s, project, org, "PART_OF", item=sid)
    kg = export(s)
    assert_contract(kg)
    assert len(kg["entities"]) == 3
    assert sum(e["entity_name"] == "Sam Lee" for e in kg["entities"]) == 1
    assert {e["entity_type"] for e in kg["entities"] if e["entity_name"].startswith("Lab")} == {"project", "organization"}


def add_mentions(s, sam, org):
    mentioned, message = record(s, "Sam Lee mentioned Example University in an update.", external="mention")
    edge(s, sam, message, "SENT", item=mentioned)
    edge(s, message, org, "MENTIONS", item=mentioned)
    # Legacy sender filtering requires reciprocal correspondence, as before this change.
    other = node(s, "Person", "Pat Other")
    edge(s, message, other, "RECEIVED", item=mentioned)
    reply, reply_node = record(s, "Reply to Sam", external="reply")
    edge(s, other, reply_node, "SENT", item=reply)
    edge(s, reply_node, sam, "RECEIVED", item=reply)
    topic = node(s, "Topic", "Project planning")
    edge(s, message, topic, "ABOUT", item=mentioned)


def test_multiple_facts_for_one_pair_keep_both_facts_and_their_evidence(s):
    _sid, sam, _p, org = derived_graph(s)
    add_mentions(s, sam, org)
    kg = export(s)
    assert_contract(kg)
    rel = next(r for r in kg["relationships"] if {r["src_id"], r["tgt_id"]} == {"Sam Lee", "Example University"})
    assert "worked or studied at" in rel["description"] and "mentioned" in rel["description"]
    evidence = next(c for c in kg["chunks"] if c["source_id"] == rel["source_id"])
    assert "gmail/record" in evidence["content"] and "gmail/mention" in evidence["content"]
    assert any("wrote about Project planning" in r["description"] for r in kg["relationships"])


def test_demo_blocks_unexpected_hosted_calls_before_transmitting(s, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("lightrag_demo", Path(__file__).parents[1] / "eval/lightrag_demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    finalized = []

    class UnexpectedCaller:
        def __init__(self, **kwargs):
            self.model = kwargs["llm_model_func"]

        async def initialize_storages(self):
            pass

        async def ainsert_custom_kg(self, kg):
            await self.model("This must never reach the hosted provider")

        async def finalize_storages(self):
            finalized.append(True)

    fake = ModuleType("lightrag")
    fake.LightRAG, fake.QueryParam = UnexpectedCaller, SimpleNamespace
    utils = ModuleType("lightrag.utils")
    utils.EmbeddingFunc = lambda **_kw: object()
    monkeypatch.setitem(sys.modules, "lightrag", fake)
    monkeypatch.setitem(sys.modules, "lightrag.utils", utils)
    kg_file = tmp_path / "kg.json"
    kg_file.write_text('{"chunks":[],"entities":[],"relationships":[]}')
    with pytest.raises(RuntimeError, match="hosted calls are disabled"):
        asyncio.run(demo.main(str(kg_file), None, load_only=True))
    assert finalized == [True]


def test_lightrag_load_and_graph_retrieval_without_hosted_calls(s, tmp_path):
    lightrag = pytest.importorskip("lightrag", reason="run with uv run --with lightrag-hku==1.5.7 pytest")
    import numpy as np
    from lightrag.utils import EmbeddingFunc

    from igneous.rag.embed import embed_texts

    _sid, sam, _p, org = derived_graph(s)
    add_mentions(s, sam, org)
    kg = export(s)
    calls = []

    async def forbidden(*a, **kw):
        calls.append((a, kw))
        raise AssertionError("custom KG loading and explicit-keyword context retrieval need no LLM")

    async def embed(texts):
        return np.array(embed_texts(texts))

    async def check():
        rag = lightrag.LightRAG(working_dir=str(tmp_path), llm_model_func=forbidden,
                               embedding_func=EmbeddingFunc(embedding_dim=384, func=embed,
                                                            model_name=llm.settings.embedding_model))
        await rag.initialize_storages()
        try:
            await rag.ainsert_custom_kg(kg)
            assert calls == []
            saved = await rag.chunk_entity_relation_graph.get_edge("Sam Lee", "Example University")
            assert "mentioned" in saved["description"] and "worked or studied at" in saved["description"]
            assert saved["source_id"] != "UNKNOWN"
            cited = await rag.text_chunks.get_by_id(saved["source_id"])
            assert "gmail/record" in cited["content"] and "gmail/mention" in cited["content"]
            for question, keywords, wanted in [
                ("Who worked on Widget Tracker?", ["Widget Tracker"], "Sam Lee took part in Widget Tracker"),
                ("What projects are under Example University?", ["Example University"],
                 "Widget Tracker is part of Example University"),
            ]:
                context = await rag.aquery(question, param=lightrag.QueryParam(
                    mode="hybrid", top_k=20, only_need_context=True, enable_rerank=False,
                    hl_keywords=["project", "organization", "membership"], ll_keywords=keywords))
                assert wanted in context
            assert calls == []
        finally:
            await rag.finalize_storages()

    asyncio.run(check())
