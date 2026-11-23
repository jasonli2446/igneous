"""One rule for every hosted model call: text only from opted-in sources, names only of what appears in them.

A synthetic graph mixes an opted-in source (gmail) with one that never is (imessage, carrying PRIVATE markers).
Every model-calling stage runs against it with a recording provider in place of Bedrock.
"""
import json
import re
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
    from igneous.graph.derived import Writer

    rec = Recorder()
    monkeypatch.setattr(llm, "chat", rec)
    monkeypatch.setattr(llm.settings, "llm_enabled", True)
    monkeypatch.setattr(llm.settings, "llm_sources", sources)
    monkeypatch.setattr(llm.settings, "local_sources_model_enabled", False)
    with SessionLocal() as s:
        verify.run(s, log=None)
        resolve_threads.run(s, log=None)
        threads.umbrellas(s, Writer(s, threads.EXTRACTOR))
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


def test_a_name_from_outside_opted_in_records_never_reaches_a_prompt(db, monkeypatch):
    """A project named by your LinkedIn (never opted in) whose opted-in records don't name it, and a person named by a
    contact card whose opted-in mail shows only an address: the verifier may read their records but never names them.
    A name the records hold (Sam's, on his own mail; a thread named from its records) is named."""
    from igneous.graph import verify

    me = _node(db, "Person", "me", "Me Myself", {"self": True})
    quinn = _node(db, "Person", "quinn@x.test", "Quinn Hidden", {"named_by": "contacts"})
    sam = _node(db, "Person", "sam@x.test", "Sam Shared")
    zephyr = _node(db, "Project", "profile:zephyr", "Zephyr Mobile Suite",
                   {"derived": "threads", "kind": "project", "pinned": True, "named_from": "profile"})
    atlas = _node(db, "Project", "thread:atlas", "Atlas", {"derived": "threads", "kind": "project",
                                                           "named_from": "model"})
    for i in range(3):
        for who, project, ref, title in ((quinn, zephyr, f"q{i}", f"Your mobile bill {i}"),
                                         (sam, atlas, f"s{i}", f"Atlas update {i}")):
            sid = _item(db, "gmail", ref, title, f"{title}: as attached.", T0 + timedelta(days=i))
            m = _node(db, "Message", f"gmail:{ref}", title, item=sid)
            _edge(db, who, m, "SENT", item=sid)
            _edge(db, m, me, "RECEIVED", item=sid)
            _edge(db, m, project, "PART_OF", "threads", item=sid)
    _edge(db, quinn, zephyr, "INVOLVED_IN", "threads")
    _edge(db, sam, atlas, "INVOLVED_IN", "threads")
    rec = Recorder()
    monkeypatch.setattr(llm, "chat", rec)
    monkeypatch.setattr(llm.settings, "llm_enabled", True)
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    stats = verify.run(db, log=None)
    joined = "\n".join(rec.prompts)
    assert "Person 'Sam Shared'" in joined and "Project 'Atlas'" in joined
    assert "Zephyr" not in joined and "Quinn" not in joined and stats["not_shared"] == 4
    assert llm.nameable(db, [sam, quinn, zephyr, atlas]) == {sam: "Sam Shared", atlas: "Atlas"}


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


class Unsure(Recorder):
    """A verifier that is unsure of every edge, so each one is asked again of the strong model."""

    def __init__(self):
        super().__init__()
        self.models: list[str | None] = []

    def __call__(self, messages, schema=None, **options):
        super().__call__(messages, schema, **options)
        self.models.append(options.get("model"))
        n = len(re.findall(r"^### Edge \d+$", messages[-1]["content"], re.M))
        return ChatResult(json.dumps({"edges": [{"edge": i, "verdict": "unsure", "confidence": 0.5, "reason": "?"}
                                                for i in range(n)]}), {"input_tokens": 0, "output_tokens": 0})


def test_the_verifiers_second_opinion_and_an_introducers_name_follow_the_rule(graph, monkeypatch):
    """The strong model's second look reads the same opted-in prompt as the first, and an introduction names its
    introducer only when opted-in records show them (Pat is only in texts)."""
    from igneous.graph import verify

    g = graph
    with SessionLocal() as s:
        sid = s.execute(text("SELECT id FROM source_items WHERE external_id = 'g0'")).scalar_one()
        s.execute(text("""INSERT INTO edges (src, dst, type, props, extractor, source_item_id)
                          VALUES (:a, :b, 'INTRODUCED', CAST(:p AS jsonb), 'relationship-history', :i)"""),
                  {"a": g["sam"], "b": g["sam2"], "p": json.dumps({"introducer": g["pat"]}), "i": sid})
        s.commit()
    rec = Unsure()
    monkeypatch.setattr(llm, "chat", rec)
    monkeypatch.setattr(llm.settings, "llm_enabled", True)
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    with SessionLocal() as s:
        verify.run(s, log=None)
        s.commit()
    assert None in rec.models and llm.settings.llm_cheap_model in rec.models  # asked again of the strong model
    assert rec.leaked() == [] and "introducer = '(not shared)'" in "\n".join(rec.prompts)


def test_opting_in_a_document_source_lets_its_resume_be_read(graph, monkeypatch):
    rec = _run_model_stages(monkeypatch, "gmail,drive")
    assert any("Robotics Lab research assistant" in p and "PRIVATEMARKER" not in p for p in rec.prompts)
    assert rec.leaked() == []


def test_a_cached_resume_still_counts_with_the_model_off(graph, monkeypatch):
    """A resume the model read earlier keeps counting with the model off: its cached reading is reused."""
    import hashlib

    from igneous.graph import profile

    with SessionLocal() as s:
        doc = next(d for d in profile.documents(s) if d.title == "Resume 2026")
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


def test_question_filters_send_the_question_alone_to_the_cheap_model_once(graph, monkeypatch):
    """Reading filters out of a question sends that question and nothing else, only once something is opted in, on
    the cheap model, and once per question."""
    from igneous.rag.constraints import apply

    rec, models = Recorder(), []

    def chat(messages, schema=None, **options):
        models.append(options.get("model"))
        return rec(messages, schema)

    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(llm.settings, "llm_enabled", True)
    question = "What did the Robotics Lab decide about the demo?"
    monkeypatch.setattr(llm.settings, "llm_sources", "")
    with SessionLocal() as s:
        apply(s, question)
    assert rec.prompts == []
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    with SessionLocal() as s:
        apply(s, question)
        apply(s, question)
    assert models == [llm.settings.llm_cheap_model]
    (prompt,) = rec.prompts
    assert prompt.endswith("\n" + question) and rec.leaked() == []
    assert "Atlas" not in prompt and "Sam Shared" not in prompt  # no record, no name from the graph


def _record_prompts(monkeypatch, sources: str) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(llm, "chat", rec)
    monkeypatch.setattr(llm.settings, "llm_enabled", True)
    monkeypatch.setattr(llm.settings, "llm_sources", sources)
    monkeypatch.setattr(llm.settings, "local_sources_model_enabled", False)
    return rec


def test_ask_reads_only_opted_in_records_even_through_the_graph(graph, monkeypatch):
    """Ask's retrieval and its graph expansion stay inside the opted-in sources: a text in the same thread as an
    opted-in email, even a reply to it, never reaches the prompt, while the email's opted-in neighbours do."""
    from igneous.rag import qa, rerank

    g = graph
    with SessionLocal() as s:
        thread = _node(s, "Thread", "thread:atlas-mail", "Atlas status")
        for i in range(8):
            _edge(s, g["items"][f"g{i}"], thread, "IN_THREAD")
            _edge(s, g["items"][f"i{i}"], thread, "IN_THREAD")
        _edge(s, g["items"]["i0"], g["items"]["g0"], "REPLIES_TO", "spine-quoted")
        s.commit()
    monkeypatch.setattr(rerank.settings, "reranker_enabled", False)
    question = "What was the Atlas status in 2025?"  # the rules read the year: no question-filter call
    rec = _record_prompts(monkeypatch, "gmail")
    with SessionLocal() as s:
        out = qa.answer(s, question, mode="lexical")
    assert out["evidence"] and {e["source"] for e in out["evidence"]} == {"gmail"}
    assert any("graph" in e["matched"] for e in out["evidence"])  # the expansion ran, inside gmail
    assert len(rec.prompts) == 1 and rec.leaked() == [] and "Pat Private" not in rec.prompts[0]
    # The same question with the texts opted in too does reach them through the graph: the limit is the opt-in.
    rec = _record_prompts(monkeypatch, "gmail,imessage")
    with SessionLocal() as s:
        qa.answer(s, question, mode="lexical")
    assert "PRIVATEMARKER" in rec.leaked()


def test_node_summaries_carry_only_opted_in_records_and_names(graph, monkeypatch):
    from igneous.graph.summary import summarize

    g = graph
    rec = _record_prompts(monkeypatch, "gmail")
    with SessionLocal() as s:
        for node in (g["atlas"], g["platform"], g["sam"], g["pat"], g["pat_only"], g["items"]["g0"]):
            summarize(s, node)
    assert rec.prompts and any("Sam Shared" in p for p in rec.prompts)
    assert rec.leaked() == []


def test_a_summary_names_a_connection_only_from_its_own_opted_in_evidence(graph, monkeypatch):
    """Sam's mail is opted in; the organization only a LinkedIn record links him to is not. A summary of Sam never names
    it: a connection carries the sources of its own evidence, never those of the person's mail."""
    from igneous.graph.summary import summarize

    g = graph
    with SessionLocal() as s:
        org = _node(s, "Organization", "org:secretcorp", "SecretCorp Holdings")
        item = _item(s, "linkedin", "connections/sam", "LinkedIn connection", "Sam Shared, SecretCorp Holdings", T0)
        _edge(s, g["sam"], org, "WORKED_AT", "linkedin", item=item)
        s.commit()
    rec = _record_prompts(monkeypatch, "gmail")
    with SessionLocal() as s:
        out = summarize(s, g["sam"])
    assert len(rec.prompts) == 1 and "Item: Sam Shared" in rec.prompts[0]
    assert "SecretCorp" not in rec.prompts[0] and "1 other organization connection" in rec.prompts[0]
    assert "SecretCorp Holdings" in [h["name"] for h in out["highlights"]]  # the inspector on this computer shows it
    # Nothing a summary of Pat may name: Pat appears only in texts, so no model is asked at all.
    rec = _record_prompts(monkeypatch, "gmail")
    with SessionLocal() as s:
        assert summarize(s, g["pat"])["generated_by"] == "fallback"
    assert rec.prompts == []


def test_extraction_reads_only_opted_in_messages(graph, monkeypatch):
    from igneous.graph import extract

    monkeypatch.setattr(extract, "MIN_CHARS", 20)  # the fixture's messages are short
    rec = _record_prompts(monkeypatch, "gmail")  # emails and texts alike carry no labels: only the opt-in tells them apart
    with SessionLocal() as s:
        extract.run(s, limit=50, log=lambda *_: None)
    assert len(rec.prompts) == 9 and all("Atlas" in p or "Same Sam" in p for p in rec.prompts)
    assert rec.leaked() == []


def test_entity_screening_sends_only_names_grounded_in_opted_in_records(graph, monkeypatch):
    """Candidate names go to the model only when an anchor appears in opted-in records: a folder of Drive files may
    be screened with Drive opted in; a folder of local files never is."""
    from igneous.graph import entities

    with SessionLocal() as s:
        for folder, source in (("Robotics Lab", "drive"), ("Zephyr Secret", "fs")):
            fid = _node(s, "Folder", f"folder:{folder}", folder)
            for n in range(2):
                item = _item(s, source, f"{folder}/{n}", f"{folder} file {n}", "notes", T0)
                _edge(s, fid, _node(s, "File", f"{source}:{folder}/{n}", f"{folder} file {n}", item=item), "CONTAINS",
                      item=item)
        s.commit()
    rec = _record_prompts(monkeypatch, "gmail,drive")
    with SessionLocal() as s:
        entities.run(s, log=None)
        s.commit()
    joined = "\n".join(rec.prompts).lower()
    assert "robotics lab" in joined and "zephyr" not in joined


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
    assert threads._judge(session, c)[0]["judged"] is False and rec.prompts == []
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    threads._judge(session, c)
    assert len(rec.prompts) == 1 and "Shared terms: Atlas\n" in rec.prompts[0]
    assert "Zephyr" not in rec.prompts[0] and rec.leaked() == []


def test_commitment_checks_read_only_opted_in_messages(db, monkeypatch):
    """The promise and a reply are mail (opted in); a text in the same thread never is, nor is a promise made in one."""
    from types import SimpleNamespace

    from igneous.graph import history
    from igneous.rag import embed

    def no_local_model(_query):
        raise RuntimeError("no local model")

    thread = _node(db, "Thread", "t:deck", "Deck")
    nodes = {}
    for ref, source, body, days in (("promise", "gmail", "I will send the deck by Monday.", 0),
                                    ("reply", "gmail", "Deck received, thanks.", 1),
                                    ("text", "imessage", "PRIVATEMARKER the deck is late again", 2),
                                    ("texted", "imessage", "PRIVATEMARKER I will call Pat Private tonight.", 3)):
        item = _item(db, source, ref, "Deck", body, T0 + timedelta(days=days))
        nodes[ref] = (item, _node(db, "Message", f"{source}:{ref}", "Deck", item=item), body)
        _edge(db, nodes[ref][1], thread, "IN_THREAD", item=item)
    for ref, what in (("promise", "send the deck"), ("texted", "call Pat Private")):
        item, node, body = nodes[ref]
        history.store_claims(db, SimpleNamespace(id=item, node_id=node, occurred_at=T0), body,
                             {"commitments": [{"who": "sender", "what": what, "by_when": None,
                                               "quote": body.removeprefix("PRIVATEMARKER ")}], "decisions": []}, "m")
    rec = Recorder()
    monkeypatch.setattr(llm, "chat", rec)
    monkeypatch.setattr(llm.settings, "llm_enabled", True)
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    monkeypatch.setattr(embed, "embed_query", no_local_model)
    history.refresh_commitment_status(db)
    assert len(rec.prompts) == 1
    assert "send the deck" in rec.prompts[0] and "Deck received" in rec.prompts[0]
    assert rec.leaked() == []


def test_naming_a_domain_reads_only_its_peoples_opted_in_mail(db, monkeypatch):
    """Ana signs her mail (opted in); her mail in a source never opted in stays out of the prompt."""
    from igneous.graph import organizations

    ana = _node(db, "Person", "ana@acme.test", "Ana Ames")
    for ref, source, body in (("g1", "gmail", "See you Monday.\n--\nAna Ames, Acme Robotics"),
                              ("e1", "email", "PRIVATEMARKER thanks, Pat Private")):
        item = _item(db, source, ref, "Hi", body, T0)
        _edge(db, ana, _node(db, "Message", f"{source}:{ref}", "Hi", item=item), "SENT", item=item)
    rec = Recorder()
    monkeypatch.setattr(llm, "chat", rec)
    monkeypatch.setattr(llm.settings, "llm_enabled", True)
    monkeypatch.setattr(llm.settings, "llm_sources", "")
    assert organizations.model_name(db, "acme.test") is None and rec.prompts == []
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    assert organizations.model_name(db, "acme.test") is None  # the recorder answers with an empty name
    assert len(rec.prompts) == 1 and "Domain: acme.test" in rec.prompts[0] and "Acme Robotics" in rec.prompts[0]
    assert rec.leaked() == []
