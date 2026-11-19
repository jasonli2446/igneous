"""Criterion 5: LightRAG answers from OUR graph, with no re-extraction and the same embedding model.

    uv run igneous export lightrag /tmp/igneous_kg.json
    uv run --with lightrag-hku==1.5.7 python eval/lightrag_demo.py /tmp/igneous_kg.json "Who mentioned UBS?"

Loads the export with `insert_custom_kg`, then runs a hybrid query. Hosted calls are forbidden during load;
ordinary queries use Bedrock for keywords and answering. --context-only with explicit keywords forbids all
hosted calls. Embeddings use the same local model as Igneous.
"""
import argparse
import asyncio
import shutil
import tempfile

import numpy as np

from igneous.config import settings
from igneous.llm import chat
from igneous.rag.embed import embed_texts


async def main(kg_path: str, question: str | None, *, load_only: bool = False,
               context_only: bool = False, keywords: list[str] | None = None):
    import json

    from lightrag import LightRAG, QueryParam
    from lightrag.utils import EmbeddingFunc

    if context_only and not keywords:
        raise ValueError("context-only retrieval requires explicit keywords")
    calls = {"llm": 0}
    querying = False

    async def embed(texts: list[str]) -> np.ndarray:
        return np.array(embed_texts(texts))

    async def complete(prompt, system_prompt=None, history_messages=None, **kwargs) -> str:
        calls["llm"] += 1
        if not querying or load_only or context_only:
            raise RuntimeError("hosted calls are disabled during load and context-only retrieval")
        messages = ([{"role": "system", "content": system_prompt}] if system_prompt else [])
        messages.extend(history_messages or [])
        messages.append({"role": "user", "content": prompt})
        return chat(messages, max_tokens=2000, effort="low", model=settings.llm_model).text

    work = tempfile.mkdtemp(prefix="igneous-lightrag-")
    try:
        rag = LightRAG(working_dir=work, llm_model_func=complete,
                       embedding_func=EmbeddingFunc(embedding_dim=settings.embedding_dim, func=embed,
                                                    model_name=settings.embedding_model))
        await rag.initialize_storages()
        try:
            with open(kg_path) as file:
                kg = json.load(file)
            await rag.ainsert_custom_kg(kg)
            print(f"loaded {len(kg['chunks'])} chunks, {len(kg['entities'])} entities, "
                  f"{len(kg['relationships'])} relationships; LLM calls during load: {calls['llm']}")
            if calls["llm"]:
                raise RuntimeError("custom-KG loading unexpectedly called the model")
            if load_only:
                return
            querying = True
            param = QueryParam(mode="hybrid", top_k=20, enable_rerank=False, only_need_context=context_only)
            if context_only:
                param.ll_keywords = keywords
                param.hl_keywords = ["project", "organization", "relationship"]
            answer = await rag.aquery(question, param=param)
            print(f"\nQ: {question}\n{'Context' if context_only else 'A'}: {answer}\n"
                  f"(LLM calls during query: {calls['llm']})")
            if context_only and calls["llm"]:
                raise RuntimeError("explicit-keyword context retrieval unexpectedly called the model")
        finally:
            await rag.finalize_storages()
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kg_path")
    parser.add_argument("question", nargs="?")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--load-only", action="store_true", help="verify loading without making a hosted request")
    mode.add_argument("--context-only", action="store_true", help="retrieve graph context without hosted synthesis")
    parser.add_argument("--keyword", action="append", help="entity name for --context-only; repeatable")
    args = parser.parse_args()
    if not args.load_only and not args.question:
        parser.error("provide a question, or use --load-only")
    if args.context_only and not args.keyword:
        parser.error("--context-only needs at least one --keyword to avoid hosted keyword extraction")
    asyncio.run(main(args.kg_path, args.question, load_only=args.load_only,
                     context_only=args.context_only, keywords=args.keyword))
