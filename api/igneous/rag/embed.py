"""Embedding backfill. Local ONNX model (fastembed) so it runs on a laptop with no GPU and no torch."""
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings

BATCH = 128


@lru_cache(maxsize=1)
def model():
    from fastembed import TextEmbedding

    return TextEmbedding(settings.embedding_model)


def embed_texts(texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in model().embed(texts)]


def embed_query(q: str) -> list[float]:
    return [v.tolist() for v in model().query_embed(q)][0]


def run(s: Session, log=print) -> int:
    total = 0
    while True:
        rows = s.execute(text("SELECT id, text FROM chunks WHERE embedding IS NULL ORDER BY id LIMIT :n"),
                         {"n": BATCH}).all()
        if not rows:
            break
        vecs = embed_texts([r.text for r in rows])
        s.execute(text("UPDATE chunks SET embedding = CAST(:v AS vector) WHERE id = :id"),
                  [{"id": r.id, "v": str(v)} for r, v in zip(rows, vecs)])
        s.commit()
        total += len(rows)
        if total % (BATCH * 10) == 0:
            log(f"  embedded {total}")
    return total
