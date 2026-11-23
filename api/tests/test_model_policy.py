"""One rule for every hosted model call: text only from opted-in sources, names only of what appears in them.

A synthetic graph mixes an opted-in source (gmail) with one that never is (imessage, carrying PRIVATE markers).
Every model-calling stage runs against it with a recording provider in place of Bedrock.
"""
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from mixed_sources import T0, Recorder, _edge, _item, _named, _node, build, wipe
from sqlalchemy import text

from igneous import llm
from igneous.db import SessionLocal
from igneous.providers import ChatResult


@pytest.fixture
def graph(test_db):
    """Two threads (Atlas, Atlas platform) with mail from Sam (gmail) and texts from Pat (imessage), over 8 months."""
    yield build()
    wipe()


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


def test_a_link_you_add_never_lets_a_prompt_name_its_ends(graph):
    """A connection you add is your word, not a record's: linking Pat (known only from texts) and Pat's trip to
    opted-in mail changes nothing a prompt may name."""
    g = graph
    with SessionLocal() as s:
        ids = s.execute(text("SELECT id FROM nodes")).scalars().all()
        before = llm.nameable(s, ids, frozenset({"gmail"}))
        _edge(s, g["pat"], g["items"]["g0"], "RELATED_TO", "user")
        _edge(s, g["items"]["g1"], g["pat_only"], "RELATED_TO", "user")
        assert llm.nameable(s, ids, frozenset({"gmail"})) == before
        assert g["pat"] not in before and g["pat_only"] not in before
        s.rollback()


def test_a_name_only_a_record_outside_the_opt_in_gave_is_never_named(db):
    """Ella is named only by a calendar invite (Calendar not opted in) and appears in opted-in mail by address alone:
    no prompt may name her until an opted-in record holds the name, or Calendar is opted in."""
    ella = _node(db, "Person", "ella@x.test", "Ella Novak")
    invite = _item(db, "gcal", "cal/1", "Reading group", "Weekly reading group.", T0)
    _named(db, ella, invite, "Ella Novak")
    mail = _item(db, "gmail", "m1", "Notes", "Notes from today, as promised.", T0)
    message = _node(db, "Message", "gmail:m1", "Notes", item=mail)
    _edge(db, message, ella, "RECEIVED", item=mail)
    _named(db, ella, mail, "Ella")  # the address's local part: the mail has no display name
    assert llm.nameable(db, [ella], frozenset({"gmail"})) == {}
    assert llm.nameable(db, [ella], frozenset({"gmail", "gcal"})) == {ella: "Ella Novak"}
    thanks = _item(db, "gmail", "m2", "Re: Notes", "Thanks, Ella Novak!", T0)
    reply = _node(db, "Message", "gmail:m2", "Re: Notes", item=thanks)
    _edge(db, reply, ella, "RECEIVED", item=thanks)
    assert llm.nameable(db, [ella], frozenset({"gmail"})) == {ella: "Ella Novak"}


def test_a_person_is_named_from_any_identitys_records_when_they_hold_the_name(db):
    """A person whose opted-in records all sit under another of their addresses (the cluster's head has none of its
    own) is named when those records hold the name, and only then."""
    head = _node(db, "Person", "email:dana@school.test", "Dana Kimura")
    alias = _node(db, "Person", "email:dk@home.test", "dk@home.test")
    quiet = _node(db, "Person", "email:q@school.test", "Quentin Quiet")
    quiet_alias = _node(db, "Person", "email:qq@home.test", "qq@home.test")
    db.execute(text("INSERT INTO canonical VALUES (:a, :h), (:h, :h), (:qa, :q), (:q, :q)"),
               {"a": alias, "h": head, "qa": quiet_alias, "q": quiet})
    for who, body in ((alias, "From: Dana Kimura <dk@home.test>. See you in Lisbon."), (quiet_alias, "See you soon.")):
        sid = _item(db, "gmail", f"m{who}", "Plans", body, T0)
        m = _node(db, "Message", f"gmail:m{who}", "Plans", item=sid)
        _edge(db, who, m, "SENT", item=sid)
    assert llm.nameable(db, [head, quiet], frozenset({"gmail"})) == {head: "Dana Kimura"}
    assert llm.nameable(db, [head], frozenset({"imessage"})) == {}


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
            if who == sam:
                _named(db, sam, sid, "Sam Shared")  # his mail's From header names him
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
