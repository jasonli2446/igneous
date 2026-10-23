from datetime import datetime


def test_rules_extract_person_date_and_source_then_resolve_canonical_id():
    from igneous.rag.constraints import apply

    class Result:
        def mappings(self):
            return self

        def first(self):
            return {"id": 42}

    class Session:
        def __init__(self):
            self.params = None

        def execute(self, statement, params):
            self.params = params
            assert "COALESCE(c.canonical_id, n.id)" in str(statement)
            return Result()

    session = Session()
    filters = apply(session, "Show emails from Jeff Skilling in May 2001")

    assert filters.person_id == 42
    assert filters.sources == ("email", "gmail")
    assert filters.since == datetime(2001, 5, 1)
    assert filters.until == datetime(2001, 6, 1)
    assert session.params == {"name": "Jeff Skilling"}


def test_explicit_filters_win_and_provider_failure_leaves_search_unconstrained(monkeypatch):
    from igneous import providers
    from igneous.rag.constraints import apply
    from igneous.rag.search import Filters

    monkeypatch.setattr(providers, "chat", lambda *args, **kwargs: (_ for _ in ()).throw(providers.LLMDisabled("off")))

    class Session:
        def execute(self, *args, **kwargs):
            raise AssertionError("no person should be resolved")

    explicit = Filters(source="fs", since=datetime(2020, 1, 1))
    assert apply(Session(), "What was discussed?", explicit) == explicit


def test_provider_fallback_uses_merged_interface_and_validates_result(monkeypatch):
    from igneous import llm, providers
    from igneous.rag.constraints import apply

    monkeypatch.setattr(llm.settings, "llm_sources", "gcal")
    calls = []

    def chat(messages, schema, **options):
        calls.append((messages, schema, options))
        return providers.ChatResult('{"person":"Jeff Skilling","source":"calendar","since":"2001-05-01T00:00:00","until":null}', {})

    monkeypatch.setattr(providers, "chat", chat)

    class Result:
        def mappings(self):
            return self

        def first(self):
            return {"id": 7}

    class Session:
        def execute(self, *args):
            return Result()

    filters = apply(Session(), "What did the planning discussion cover?")
    assert filters.person_id == 7 and filters.source == "gcal"
    assert filters.since == datetime(2001, 5, 1) and filters.until is None
    assert calls[0][2] == {"max_tokens": 180}

    # With nothing opted in, a search sends nothing anywhere, not even the question (#179).
    monkeypatch.setattr(llm.settings, "llm_sources", "")
    calls.clear()
    assert apply(Session(), "What did the planning discussion cover?").person_id is None
    assert calls == []


def test_person_id_filter_includes_canonical_aliases():
    from igneous.rag.search import Filters, _where

    where, params = _where(Filters(person_id=9))
    assert "canonical WHERE canonical_id = :person_id" in where
    assert "e.type = 'SENT'" in where and "e.type = 'RECEIVED'" in where
    assert params == {"person_id": 9}
