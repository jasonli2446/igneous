import json


def test_provider_wrappers_can_swap_a_fake(monkeypatch):
    from igneous import providers

    class FakeProvider:
        def embed(self, texts, *, query=False):
            return [[float(len(text)), float(query)] for text in texts]

        def chat(self, messages, schema=None, **options):
            assert messages == [{"role": "user", "content": "hello"}]
            assert schema == {"type": "object"}
            assert options == {"max_tokens": 12}
            return providers.ChatResult('{"ok": true}', {"input_tokens": 2, "output_tokens": 1})

        def rerank(self, query, docs):
            return [float(len(doc)) for doc in docs]

    monkeypatch.setattr(providers, "get_provider", lambda: FakeProvider())

    assert providers.embed(["a", "abc"], query=True) == [[1.0, 1.0], [3.0, 1.0]]
    result = providers.chat([{"role": "user", "content": "hello"}], {"type": "object"}, max_tokens=12)
    assert json.loads(result.text) == {"ok": True}
    assert result.usage == {"input_tokens": 2, "output_tokens": 1}
    assert providers.rerank("q", ["short", "longer"]) == [5.0, 6.0]


def test_structured_uses_configured_provider(monkeypatch):
    from igneous import llm, providers

    class FakeProvider:
        def chat(self, messages, schema=None, **options):
            assert messages[0] == {"role": "system", "content": "extract"}
            assert messages[1] == {"role": "user", "content": "body"}
            assert schema == {"type": "object"}
            return providers.ChatResult('{"topic": "providers"}', {"input_tokens": 3, "output_tokens": 2})

    monkeypatch.setattr(providers, "get_provider", lambda: FakeProvider())
    parsed, usage = llm.structured("extract", "body", {"type": "object"})
    assert parsed == {"topic": "providers"}
    assert usage == {"input_tokens": 3, "output_tokens": 2}
