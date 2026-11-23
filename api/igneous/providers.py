"""Model access: local FastEmbed models for embeddings and reranking, and Claude on Amazon Bedrock for chat.

Embedding and reranking run on this computer and send nothing anywhere. Chat is the one hosted call, and every caller
reaches it through igneous/llm.py, which decides what a prompt may carry. A chat call fails in one of three ways:
LLMDisabled (hosted calls are off or not configured), ProviderUnavailable (the model can't be reached or used now)
and ProviderError (it gave no usable answer).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import anthropic

from igneous.config import settings


class ProviderError(RuntimeError):
    """The hosted model gave no usable answer: it declined, ran out of tokens, returned no text, or the request was
    rejected."""


class ProviderUnavailable(ProviderError):
    """The hosted model can't be used now: unreachable, rate-limited, failing, or refusing these credentials or this
    model."""


class LLMDisabled(ProviderUnavailable):
    """Hosted model calls are turned off (IGNEOUS_LLM_ENABLED=false) or not configured (no Bedrock token)."""


@dataclass(frozen=True)
class ChatResult:
    text: str
    usage: dict[str, int]


class FastEmbedProvider:
    """Local ONNX embedding model, downloaded into `cache_dir` and loaded on its first request."""

    def __init__(self, model_name: str, batch_size: int, cache_dir: str):
        self.model_name = model_name
        self.batch_size = batch_size
        self.cache_dir = cache_dir
        self._instance: Any | None = None

    def _model(self):
        if self._instance is None:
            from fastembed import TextEmbedding

            self._instance = TextEmbedding(self.model_name, cache_dir=self.cache_dir)
        return self._instance

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        encoder = self._model()
        vectors = encoder.query_embed(texts) if query else encoder.embed(texts, batch_size=self.batch_size)
        return [vector.tolist() for vector in vectors]


class CrossEncoderProvider:
    """Local ONNX cross-encoder for relevance scores aligned with ``docs``, downloaded into `cache_dir`."""

    def __init__(self, model_name: str, batch_size: int, cache_dir: str):
        self.model_name = model_name
        self.batch_size = batch_size
        self.cache_dir = cache_dir
        self._instance: Any | None = None

    def _model(self):
        if self._instance is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._instance = TextCrossEncoder(self.model_name, cache_dir=self.cache_dir)
        return self._instance

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        return [float(score) for score in self._model().rerank(query, docs, batch_size=self.batch_size)]


# A Claude model id, on Bedrock or not: "us.anthropic.claude-sonnet-5-5", "claude-haiku-4-5-20251001-v1:0", or the
# older "claude-3-5-haiku-20241022". A minor version is one digit, so a date never reads as one.
_CLAUDE = re.compile(r"claude-(?:(?P<family>opus|sonnet|haiku)-(?P<major>\d+)(?:-(?P<minor>\d)(?!\d))?"
                     r"|(?P<legacy>\d)(?:-\d(?!\d))?-(?:opus|sonnet|haiku))")


def accepts_effort(model_id: str) -> bool:
    """Whether a model takes `output_config.effort`. Opus 4.5 and every model from 4.6 on do; Sonnet 4.5, Haiku 4.5
    and earlier models reject it with a 400. A model id this can't read (a custom inference profile ARN) is assumed
    to be a current model, which takes it."""
    m = _CLAUDE.search(model_id)
    if m is None:
        return True
    if m["legacy"]:
        return False
    version = (int(m["major"]), int(m["minor"] or 0))
    return version >= (4, 6) or (m["family"] == "opus" and version >= (4, 5))


class BedrockProvider:
    """Claude on Amazon Bedrock through the Anthropic SDK. The SDK retries connection errors and 408, 409, 429 and
    5xx responses, with backoff, up to IGNEOUS_HOSTED_RETRIES times; nothing here retries again."""

    def __init__(self, model_name: str, max_retries: int):
        self.model_name = model_name
        self.max_retries = max_retries
        self._instance: anthropic.AnthropicBedrock | None = None

    def _client(self) -> anthropic.AnthropicBedrock:
        if not settings.llm_enabled:
            raise LLMDisabled("model calls are turned off (IGNEOUS_LLM_ENABLED=false)")
        if self._instance is None:
            token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
            if not token:
                raise LLMDisabled("AWS_BEARER_TOKEN_BEDROCK is not set")
            self._instance = anthropic.AnthropicBedrock(aws_region=os.environ.get("AWS_REGION", "us-east-1"),
                                                        api_key=token, max_retries=self.max_retries)
        return self._instance

    def chat(self, messages: list[dict[str, Any]], schema: dict | None = None, *, max_tokens: int = 4000,
             effort: str = "low", model: str | None = None) -> ChatResult:
        system = "\n\n".join(str(message["content"]) for message in messages if message["role"] == "system")
        conversation = [message for message in messages if message["role"] != "system"]
        if not conversation:
            raise ValueError("chat requires at least one non-system message")
        model_id = model or self.model_name
        output_config: dict[str, Any] = {"effort": effort} if accepts_effort(model_id) else {}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        kwargs: dict[str, Any] = {"model": model_id, "max_tokens": max_tokens, "messages": conversation}
        if output_config:
            kwargs["output_config"] = output_config
        if system:
            kwargs["system"] = system
        client = self._client()
        try:
            response = client.messages.create(**kwargs)
        except anthropic.APIConnectionError as e:  # timeouts included; the SDK has already retried
            raise ProviderUnavailable(f"couldn't reach {model_id} on Bedrock: {e}") from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
            raise ProviderUnavailable(f"Bedrock refused {model_id} for these credentials ({e.status_code}): "
                                      f"{e.message}") from e
        except anthropic.RateLimitError as e:
            raise ProviderUnavailable(f"Bedrock is rate-limiting {model_id}; try again shortly") from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise ProviderUnavailable(f"Bedrock failed on {model_id} ({e.status_code}); try again shortly") from e
            raise ProviderError(f"Bedrock rejected the request to {model_id} ({e.status_code}): {e.message}") from e
        except anthropic.APIError as e:  # a response the SDK couldn't read
            raise ProviderError(f"unreadable response from {model_id}: {e}") from e
        if response.stop_reason == "refusal":
            raise ProviderError(f"{model_id} declined to answer")
        if response.stop_reason == "max_tokens":
            raise ProviderError(f"{model_id}'s answer was cut off at max_tokens={max_tokens}")
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise ProviderError(f"{model_id} returned no text (stop reason: {response.stop_reason})")
        return ChatResult(text, {"input_tokens": response.usage.input_tokens,
                                 "output_tokens": response.usage.output_tokens})


@lru_cache(maxsize=1)
def _embedder() -> FastEmbedProvider:
    return FastEmbedProvider(settings.embedding_model, settings.embedding_batch_size, settings.models_dir)


@lru_cache(maxsize=1)
def _reranker() -> CrossEncoderProvider:
    return CrossEncoderProvider(settings.reranker_model, settings.rerank_batch_size, settings.models_dir)


@lru_cache(maxsize=1)
def _chat_model() -> BedrockProvider:
    return BedrockProvider(settings.llm_model, settings.hosted_retries)


def embed(texts: list[str], *, query: bool = False) -> list[list[float]]:
    return _embedder().embed(texts, query=query)


def chat(messages: list[dict[str, Any]], schema: dict | None = None, **options: Any) -> ChatResult:
    return _chat_model().chat(messages, schema, **options)


def rerank(query: str, docs: list[str]) -> list[float]:
    return _reranker().rerank(query, docs)
