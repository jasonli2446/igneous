"""Backward-compatible structured chat helpers backed by :mod:`igneous.providers`."""
import json

from igneous.providers import LLMDisabled, chat, get_provider

__all__ = ["LLMDisabled", "client", "structured"]


def client():
    """Return the existing Bedrock client for legacy integrations.

    New application code should call ``igneous.providers.chat`` instead.
    """
    provider = get_provider()
    return provider.chat_provider._client()  # type: ignore[attr-defined]


def structured(system: str, user: str, schema: dict, max_tokens: int = 4000, effort: str = "low",
               model: str | None = None) -> tuple[dict, dict]:
    """One schema-constrained chat call returning parsed JSON and usage."""
    result = chat([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], schema, max_tokens=max_tokens, effort=effort, model=model)
    return json.loads(result.text), result.usage
