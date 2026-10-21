"""Provider-neutral model access.

The default combines local FastEmbed models with the existing Bedrock Messages
client.  It deliberately does not implement an OpenAI/Anthropic compatibility
layer: callers use this small application-level contract instead.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

import anthropic

from igneous.config import settings


class ProviderError(RuntimeError):
    """A configured provider cannot fulfill a request."""


class LLMDisabled(ProviderError):
    """Outbound hosted model access is intentionally unavailable."""


@dataclass(frozen=True)
class ChatResult:
    text: str
    usage: dict[str, int]


class ModelProvider(Protocol):
    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]: ...

    def chat(self, messages: list[dict[str, Any]], schema: dict | None = None, *, max_tokens: int = 4000,
             effort: str = "low", model: str | None = None) -> ChatResult: ...

    def rerank(self, query: str, docs: list[str]) -> list[float]: ...


class FastEmbedProvider:
    """Local ONNX embedding model; loaded only on its first request."""

    def __init__(self, model_name: str, batch_size: int):
        self.model_name = model_name
        self.batch_size = batch_size
        self._instance: Any | None = None

    def _model(self):
        if self._instance is None:
            from fastembed import TextEmbedding

            self._instance = TextEmbedding(self.model_name)
        return self._instance

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        encoder = self._model()
        vectors = encoder.query_embed(texts) if query else encoder.embed(texts, batch_size=self.batch_size)
        return [vector.tolist() for vector in vectors]


class CrossEncoderProvider:
    """Local ONNX cross-encoder for relevance scores aligned with ``docs``."""

    def __init__(self, model_name: str, batch_size: int):
        self.model_name = model_name
        self.batch_size = batch_size
        self._instance: Any | None = None

    def _model(self):
        if self._instance is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._instance = TextCrossEncoder(self.model_name)
        return self._instance

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        return [float(score) for score in self._model().rerank(query, docs, batch_size=self.batch_size)]


class BedrockProvider:
    """The existing Bedrock Messages integration, with bounded transient retries."""

    def __init__(self, model_name: str, retries: int, retry_seconds: float):
        self.model_name = model_name
        self.retries = retries
        self.retry_seconds = retry_seconds
        self._instance: anthropic.AnthropicBedrock | None = None

    def _client(self) -> anthropic.AnthropicBedrock:
        if self._instance is None:
            if not settings.llm_enabled:
                raise LLMDisabled("model calls disabled (IGNEOUS_LLM_ENABLED=false)")
            token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
            if not token:
                raise LLMDisabled("AWS_BEARER_TOKEN_BEDROCK is not set")
            self._instance = anthropic.AnthropicBedrock(
                aws_region=os.environ.get("AWS_REGION", "us-east-1"), api_key=token)
        return self._instance

    def chat(self, messages: list[dict[str, Any]], schema: dict | None = None, *, max_tokens: int = 4000,
             effort: str = "low", model: str | None = None) -> ChatResult:
        system = "\n\n".join(str(message["content"]) for message in messages if message["role"] == "system")
        conversation = [message for message in messages if message["role"] != "system"]
        if not conversation:
            raise ValueError("chat requires at least one non-system message")
        kwargs: dict[str, Any] = {
            "model": model or self.model_name,
            "max_tokens": max_tokens,
            "messages": conversation,
            "output_config": {"effort": effort},
        }
        if system:
            kwargs["system"] = system
        if schema is not None:
            kwargs["output_config"]["format"] = {"type": "json_schema", "schema": schema}
        for attempt in range(self.retries + 1):
            try:
                response = self._client().messages.create(**kwargs)
                if response.stop_reason in ("refusal", "max_tokens"):
                    raise ProviderError(f"model stopped: {response.stop_reason}")
                text = next(block.text for block in response.content if block.type == "text")
                return ChatResult(text, {"input_tokens": response.usage.input_tokens,
                                         "output_tokens": response.usage.output_tokens})
            except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError):
                if attempt == self.retries:
                    raise
                time.sleep(self.retry_seconds * (2 ** attempt))
        raise AssertionError("unreachable")

    def chat_many(self, requests: list[tuple[list[dict[str, Any]], dict | None, dict[str, Any]]]) -> list[ChatResult]:
        """Run independent Bedrock requests concurrently while preserving input order."""
        with ThreadPoolExecutor(max_workers=min(settings.hosted_batch_size, len(requests) or 1)) as executor:
            futures = [executor.submit(self.chat, messages, schema, **options)
                       for messages, schema, options in requests]
            return [future.result() for future in futures]


class ConfiguredProvider:
    """Routes each capability to the implementation selected through settings."""

    def __init__(self):
        if settings.embedding_provider != "fastembed":
            raise ValueError(f"unsupported embedding provider: {settings.embedding_provider}")
        if settings.rerank_provider != "cross_encoder":
            raise ValueError(f"unsupported rerank provider: {settings.rerank_provider}")
        if settings.chat_provider != "bedrock":
            raise ValueError(f"unsupported chat provider: {settings.chat_provider}")
        self.embedding = FastEmbedProvider(settings.embedding_model, settings.embedding_batch_size)
        self.reranker = CrossEncoderProvider(settings.reranker_model, settings.rerank_batch_size)
        self.chat_provider = BedrockProvider(settings.llm_model, settings.hosted_retries, settings.hosted_retry_seconds)

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        return self.embedding.embed(texts, query=query)

    def chat(self, messages: list[dict[str, Any]], schema: dict | None = None, **options: Any) -> ChatResult:
        return self.chat_provider.chat(messages, schema, **options)

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        return self.reranker.rerank(query, docs)


@lru_cache(maxsize=1)
def get_provider() -> ModelProvider:
    return ConfiguredProvider()


def embed(texts: list[str], *, query: bool = False) -> list[list[float]]:
    return get_provider().embed(texts, query=query)


def chat(messages: list[dict[str, Any]], schema: dict | None = None, **options: Any) -> ChatResult:
    return get_provider().chat(messages, schema, **options)


def rerank(query: str, docs: list[str]) -> list[float]:
    return get_provider().rerank(query, docs)
