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
    return [v.tolist() for v in model().embed(texts, batch_size=BATCH)]


def embed_query(q: str) -> list[float]:
    return next(v.tolist() for v in model().query_embed(q))


def run(s: Session, log=print) -> int:
    total = 0
    while True:
        rows = s.execute(text("SELECT id, text FROM chunks WHERE embedding IS NULL ORDER BY id LIMIT :n"),
                         {"n": BATCH}).all()
        if not rows:
            break
        vecs = embed_texts([r.text for r in rows])
        # One statement per batch; per-row UPDATEs made the DB, not the model, the bottleneck (~10x).
        s.execute(text("""
            UPDATE chunks c SET embedding = CAST(u.v AS vector)
            FROM unnest(CAST(:ids AS bigint[]), CAST(:vs AS text[])) AS u(id, v) WHERE c.id = u.id
        """), {"ids": [r.id for r in rows], "vs": [str(v) for v in vecs]})
        s.commit()
        total += len(rows)
        if total % (BATCH * 10) == 0:
            log(f"  embedded {total}")
    return total
