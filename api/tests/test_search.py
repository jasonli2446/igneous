"""Search: negation in both legs, route validation, the filters each response reports, and /experts under the
same filters. A small mailbox in the test database; embeddings are fixed vectors, not a model."""
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from igneous.config import settings
from igneous.rag import search
from igneous.rag.search import Filters, dense, lexical

T0 = datetime(2001, 1, 15, tzinfo=UTC)
DIM = 384


def _vector(*weights: float) -> str:
    return str([*weights, *([0.0] * (DIM - len(weights)))])


class Corpus:
    def __init__(self, s):
        self.s, self.chunks, self.items = s, {}, {}
        self.alice = self.person("alice@example.test", "Alice Smith")
        self.bob = self.person("bob@example.test", "Bob Jones")
        self.me = self.person("me@example.test", "Me")

    def person(self, key, name) -> int:
        return self.s.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Person', :k, :n) RETURNING id"),
                              {"k": key, "n": name}).scalar_one()

    def mail(self, key, sender, body, vector, source="gmail"):
        s = self.s
        item = s.execute(text("""INSERT INTO source_items (source, external_id, content_hash, title, body_text, occurred_at)
                                 VALUES (:s, :e, :e, :t, :b, :w) RETURNING id"""),
                         {"s": source, "e": key, "t": body, "b": body, "w": T0}).scalar_one()
        node = s.execute(text("INSERT INTO nodes (type, key, name, source_item_id) VALUES ('Message', :k, :n, :i) "
                              "RETURNING id"), {"k": key, "n": body, "i": item}).scalar_one()
        for src, dst, kind in ((sender, node, "SENT"), (node, self.me, "RECEIVED")):
            s.execute(text("INSERT INTO edges (src, dst, type) VALUES (:a, :b, :t)"), {"a": src, "b": dst, "t": kind})
        self.chunks[key] = s.execute(text("""
            INSERT INTO chunks (source_item_id, node_id, ord, text, embedding, embedding_model)
            VALUES (:i, :n, 0, :t, CAST(:v AS vector), :m) RETURNING id"""),
            {"i": item, "n": node, "t": body, "v": vector, "m": settings.embedding_model}).scalar_one()
        self.items[key] = item


@pytest.fixture
def corpus(db, monkeypatch):
    c = Corpus(db)
    c.mail("prices", c.alice, "Gas prices rose in California", _vector(1.0))
    c.mail("supply", c.alice, "Gas supply was short this week", _vector(0.9, 0.1))
    c.mail("power", c.bob, "Power prices fell again", _vector(0.8, 0.0, 0.2))
    c.mail("storage", c.bob, "Gas storage report for the week", _vector(0.7, 0.0, 0.0, 0.3))
    db.flush()
    monkeypatch.setattr(search, "embed_query", lambda _q: [1.0] + [0.0] * (DIM - 1))
    return c


def _names(c: Corpus, ids) -> set[str]:
    by_id = {v: k for k, v in c.chunks.items()}
    return {by_id[i] for i in ids}


def test_negated_terms_exclude_and_the_rest_are_ored(corpus):
    s = corpus.s
    assert _names(corpus, lexical(s, "gas prices", Filters())) == {"prices", "supply", "power", "storage"}
    assert _names(corpus, lexical(s, "gas -prices", Filters())) == {"supply", "storage"}
    assert _names(corpus, lexical(s, 'week -"gas supply"', Filters())) == {"storage"}
    assert lexical(s, "-prices", Filters()) == []  # nothing left to rank by
    # The dense leg drops them too: the nearest vector here is the negated one.
    assert _names(corpus, dense(s, "gas -prices", Filters())) == {"supply", "storage"}


def test_dense_search_compares_only_vectors_from_the_configured_model(corpus):
    s = corpus.s
    s.execute(text("UPDATE chunks SET embedding_model = 'another/model' WHERE id = :c"), {"c": corpus.chunks["prices"]})
    assert _names(corpus, dense(s, "gas", Filters())) == {"supply", "power", "storage"}


@pytest.mark.parametrize("params", [
    {"q": "   "}, {"q": "gas", "k": 0}, {"q": "gas", "k": -3}, {"q": "gas", "k": 51}, {"q": "gas", "mode": "bogus"},
    {"q": "gas", "sources": "faxes"}, {"q": "gas", "person": "nobody@example.test"},
    {"q": "gas", "from": "2001-02-01", "to": "2001-01-01"},
])
def test_search_rejects_bad_input(corpus, params):
    from igneous.main import app

    r = TestClient(app).get("/search", params=params)
    assert r.status_code == 422, r.text


def test_search_reports_the_filters_it_applied(corpus):
    from igneous.main import app

    client = TestClient(app)
    body = client.get("/search", params={"q": "gas", "person": "alice@example.test", "mode": "lexical"}).json()
    assert {h["chunk_id"] for h in body["hits"]} == {corpus.chunks["prices"], corpus.chunks["supply"]}
    assert body["filters"]["person"] == {"id": corpus.alice, "name": "Alice Smith"}
    assert body["filters"]["from_question"] == []
    body = client.get("/search", params={"q": "my emails about gas", "mode": "lexical"}).json()
    assert body["filters"]["sources"] == ["email", "gmail"] and body["filters"]["from_question"] == ["sources"]
    body = client.get("/search", params={"q": "gas", "sources": ["imessage"], "mode": "lexical"}).json()
    assert body["hits"] == [] and body["filters"]["sources"] == ["imessage"]


def test_experts_come_from_the_items_the_same_filters_find(corpus):
    from igneous.main import app

    client = TestClient(app)
    everyone = client.get("/experts", params={"q": "gas", "mode": "lexical"}).json()["experts"]
    assert {e["id"] for e in everyone} >= {corpus.alice, corpus.bob}
    body = client.get("/experts", params={"q": "gas", "person": "Alice Smith", "mode": "lexical"}).json()
    assert corpus.bob not in {e["id"] for e in body["experts"]} and corpus.alice in {e["id"] for e in body["experts"]}
    assert body["filters"]["person"]["id"] == corpus.alice
    for bad in ({"q": "  "}, {"q": "gas", "k": 0}, {"q": "gas", "k": 21}, {"q": "gas", "evidence_limit": 6},
                {"q": "gas", "mode": "bogus"}, {"q": "gas", "person": "nobody"}):
        assert client.get("/experts", params=bad).status_code == 422


def test_experts_gets_the_bounded_parameters(corpus, monkeypatch):
    from igneous import main

    calls = []
    monkeypatch.setattr(main, "experts", lambda _s, q, **kwargs: calls.append((q, kwargs)) or [])
    r = TestClient(main.app).get("/experts", params={"q": "budget", "k": 2, "evidence_limit": 1, "mode": "lexical"})
    assert r.status_code == 200 and r.json()["experts"] == []
    (q, kwargs), = calls
    assert q == "budget" and {k: kwargs[k] for k in ("k", "evidence_limit", "mode")} == {
        "k": 2, "evidence_limit": 1, "mode": "lexical"}
    assert kwargs["f"] == Filters()
