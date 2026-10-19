"""The one place model calls go through (CO-4). Swap providers here, nowhere else.

Claude Sonnet 5.5 on Amazon Bedrock via a bearer token (AWS_BEARER_TOKEN_BEDROCK + AWS_REGION). On this
setup Sonnet 5.5 is reachable only through the InvokeModel client with the `us.` inference-profile ID, and
it supports structured outputs there, so callers get schema-valid JSON back.
"""
import json
import os
from functools import lru_cache

import anthropic

from igneous.config import settings


class LLMDisabled(RuntimeError):
    pass


@lru_cache(maxsize=1)
def client() -> anthropic.AnthropicBedrock:
    if not settings.llm_enabled:
        raise LLMDisabled("model calls disabled (IGNEOUS_LLM_ENABLED=false)")
    token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    if not token:
        raise LLMDisabled("AWS_BEARER_TOKEN_BEDROCK is not set")
    return anthropic.AnthropicBedrock(aws_region=os.environ.get("AWS_REGION", "us-east-1"), api_key=token)


def structured(system: str, user: str, schema: dict, max_tokens: int = 4000, effort: str = "low",
               model: str | None = None) -> tuple[dict, dict]:
    """One call returning (parsed JSON matching `schema`, usage). Raises on refusal or truncation."""
    r = client().messages.create(
        model=model or settings.llm_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    if r.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"model stopped: {r.stop_reason}")
    text = next(b.text for b in r.content if b.type == "text")
    usage = {"input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens}
    return json.loads(text), usage
