"""Exercise configured local providers; opt in explicitly before a paid hosted request.

    cd api && uv run python scripts/provider_smoke.py
    cd api && AWS_BEARER_TOKEN_BEDROCK=... uv run python scripts/provider_smoke.py --hosted
"""
import argparse
import os

from igneous.providers import LLMDisabled, chat, embed, rerank


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosted", action="store_true", help="make one real Bedrock chat request (may incur cost)")
    args = parser.parse_args()

    vectors = embed(["Igneous provider smoke test."])
    scores = rerank("Igneous", ["Igneous provider smoke test.", "A different document."])
    print(f"local providers: embedded {len(vectors)} text(s) into {len(vectors[0])} dimensions; reranked {len(scores)} docs")

    if not args.hosted:
        print("hosted chat not called. Re-run with --hosted and an explicit AWS_BEARER_TOKEN_BEDROCK to test it.")
        return
    if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        raise SystemExit("--hosted requires AWS_BEARER_TOKEN_BEDROCK; no hosted request was made.")
    try:
        result = chat([{"role": "user", "content": "Reply with exactly: provider smoke passed"}], max_tokens=32)
    except LLMDisabled as error:
        raise SystemExit(f"hosted chat not called: {error}") from error
    print(f"hosted chat: {result.text}")


if __name__ == "__main__":
    main()
