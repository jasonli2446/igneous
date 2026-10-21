"""Embedding backfill through the configured model provider."""
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings
from igneous.providers import embed

BATCH = settings.embedding_batch_size


def embed_texts(texts: list[str]) -> list[list[float]]:
    return embed(texts)


def embed_query(q: str) -> list[float]:
    return embed([q], query=True)[0]


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
