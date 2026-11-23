"""Ask: graph neighbours reach the evidence with the reranker on and off, nearest first, and citations come only
with an answer. A synthetic mailbox in the test database; the hosted model is a stub."""
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from igneous import llm
from igneous.providers import ChatResult, LLMDisabled, ProviderError, ProviderUnavailable
from igneous.rag import qa, rerank

T0 = datetime(2001, 5, 1, 9, 0, tzinfo=UTC)
QUESTION = "Was the Atlas budget approval granted?"


def _node(s, type_, key, name, item=None) -> int:
    return s.execute(text("INSERT INTO nodes (type, key, name, source_item_id) VALUES (:t, :k, :n, :i) RETURNING id"),
                     {"t": type_, "k": key, "n": name, "i": item}).scalar_one()


def _edge(s, src, dst, type_, when=None, extractor="spine"):
    s.execute(text("INSERT INTO edges (src, dst, type, extractor, valid_from) VALUES (:a, :b, :t, :x, :w)"),
              {"a": src, "b": dst, "t": type_, "x": extractor, "w": when})


class Mailbox:
    """Messages between two people, each with its thread, its chunk and the spine's edges."""

    def __init__(self, s):
        self.s = s
        self.sam = _node(s, "Person", "sam@example.test", "Sam Lee")
        self.me = _node(s, "Person", "me@example.test", "Me")
        self.threads: dict[str, int] = {}
        self.chunks: dict[str, int] = {}
        self.nodes: dict[str, int] = {}

    def mail(self, key, title, body, when, thread=None, sender=None, recipient=None, source="gmail"):
        s = self.s
        item = s.execute(text("""
            INSERT INTO source_items (source, external_id, content_hash, title, body_text, occurred_at)
            VALUES (:s, :e, :e, :t, :b, :w) RETURNING id"""),
            {"s": source, "e": key, "t": title, "b": body, "w": when}).scalar_one()
        node = _node(s, "Message", f"{source}:{key}", title, item)
        _edge(s, sender or self.sam, node, "SENT", when)
        _edge(s, node, recipient or self.me, "RECEIVED", when)
        thread = thread or key
        if thread not in self.threads:
            self.threads[thread] = _node(s, "Thread", f"thread:{thread}", title)
        _edge(s, node, self.threads[thread], "IN_THREAD", when)
        self.chunks[key] = s.execute(text("""
            INSERT INTO chunks (source_item_id, node_id, ord, text) VALUES (:i, :n, 0, :t) RETURNING id"""),
            {"i": item, "n": node, "t": f"Subject: {title}\n{body}"}).scalar_one()
        self.nodes[key] = node
        return self.chunks[key]


@pytest.fixture
def mailbox(db):
    """A request (a1) whose reply (b1) answers the question without its words; ten notes that match the question
    better than a2, whose reply b2 is the only one to name the amount."""
    box = Mailbox(db)
    box.mail("a1", "Atlas budget approval", "Atlas budget approval: please grant the Atlas budget approval. "
             "The Atlas budget approval is needed for the robotics lab.", T0, thread="atlas")
    box.mail("b1", "Re: Atlas budget approval", "Yes, go ahead with the lab plan.", T0 + timedelta(hours=2),
             thread="atlas", sender=box.me, recipient=box.sam)
    _edge(db, box.nodes["b1"], box.nodes["a1"], "REPLIES_TO", extractor="spine-quoted")
    for i in range(10):
        box.mail(f"d{i}", f"Atlas budget notes {i}", f"Atlas budget notes for week {i}: the Atlas budget and its "
                 "approval steps.", T0 + timedelta(days=20 + i))
    box.mail("a2", "Lab spending", "Some spending items for Atlas are still open, and Sam will check them.",
             T0 + timedelta(days=60), thread="spending")
    box.mail("b2", "Re: Lab spending", "Confirmed: forty thousand dollars.", T0 + timedelta(days=61),
             thread="spending", sender=box.me, recipient=box.sam)
    db.flush()
    return box


@pytest.fixture
def model(monkeypatch):
    """Opt gmail in and answer with a stub that cites whatever the test says; record each prompt."""
    calls: list[dict] = []
    reply: dict = {"answerable": True, "answer": "", "cited": []}

    def chat(messages, schema=None, **options):
        if not schema or "answerable" not in schema.get("properties", {}):
            raise LLMDisabled("only Ask's answer is stubbed")
        calls.append({"prompt": messages[-1]["content"], **options})
        return ChatResult(json.dumps(reply), {"input_tokens": 1, "output_tokens": 1})

    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    monkeypatch.setattr(llm.settings, "llm_enabled", True)
    monkeypatch.setattr(rerank, "_load_failure", None)
    return type("Model", (), {"calls": calls, "reply": reply})


def _shown(result) -> list[int]:
    return [e["chunk_id"] for e in result["evidence"]]


def test_without_a_reranker_graph_neighbours_keep_places_in_the_evidence(mailbox, model, monkeypatch):
    monkeypatch.setattr(rerank.settings, "reranker_enabled", False)
    result = qa.answer(mailbox.s, QUESTION, mode="lexical")
    evidence = {e["chunk_id"]: e for e in result["evidence"]}
    assert len(evidence) == qa.CONTEXT_CHUNKS
    # The reply ranks far below the cut on its subject line alone; the graph places it right after its request.
    assert [h["chunk_id"] for h in qa.hybrid(mailbox.s, QUESTION, k=32, mode="lexical")].index(
        mailbox.chunks["b1"]) >= qa.CONTEXT_CHUNKS
    b1 = evidence[mailbox.chunks["b1"]]
    assert b1["matched"] == ["lexical", "graph"] and b1["relation"] == "reply"
    assert b1["via_chunk_id"] == mailbox.chunks["a1"]
    shown = _shown(result)
    assert shown[shown.index(mailbox.chunks["a1"]) + 1] == mailbox.chunks["b1"]
    assert sum("graph" in e["matched"] for e in result["evidence"]) <= qa.GRAPH_SLOTS
    assert "reply of c" in model.calls[0]["prompt"]  # the model is told how the neighbour was found


def test_a_failed_reranker_also_keeps_graph_places(mailbox, model, monkeypatch):
    monkeypatch.setattr(rerank, "score_documents", lambda *_: (_ for _ in ()).throw(OSError("no model files")))
    result = qa.answer(mailbox.s, QUESTION, mode="lexical")
    assert mailbox.chunks["b1"] in _shown(result)


def test_with_the_reranker_the_pool_reaches_neighbours_of_hits_below_the_evidence_cut(mailbox, model, monkeypatch):
    hits = qa.hybrid(mailbox.s, QUESTION, k=32, mode="lexical")
    assert [h["chunk_id"] for h in hits].index(mailbox.chunks["a2"]) >= qa.CONTEXT_CHUNKS  # a2 ranks low
    scored: list[list[str]] = []

    def cross_encoder(question, documents):
        scored.append(documents)
        return [9.0 if "forty thousand" in d else 1.0 for d in documents]

    monkeypatch.setattr(rerank, "score_documents", cross_encoder)
    result = qa.answer(mailbox.s, QUESTION, mode="lexical")
    top = result["evidence"][0]
    assert top["chunk_id"] == mailbox.chunks["b2"] and top["matched"] == ["graph"] and top["rerank_score"] == 9.0
    assert len(scored[0]) <= qa.settings.reranker_pool_size
    assert any("go ahead with the lab plan" in d for d in scored[0])  # every hit's neighbours were scored


def test_thread_siblings_are_the_nearest_in_time_to_the_hit(db):
    box = Mailbox(db)
    days = [0, 6, 1, 5, 2, 4, 3]  # inserted out of time order, so chunk ids don't follow time
    for d in days:
        box.mail(f"m{d}", f"Planning day {d}", f"Notes for day {d}", T0 + timedelta(days=d), thread="plan")
    db.flush()
    hit = {"chunk_id": box.chunks["m3"], "node_id": box.nodes["m3"], "score": 0.5}
    out = qa.expand(db, [hit], per_hit=2)
    assert {n["chunk_id"] for n in out} == {box.chunks["m2"], box.chunks["m4"]}
    assert all(n["relation"] == "thread" and n["score"] == 0.5 for n in out)
    # A direct reply outranks a closer thread sibling, and the cap applies only after ranking.
    _edge(db, box.nodes["m6"], box.nodes["m3"], "REPLIES_TO", extractor="spine-quoted")
    db.flush()
    out = qa.expand(db, [hit], per_hit=2)
    assert [n["chunk_id"] for n in out] == [box.chunks["m6"], box.chunks["m2"]]
    assert [n["relation"] for n in out] == ["reply", "thread"]


def test_citations_come_only_with_an_answer_and_carry_item_and_node_ids(mailbox, model, monkeypatch):
    monkeypatch.setattr(rerank.settings, "reranker_enabled", False)
    b1 = mailbox.chunks["b1"]
    model.reply.update(answerable=True, answer=f"Yes [c{b1}].", cited=[b1, 999999])
    result = qa.answer(mailbox.s, QUESTION, mode="lexical")
    assert result["answerable"] and [c["chunk_id"] for c in result["citations"]] == [b1]
    citation = result["citations"][0]
    assert citation["node_id"] == mailbox.nodes["b1"] and citation["source_item_id"]

    model.reply.update(answerable=False, answer="The evidence does not say.", cited=[b1])
    result = qa.answer(mailbox.s, QUESTION, mode="lexical")
    assert not result["answerable"] and result["citations"] == [] and result["evidence"]

    model.reply.update(answerable=True, answer="Yes.", cited=[])
    result = qa.answer(mailbox.s, QUESTION, mode="lexical")
    assert not result["answerable"] and result["citations"] == [] and "withheld" in result["answer"]


def test_ask_route_maps_provider_failures_and_validates_its_body(mailbox, model, monkeypatch):
    from igneous.main import app

    client = TestClient(app)
    for bad in ({"question": "   "}, {"question": QUESTION, "mode": "bogus"}, {}):
        assert client.post("/ask", json=bad).status_code == 422

    def refuse(*_args, **_kwargs):
        raise ProviderError("model stopped: refusal")

    monkeypatch.setattr(llm, "chat", refuse)
    r = client.post("/ask", json={"question": QUESTION, "mode": "lexical"})
    assert r.status_code == 502 and "refusal" in r.json()["detail"]

    def off(*_args, **_kwargs):
        raise LLMDisabled("AWS_BEARER_TOKEN_BEDROCK is not set")

    monkeypatch.setattr(llm, "chat", off)
    r = client.post("/ask", json={"question": QUESTION, "mode": "lexical"})
    assert r.status_code == 503 and "AWS_BEARER_TOKEN_BEDROCK" in r.json()["detail"]

    def busy(*_args, **_kwargs):
        raise ProviderUnavailable("Bedrock is rate-limiting the model; try again shortly")

    monkeypatch.setattr(llm, "chat", busy)
    r = client.post("/ask", json={"question": QUESTION, "mode": "lexical"})
    assert r.status_code == 503 and "rate-limiting" in r.json()["detail"]
