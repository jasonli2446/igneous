"""Check the model providers: the local ones always, the hosted ones only when asked (a paid request to each).

    cd api && uv run python scripts/provider_smoke.py
    cd api && AWS_BEARER_TOKEN_BEDROCK=... uv run python scripts/provider_smoke.py --hosted

Local: one embedding and one reranking, on this computer. --hosted: one schema-constrained request to each hosted
model Igneous uses (IGNEOUS_LLM_MODEL for answers, IGNEOUS_LLM_CHEAP_MODEL for high-volume checks), sent the way every
model call is (llm.structured), and one turn with a tool to IGNEOUS_LLM_MODEL, sent the way the research agent's turns
are (llm.agent_turn: a strict tool, the choice left to the model), so a model that rejects the request's shape (its
effort setting, the JSON schema, the tool) fails here first. It sends no record, only the instructions below. Exits
non-zero if anything fails.
"""
import argparse
import os

from igneous import llm
from igneous.config import settings
from igneous.providers import embed, rerank

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
    "required": ["ok", "word"],
    "additionalProperties": False,
}
TOOL = {"name": "record_word", "description": "Record one word.",
        "input_schema": {"type": "object", "properties": {"word": {"type": "string", "maxLength": 40}},
                         "required": ["word"]}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hosted", action="store_true", help="also make one paid request to each hosted model")
    args = parser.parse_args()

    vectors = embed(["Igneous provider smoke test."])
    scores = rerank("Igneous", ["Igneous provider smoke test.", "A different document."])
    print(f"local: {settings.embedding_model} embedded 1 text into {len(vectors[0])} dimensions; "
          f"{settings.reranker_model} scored {len(scores)} documents")

    if not args.hosted:
        print("hosted models not called. Re-run with --hosted and AWS_BEARER_TOKEN_BEDROCK set to test them.")
        return
    if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        raise SystemExit("--hosted requires AWS_BEARER_TOKEN_BEDROCK; no hosted request was made.")
    failed = 0
    for role, model in (("answers", settings.llm_model), ("checks", settings.llm_cheap_model)):
        try:
            out, usage = llm.structured("Reply in the JSON shape given.", "Set ok to true and word to 'igneous'.",
                                        SCHEMA, max_tokens=1024, model=model)
        except llm.ProviderError as error:
            failed += 1
            print(f"{role} model {model}: FAILED: {error}")
            continue
        good = out.get("ok") is True and str(out.get("word", "")).strip().lower() == "igneous"
        failed += not good
        print(f"{role} model {model}: {'ok' if good else 'unexpected answer'} {out} "
              f"({usage['input_tokens']} tokens in, {usage['output_tokens']} out)")
    try:
        turn = llm.agent_turn("Answer by calling the record_word tool.", [
            {"role": "user", "content": "Record the word 'igneous'."}], [TOOL], max_tokens=4096)
    except llm.ProviderError as error:
        failed += 1
        print(f"research agent's turn on {settings.llm_model}: FAILED: {error}")
    else:
        calls = turn.tool_calls
        good = bool(calls) and calls[0]["name"] == "record_word" and \
            str(calls[0]["input"].get("word", "")).strip().lower() == "igneous"
        failed += not good
        print(f"research agent's turn on {settings.llm_model}: {'ok' if good else 'unexpected turn'} "
              f"(stop reason {turn.stop_reason}, calls {calls}, {turn.usage['input_tokens']} tokens in, "
              f"{turn.usage['output_tokens']} out)")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
