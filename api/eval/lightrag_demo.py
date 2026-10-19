"""Criterion 5: LightRAG answers from OUR graph, with no re-extraction and the same embedding model.

    uv run igneous export lightrag /tmp/igneous_kg.json
    uv run --with lightrag-hku python eval/lightrag_demo.py /tmp/igneous_kg.json "Who mentioned UBS?"

Loads the export with `insert_custom_kg` (LightRAG never extracts entities itself here: its LLM is only used
to answer), then runs a hybrid query. Uses the same local embedding model as Igneous and the same Bedrock LLM.
"""
import asyncio
import shutil
import sys
import tempfile

import numpy as np

from igneous import llm
from igneous.config import settings
from igneous.rag.embed import embed_texts


async def main(kg_path: str, question: str):
    import json

    from lightrag import LightRAG, QueryParam
    from lightrag.utils import EmbeddingFunc

    calls = {"llm": 0}

    async def embed(texts: list[str]) -> np.ndarray:
        return np.array(embed_texts(texts))

    async def complete(prompt, system_prompt=None, history_messages=None, **kwargs) -> str:
        calls["llm"] += 1
        r = llm.client().messages.create(
            model=settings.llm_model, max_tokens=2000, system=system_prompt or "",
            messages=[*(history_messages or []), {"role": "user", "content": prompt}],
            output_config={"effort": "low"})
        return next(b.text for b in r.content if b.type == "text")

    work = tempfile.mkdtemp(prefix="igneous-lightrag-")
    try:
        rag = LightRAG(working_dir=work, llm_model_func=complete,
                       embedding_func=EmbeddingFunc(embedding_dim=settings.embedding_dim, func=embed,
                                                    model_name=settings.embedding_model))
        await rag.initialize_storages()
        kg = json.load(open(kg_path))
        await rag.ainsert_custom_kg(kg)
        print(f"loaded {len(kg['chunks'])} chunks, {len(kg['entities'])} entities, "
              f"{len(kg['relationships'])} relationships; LLM calls during load: {calls['llm']}")
        calls["llm"] = 0
        answer = await rag.aquery(question, param=QueryParam(mode="hybrid", top_k=20))
        print(f"\nQ: {question}\nA: {answer}\n(LLM calls during query: {calls['llm']})")
        await rag.finalize_storages()
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
