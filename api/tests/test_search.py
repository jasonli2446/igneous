"""Search: negation in both legs, exact dense search under filters, rankings two builds of the same records repeat,
route validation, the filters each response reports, and /experts under the same filters. A small mailbox in the test
database; embeddings are fixed vectors, not a model."""
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


def test_dense_search_finds_the_nearest_passages_a_filter_lets_through(corpus):
    """Exact search: however many nearer passages a filter leaves out (here a negated word in the 120 nearest the
    query), the ones it lets through come back nearest first. Sorting is off, so the planner would read any index that
    gives the order: an approximate one looks at only so many candidates, and the filter comes after them."""
    s = corpus.s
    for i in range(120):
        corpus.mail(f"near{i}", corpus.alice, f"Gas note {i}", _vector(1.0, 0.001 * i))
    s.flush()
    s.execute(text("SET LOCAL enable_sort = off"))
    assert dense(s, "gas -note", Filters()) == [corpus.chunks[k] for k in ("prices", "supply", "power", "storage")]


def _build(s, records: list[tuple[int, str, str]], order: list[int]) -> None:
    """One build of `records` (id, text, vector): every record keeps its id, and rows are stored in `order`, as two
    builds of the same data, or one before and after a vacuum or an update, store them in different places."""
    s.execute(text("DELETE FROM chunks"))
    s.execute(text("DELETE FROM source_items"))
    for i in order:
        rid, body, vector = records[i]
        s.execute(text("""INSERT INTO source_items (id, source, external_id, content_hash, title, body_text, occurred_at)
                          VALUES (:i, 'gmail', :e, :e, :b, :b, :w)"""), {"i": rid, "e": f"m{rid}", "b": body, "w": T0})
        s.execute(text("""INSERT INTO chunks (id, source_item_id, ord, text, embedding, embedding_model)
                          VALUES (:i, :i, 0, :b, CAST(:v AS vector), :m)"""),
                  {"i": rid, "b": body, "v": vector, "m": settings.embedding_model})
    s.flush()


def test_two_builds_of_the_same_records_rank_alike(db, monkeypatch):
    """Equal scores go in chunk id order in every ranking (each leg, the fusion, the cut at k), so the order rows are
    stored in never shows in the results."""
    monkeypatch.setattr(search, "embed_query", lambda _q: [1.0] + [0.0] * (DIM - 1))
    # Twelve passages that tie on both legs, others that tie with them on one, and four nearer the query's vector.
    records = [(100 + i, f"Atlas budget review {chr(97 + i)}", _vector(0.9, 0.1)) for i in range(12)]
    records += [(200 + i, f"Notes on the Atlas launch, week {i}: budget later", _vector(0.9, 0.3 + 0.2 * i))
                for i in range(6)]
    records += [(300 + i, f"Lunch order {i}", _vector(1.0, 0.01 * i)) for i in range(4)]
    queries = ("atlas budget", "atlas launch", "lunch -order", "budget")

    def results() -> dict:
        out = {}
        for q in queries:
            out[q, "lexical"] = lexical(db, q, Filters())
            out[q, "lexical@5"] = lexical(db, q, Filters(), n=5)
            out[q, "dense"] = dense(db, q, Filters())
            out[q, "dense@5"] = dense(db, q, Filters(), n=5)
            for mode in ("hybrid", "lexical", "dense"):
                out[q, mode, "k=7"] = [h["chunk_id"] for h in search.hybrid(db, q, k=7, mode=mode)]
        return out

    _build(db, records, list(range(len(records))))
    first = results()
    _build(db, records, list(reversed(range(len(records)))))
    assert results() == first
    assert first["budget", "lexical@5"] == [100, 101, 102, 103, 104]  # all eighteen rank alike
    assert first["atlas budget", "dense@5"] == [300, 301, 302, 303, 100]


def test_fusion_orders_equal_scores_by_chunk_id():
    # 7 and 5 both rank first in one list; 2 and 9 both second.
    assert [cid for cid, _ in search.rrf([7, 2], [5, 9])] == [5, 7, 2, 9]


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
