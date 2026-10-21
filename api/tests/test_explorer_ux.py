"""Explorer search ranking and node-summary grounding/fallback (#125). Synthetic data only."""
from igneous.graph import summary
from igneous.graph.find import match_quality


def test_match_quality_orders_exact_prefix_word_substring():
    q = "ann"
    scores = [match_quality(q, n) for n in ("Ann", "Ann Lee", "Mary Ann", "Joanne", "Zed")]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 1.0 and scores[-1] == 0.0
    assert match_quality("lee ann", "Ann Lee") == 0.7  # every query word starts a word


def test_match_quality_ignores_case_and_blank():
    assert match_quality("PROJECT x", "project x") == 1.0
    assert match_quality("", "x") == 0.0 and match_quality("x", "") == 0.0


def facts(sources=("email",)):
    return {
        "node": {"id": 1, "type": "Person", "name": "Ada Example", "key": "ada@example.test"},
        "stats": {"messages": 40, "first": "2001-01-05T00:00:00", "last": "2001-06-01T00:00:00", "recent": 3,
                  "sources": list(sources)},
        "connections": [
            {"id": 2, "name": "Bo Sample", "type": "Person", "category": "People", "count": 12, "last": None,
             "kind": "person", "why": "12 messages together, last Jun 2001", "sources": ["email"]},
            {"id": 3, "name": "Secret Chat", "type": "Thread", "category": "Groups", "count": 5, "last": None,
             "kind": "thread", "why": "5 messages in it, last Jun 2001", "sources": ["imessage"]},
        ],
    }


def test_prompt_withholds_details_for_sources_not_opted_in():
    prompt, used = summary.prompt_for(facts(), frozenset({"email"}), [])
    assert "Bo Sample" in prompt
    assert "Secret Chat" not in prompt and "1 other thread connection" in prompt
    assert used == []
    none_allowed, _ = summary.prompt_for(facts(), frozenset(), [])
    assert "Bo Sample" not in none_allowed


def test_prompt_includes_only_supplied_snippets_and_reports_their_sources():
    snip = [{"id": 77, "occurred_at": None, "title": "Quarterly forecast"}]
    prompt, used = summary.prompt_for(facts(), frozenset({"email"}), snip)
    assert "Quarterly forecast" in prompt and used == [77]


def test_fallback_is_plain_language_with_names_not_ids():
    text = summary.fallback(facts())
    assert "Ada Example" in text and "Bo Sample" in text and "Secret Chat" in text
    assert "Bo Sample (12)" in text and "id=" not in text and "node" not in text.lower()
    empty = {**facts(), "connections": [], "stats": {}}
    assert "no messages" in summary.fallback(empty)


class FakeSession:
    """Just enough Session for summarize(): an in-memory llm_cache and a canned set of facts."""

    def __init__(self):
        self.cache: dict[str, dict] = {}

    def execute(self, stmt, params=None):
        sql, params = str(stmt), params or {}
        outer = self

        class R:
            def scalar(self_):
                return outer.cache.get(params["k"]) if "SELECT response" in sql else None

        if "INSERT INTO llm_cache" in sql:
            import json
            self.cache[params["k"]] = json.loads(params["r"])
        return R()

    def commit(self): ...
    def rollback(self): ...


def run(monkeypatch, *, enabled, sources, model):
    monkeypatch.setattr(summary, "gather", lambda s, n: facts())
    monkeypatch.setattr(summary, "snippets", lambda s, n, src: [])
    monkeypatch.setattr(summary.settings, "llm_enabled", enabled)
    monkeypatch.setattr(summary.settings, "llm_sources", sources)
    return summary.summarize(FakeSession(), 1, model=model)


def test_summary_falls_back_when_model_disabled_or_source_not_opted_in(monkeypatch):
    def boom(*a):
        raise AssertionError("model must not be called")

    off = run(monkeypatch, enabled=False, sources="email", model=boom)
    assert off["generated_by"] == "fallback" and "Ada Example" in off["summary"]
    unopted = run(monkeypatch, enabled=True, sources="", model=boom)
    assert unopted["generated_by"] == "fallback"
    assert [h["name"] for h in unopted["highlights"]] == ["Bo Sample", "Secret Chat"]
    assert set(unopted["highlights"][0]) == {"id", "name", "type", "category", "why"}


def test_summary_falls_back_when_model_errors(monkeypatch):
    def fail(*a):
        raise RuntimeError("bedrock down")

    out = run(monkeypatch, enabled=True, sources="email", model=fail)
    assert out["generated_by"] == "fallback" and out["summary"]


def test_summary_uses_model_and_caches_it(monkeypatch):
    calls = []

    def model(system, user):
        calls.append(user)
        return "Ada talks mostly with Bo Sample."

    monkeypatch.setattr(summary, "gather", lambda s, n: facts())
    monkeypatch.setattr(summary, "snippets", lambda s, n, src: [])
    monkeypatch.setattr(summary.settings, "llm_enabled", True)
    monkeypatch.setattr(summary.settings, "llm_sources", "email")
    session = FakeSession()
    first = summary.summarize(session, 1, model=model)
    second = summary.summarize(session, 1, model=model)
    assert first["generated_by"] == second["generated_by"] == "model"
    assert first["summary"] == "Ada talks mostly with Bo Sample." and len(calls) == 1


def test_routes_are_registered():
    from igneous.main import app

    paths = app.openapi()["paths"]
    assert "get" in paths["/graph/find"] and "get" in paths["/graph/node/{node_id}/summary"]
