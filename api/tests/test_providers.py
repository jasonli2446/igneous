"""What the Bedrock provider sends and how it fails, against a fake Anthropic client: no network, no credentials."""
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from igneous import llm, providers
from igneous.providers import BedrockProvider, LLMDisabled, ProviderError, ProviderUnavailable, accepts_effort

SONNET = "us.anthropic.claude-sonnet-5-5"
HAIKU_4_5 = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
REQUEST = httpx2.Request("POST", "https://bedrock-runtime.us-east-1.amazonaws.com/model/x/invoke")


def _reply(text="{}", stop="end_turn", blocks=None):
    content = blocks if blocks is not None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(stop_reason=stop, content=content, usage=SimpleNamespace(input_tokens=5, output_tokens=7))


def _status(cls, code: int):
    return cls(f"status {code}", response=httpx2.Response(code, request=REQUEST), body=None)


class FakeMessages:
    def __init__(self, *outcomes):
        self.outcomes, self.sent = list(outcomes), []

    def create(self, **kwargs):
        self.sent.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def bedrock(monkeypatch):
    """A provider whose client is a fake; `connect(*outcomes)` sets what each request gets back."""
    monkeypatch.setattr(providers.settings, "llm_enabled", True)
    provider = BedrockProvider(SONNET, max_retries=2)

    def connect(*outcomes) -> FakeMessages:
        messages = FakeMessages(*outcomes)
        provider._instance = SimpleNamespace(messages=messages)
        return messages
    provider.connect = connect
    return provider


@pytest.mark.parametrize(("model", "takes_effort"), [
    (SONNET, True), ("us.anthropic.claude-opus-5-5", True), ("us.anthropic.claude-haiku-5-5", True),
    ("claude-opus-4-6", True), ("claude-sonnet-4-6", True), ("us.anthropic.claude-opus-4-5-20251101-v1:0", True),
    ("claude-fable-5-2", True),
    (HAIKU_4_5, False), ("anthropic.claude-sonnet-4-5-20250929-v1:0", False),
    ("anthropic.claude-opus-4-1-20250805-v1:0", False), ("anthropic.claude-sonnet-4-20250514-v1:0", False),
    ("anthropic.claude-3-5-haiku-20241022-v1:0", False), ("anthropic.claude-3-haiku-20240307-v1:0", False),
])
def test_effort_goes_to_the_models_that_take_it(model, takes_effort):
    assert accepts_effort(model) is takes_effort


def test_the_request_carries_system_schema_and_effort_by_model(bedrock):
    sent = bedrock.connect(_reply('{"a": 1}'), _reply('{"a": 2}'), _reply('{"a": 3}')).sent
    messages = [{"role": "system", "content": "S1"}, {"role": "system", "content": "S2"}, {"role": "user", "content": "U"}]
    result = bedrock.chat(messages, {"type": "object"}, max_tokens=99, effort="medium")
    assert result == providers.ChatResult('{"a": 1}', {"input_tokens": 5, "output_tokens": 7})
    assert sent[0] == {"model": SONNET, "max_tokens": 99, "messages": [{"role": "user", "content": "U"}],
                       "system": "S1\n\nS2",
                       "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": {"type": "object"}}}}
    bedrock.chat([{"role": "user", "content": "U"}], {"type": "object"}, model=HAIKU_4_5)
    assert sent[1]["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}}
    bedrock.chat([{"role": "user", "content": "U"}], model="us.anthropic.claude-haiku-5-5", effort="low")
    assert sent[2]["output_config"] == {"effort": "low"} and "system" not in sent[2]


def test_the_sdk_is_the_only_retry_layer(bedrock, monkeypatch):
    sent = bedrock.connect(anthropic.APIConnectionError(request=REQUEST)).sent
    with pytest.raises(ProviderUnavailable, match="couldn't reach"):
        bedrock.chat([{"role": "user", "content": "U"}])
    assert len(sent) == 1  # the SDK already retried underneath; the provider adds no attempts
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "dummy-not-a-real-token")
    client = BedrockProvider(SONNET, max_retries=3)._client()
    assert client.max_retries == 3


@pytest.mark.parametrize(("outcome", "error", "words"), [
    (_reply(stop="refusal"), ProviderError, "declined"),
    (_reply("{", stop="max_tokens"), ProviderError, "cut off"),
    (_reply(blocks=[SimpleNamespace(type="thinking", thinking="")]), ProviderError, "no text"),
    (_status(anthropic.BadRequestError, 400), ProviderError, "rejected"),
    (_status(anthropic.RateLimitError, 429), ProviderUnavailable, "rate-limiting"),
    (_status(anthropic.AuthenticationError, 401), ProviderUnavailable, "refused"),
    (_status(anthropic.InternalServerError, 500), ProviderUnavailable, "failed"),
])
def test_failures_are_explicit_provider_errors(bedrock, outcome, error, words):
    bedrock.connect(outcome)
    with pytest.raises(error, match=words) as raised:
        bedrock.chat([{"role": "user", "content": "U"}])
    assert error is ProviderUnavailable or not isinstance(raised.value, ProviderUnavailable)


def test_no_token_or_the_kill_switch_is_llm_disabled(monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setattr(providers.settings, "llm_enabled", True)
    with pytest.raises(LLMDisabled, match="AWS_BEARER_TOKEN_BEDROCK"):
        BedrockProvider(SONNET, 2).chat([{"role": "user", "content": "U"}])
    monkeypatch.setattr(providers.settings, "llm_enabled", False)
    with pytest.raises(LLMDisabled, match="IGNEOUS_LLM_ENABLED"):
        BedrockProvider(SONNET, 2).chat([{"role": "user", "content": "U"}])
    assert issubclass(LLMDisabled, ProviderUnavailable) and issubclass(ProviderUnavailable, ProviderError)


def test_structured_parses_json_and_rejects_anything_else(monkeypatch):
    replies = iter(['{"topic": "providers"}', "not json", "[1, 2]"])
    calls = []

    def chat(messages, schema=None, **options):
        calls.append((messages, schema, options))
        return providers.ChatResult(next(replies), {"input_tokens": 3, "output_tokens": 2})

    monkeypatch.setattr(llm, "chat", chat)
    assert llm.structured("extract", "body", {"type": "object"}, model=HAIKU_4_5) == (
        {"topic": "providers"}, {"input_tokens": 3, "output_tokens": 2})
    assert calls[0] == ([{"role": "system", "content": "extract"}, {"role": "user", "content": "body"}],
                        {"type": "object"}, {"max_tokens": 4000, "effort": "low", "model": HAIKU_4_5})
    for _ in range(2):
        with pytest.raises(ProviderError, match="JSON"):
            llm.structured("extract", "body", {"type": "object"})


def test_module_functions_reach_the_configured_models(monkeypatch):
    class Fake:
        def embed(self, texts, *, query=False):
            return [[float(len(text)), float(query)] for text in texts]

        def rerank(self, query, docs):
            return [float(len(doc)) for doc in docs]

        def chat(self, messages, schema=None, **options):
            return providers.ChatResult(json.dumps({"options": options}), {})

    monkeypatch.setattr(providers, "_embedder", Fake)
    monkeypatch.setattr(providers, "_reranker", Fake)
    monkeypatch.setattr(providers, "_chat_model", Fake)
    assert providers.embed(["a", "abc"], query=True) == [[1.0, 1.0], [3.0, 1.0]]
    assert providers.rerank("q", ["short", "longer"]) == [5.0, 6.0]
    assert json.loads(providers.chat([{"role": "user", "content": "hi"}], max_tokens=12).text) == {
        "options": {"max_tokens": 12}}
